from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from services.coding.git import (
    run_git,
    scan_changed_content,
    validate_coding_git_config,
)
from services.knowledge.config import KnowledgePolicy, load_knowledge_policy
from services.knowledge.privacy import detect_secret
from services.knowledge.repository import RepositoryError, validate_git_scope


class PublicDataPreflightError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicDataSnapshot:
    head_sha: str
    tracked_manifest_sha256: str
    changed_manifest_sha256: str
    git_object_manifest_sha256: str
    git_metadata_manifest_sha256: str
    tracked_files: int
    tracked_bytes: int
    git_objects: int
    git_object_bytes: int
    git_metadata_bytes: int
    knowledge_blocked_files: int


_OBJECT_ID = re.compile(rb"^[0-9a-f]{40,64}$")
_TREE_MODES = {b"40000", b"100644", b"100755"}
_PACKED_REF = re.compile(r"^(?P<object>[0-9a-f]+) (?P<ref>refs/.+)$")
_PACKED_PEELED = re.compile(r"^\^(?P<object>[0-9a-f]+)$")
_REMOTE_CONFIG_KEY = re.compile(
    r"^remote\.(?P<name>[a-z0-9][a-z0-9._-]{0,127})\.(?P<field>url|fetch)$"
)
_BRANCH_CONFIG_KEY = re.compile(
    r"^branch\.(?P<name>[a-z0-9][a-z0-9._/-]{0,255})\.(?P<field>remote|merge)$"
)
_SAFE_CORE_CONFIG = {
    "core.bare": {"true", "false"},
    "core.filemode": {"true", "false"},
    "core.ignorecase": {"true", "false"},
    "core.logallrefupdates": {"true", "false", "always"},
    "core.precomposeunicode": {"true", "false"},
    "core.repositoryformatversion": {"0", "1"},
    "core.symlinks": {"true", "false"},
    "extensions.compatobjectformat": {"sha1", "sha256"},
    "extensions.objectformat": {"sha1", "sha256"},
    "extensions.worktreeconfig": {"true", "false"},
}
_LOOSE_OBJECT = re.compile(r"^[0-9a-f]{2}/[0-9a-f]+$")
_PACK_FILE = re.compile(
    r"^pack-(?P<object>[0-9a-f]+)\.(?P<suffix>pack|idx|rev|bitmap|mtimes)$"
)
_COMMIT_GRAPH_FILE = re.compile(r"^info/commit-graphs/graph-[0-9a-f]+\.graph$")


def _nfkc_casefold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _private_path(relative: str) -> bool:
    policy = load_knowledge_policy()
    normalized = unicodedata.normalize("NFKC", relative).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    folded = tuple(_nfkc_casefold(part) for part in parts)
    blocked_dirs = {_nfkc_casefold(item) for item in policy.blocked_directory_names}
    blocked_files = {_nfkc_casefold(item) for item in policy.blocked_file_names}
    if not folded:
        return True
    name = folded[-1]
    return (
        any(part in blocked_dirs for part in folded[:-1])
        or name in blocked_files
        or name.startswith(".env")
        or any(
            name.endswith(_nfkc_casefold(item)) for item in policy.blocked_file_suffixes
        )
        or detect_secret(normalized.encode("utf-8", errors="strict")) is not None
    )


def _private_metadata_path(relative: str) -> bool:
    normalized = unicodedata.normalize("NFKC", relative).replace("\\", "/")
    encoded = normalized.encode("utf-8", errors="strict")
    if detect_secret(encoded) is not None:
        return True
    # ``logs`` and ``config`` are policy-blocked names in a worktree, but are
    # expected structural names inside Git metadata. Apply repository path
    # policy only to the attacker-controlled ref/reflog suffix.
    folded = _nfkc_casefold(normalized)
    for marker in ("/refs/", "/logs/"):
        location = folded.find(marker)
        if location >= 0:
            return _private_path(normalized[location + len(marker) :])
    return _private_path(normalized)


def _update_manifest(digest: object, *values: bytes) -> None:
    update = getattr(digest, "update")
    for value in values:
        update(len(value).to_bytes(8, "big"))
        update(value)


def _decode_public_text(payload: bytes, *, kind: str) -> str:
    if b"\0" in payload:
        raise PublicDataPreflightError(f"public preflight rejected binary {kind}")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicDataPreflightError(
            f"public preflight rejected non-UTF-8 {kind}"
        ) from exc
    finding = detect_secret(payload)
    if finding is not None:
        raise PublicDataPreflightError(
            f"public preflight matched {kind} privacy rule {finding}"
        )
    return text


def _resolve_git_directories(repository: Path) -> tuple[Path, Path]:
    git_dir_raw = run_git(
        repository,
        ["rev-parse", "--absolute-git-dir"],
        max_output_bytes=16_384,
    ).stdout
    common_raw = run_git(
        repository,
        ["rev-parse", "--git-common-dir"],
        max_output_bytes=16_384,
    ).stdout
    try:
        git_dir_text = git_dir_raw.decode("utf-8", errors="strict").strip()
        common_text = common_raw.decode("utf-8", errors="strict").strip()
        if not git_dir_text or not common_text:
            raise ValueError
        git_dir = Path(git_dir_text).resolve(strict=True)
        common_candidate = Path(common_text)
        common_dir = (
            common_candidate.resolve(strict=True)
            if common_candidate.is_absolute()
            else (repository / common_candidate).resolve(strict=True)
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PublicDataPreflightError(
            "public preflight could not resolve exact Git metadata"
        ) from exc
    return git_dir, common_dir


def _metadata_is_indirect(path: Path, info: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda value: False)
    return bool(
        path.is_symlink()
        or is_junction(path)
        or getattr(info, "st_reparse_tag", 0)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _read_metadata_file(path: Path, *, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PublicDataPreflightError(
            "public preflight could not read Git metadata"
        ) from exc
    if (
        _metadata_is_indirect(path, before)
        or not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_nlink", 1) > 1
        or before.st_size > maximum
    ):
        raise PublicDataPreflightError(
            "public preflight rejected unsafe or oversized Git metadata"
        )
    try:
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise PublicDataPreflightError(
            "public preflight could not read Git metadata"
        ) from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_nlink,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_nlink,
    )
    if (
        before_identity != after_identity
        or _metadata_is_indirect(path, after)
        or len(payload) != after.st_size
        or len(payload) > maximum
    ):
        raise PublicDataPreflightError(
            "public preflight observed changing Git metadata"
        )
    return payload


def _validate_public_remote_url(value: str) -> None:
    candidate = value.strip()
    if candidate != value or any(ord(character) < 32 for character in candidate):
        raise PublicDataPreflightError(
            "public preflight rejected malformed Git remote URL"
        )
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise PublicDataPreflightError(
            "public preflight rejected malformed Git remote URL"
        ) from exc
    hostname = _nfkc_casefold(parsed.hostname or "")
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in candidate
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        raise PublicDataPreflightError(
            "public preflight accepts only inert credential-free HTTPS remotes"
        )
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        if "." not in hostname:
            raise PublicDataPreflightError(
                "public preflight rejected a non-public Git remote host"
            )
    else:
        if not address.is_global:
            raise PublicDataPreflightError(
                "public preflight rejected a non-public Git remote address"
            )


def _validate_fetch_refspec(value: str, *, remote: str) -> None:
    if not value.isascii() or value.count(":") != 1:
        raise PublicDataPreflightError(
            "public preflight rejected malformed Git fetch refspec"
        )
    source, destination = value.removeprefix("+").split(":", 1)
    expected_prefix = f"refs/remotes/{remote}/"
    if (
        not source.startswith("refs/heads/")
        or not destination.startswith(expected_prefix)
        or source.count("*") != destination.count("*")
        or source.count("*") > 1
        or any(token in value for token in ("..", "@{", "\\", " ", "\t", "\r", "\n"))
        or any(character in value for character in "~^?[]")
    ):
        raise PublicDataPreflightError(
            "public preflight rejected unsafe Git fetch refspec"
        )


def _validate_local_config_record(key: str, value: str) -> None:
    if (
        not key
        or not key.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
    ):
        raise PublicDataPreflightError(
            "public preflight encountered malformed local Git config"
        )
    folded = _nfkc_casefold(key)
    if folded in _SAFE_CORE_CONFIG:
        if _nfkc_casefold(value) not in _SAFE_CORE_CONFIG[folded]:
            raise PublicDataPreflightError(
                "public preflight rejected unsafe local Git config value"
            )
        return
    remote_match = _REMOTE_CONFIG_KEY.fullmatch(folded)
    branch_match = _BRANCH_CONFIG_KEY.fullmatch(folded)
    if remote_match is None and branch_match is None:
        # This is deliberately an allowlist. Git grows new command-, helper-,
        # credential- and path-bearing knobs regularly; an unknown repository
        # setting cannot silently become part of a PUBLIC cloud boundary.
        raise PublicDataPreflightError(
            "public preflight rejected non-allowlisted local Git config"
        )
    if remote_match is not None:
        remote = remote_match.group("name")
        if remote_match.group("field") == "url":
            _validate_public_remote_url(value)
        else:
            _validate_fetch_refspec(value, remote=remote)
        return
    assert branch_match is not None
    if branch_match.group("field") == "remote":
        valid = value == "." or bool(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
        )
    else:
        valid = bool(
            re.fullmatch(
                r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}",
                value,
            )
        ) and not any(token in value for token in ("..", "@{", "//"))
    if not valid:
        raise PublicDataPreflightError(
            "public preflight rejected unsafe Git branch config"
        )


def _scan_local_config_file(repository: Path, path: Path, *, label: str) -> bytes:
    raw = run_git(
        repository,
        [
            "config",
            "--file",
            str(path),
            "--no-includes",
            "--null",
            "--list",
        ],
        timeout=30,
        max_output_bytes=8 * 1024 * 1024,
        isolated_config=True,
    ).stdout
    records = [record for record in raw.split(b"\0") if record]
    seen_singletons: set[str] = set()
    for record in records:
        # Git's --null form terminates each record with NUL but separates its
        # key from the (possibly multi-line) value with the first LF.
        if b"\n" not in record:
            raise PublicDataPreflightError(
                "public preflight encountered malformed local Git config"
            )
        raw_key, raw_value = record.split(b"\n", 1)
        try:
            key = raw_key.decode("utf-8", errors="strict")
            value = raw_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicDataPreflightError(
                "public preflight rejected non-UTF-8 local Git config"
            ) from exc
        finding = detect_secret(raw_key + b"=" + raw_value)
        if finding is not None:
            raise PublicDataPreflightError(
                f"public preflight matched local Git config privacy rule {finding}"
            )
        _validate_local_config_record(key, value)
        folded = _nfkc_casefold(key)
        if not folded.endswith(".fetch") and folded in seen_singletons:
            raise PublicDataPreflightError(
                "public preflight rejected duplicate local Git config"
            )
        seen_singletons.add(folded)
    return label.encode("utf-8", errors="strict") + b"\0" + raw


def _scan_effective_local_config(
    repository: Path,
    *,
    git_dir: Path,
    common_dir: Path,
) -> bytes:
    config_paths = [("common/config", common_dir / "config")]
    worktree_config = git_dir / "config.worktree"
    if os.path.lexists(worktree_config) and worktree_config != common_dir / "config":
        config_paths.append(("git/config.worktree", worktree_config))
    result = bytearray()
    for label, path in config_paths:
        if not os.path.lexists(path):
            raise PublicDataPreflightError(
                "public preflight requires exact local Git config"
            )
        result.extend(_scan_local_config_file(repository, path, label=label))
        result.extend(b"\0")
    return bytes(result)


def _object_hash_spec(repository: Path) -> tuple[str, int]:
    raw = run_git(
        repository,
        ["rev-parse", "--show-object-format"],
        max_output_bytes=16_384,
    ).stdout.strip()
    if raw == b"sha1":
        return "sha1", 20
    if raw == b"sha256":
        return "sha256", 32
    raise PublicDataPreflightError(
        "public preflight encountered unsupported Git object format"
    )


def _validate_ref_name(value: str) -> None:
    normalized = unicodedata.normalize("NFKC", value)
    components = normalized.split("/")
    policy = load_knowledge_policy()
    blocked_directories = {
        _nfkc_casefold(item) for item in policy.blocked_directory_names
    }
    blocked_files = {_nfkc_casefold(item) for item in policy.blocked_file_names}
    public_components = tuple(_nfkc_casefold(component) for component in components[1:])
    if (
        normalized != value
        or not value.startswith("refs/")
        or len(components) < 3
        or any(
            not component or component.startswith(".") or component.endswith(".lock")
            for component in components
        )
        or value.endswith(".")
        or ".." in value
        or "@{" in value
        or any(character in value for character in " ~^:?*[\\")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(component in blocked_directories for component in public_components)
        or any(component in blocked_files for component in public_components)
        or _private_path(value.removeprefix("refs/"))
    ):
        raise PublicDataPreflightError(
            "public preflight rejected unsafe packed ref name"
        )


def _validate_packed_refs(payload: bytes, *, object_id_bytes: int) -> None:
    text = _decode_public_text(payload, kind="packed refs")
    if payload and not payload.endswith(b"\n"):
        raise PublicDataPreflightError(
            "public preflight rejected malformed packed refs"
        )
    expected_hex = object_id_bytes * 2
    seen: set[str] = set()
    previous_was_ref = False
    previous_was_peeled = False
    for ordinal, line in enumerate(text.splitlines()):
        if line.startswith("#"):
            if ordinal != 0 or not line.startswith("# pack-refs with: "):
                raise PublicDataPreflightError(
                    "public preflight rejected malformed packed refs header"
                )
            features = line.removeprefix("# pack-refs with: ").split()
            if any(
                item not in {"peeled", "fully-peeled", "sorted"} for item in features
            ):
                raise PublicDataPreflightError(
                    "public preflight rejected unknown packed refs feature"
                )
            previous_was_ref = False
            continue
        peeled = _PACKED_PEELED.fullmatch(line)
        if peeled is not None:
            if (
                not previous_was_ref
                or previous_was_peeled
                or len(peeled.group("object")) != expected_hex
            ):
                raise PublicDataPreflightError(
                    "public preflight rejected malformed peeled packed ref"
                )
            previous_was_peeled = True
            continue
        match = _PACKED_REF.fullmatch(line)
        if match is None or len(match.group("object")) != expected_hex:
            raise PublicDataPreflightError(
                "public preflight rejected malformed packed ref"
            )
        ref_name = match.group("ref")
        _validate_ref_name(ref_name)
        folded = _nfkc_casefold(ref_name)
        if folded in seen:
            raise PublicDataPreflightError(
                "public preflight rejected duplicate packed ref"
            )
        seen.add(folded)
        previous_was_ref = True
        previous_was_peeled = False


def _validate_repository_path(value: str, *, kind: str) -> None:
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        normalized != value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(
            any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in path.parts
        )
        or _private_path(normalized)
    ):
        raise PublicDataPreflightError(
            f"public preflight rejected privacy-sensitive {kind} path"
        )


def _parse_cache_tree(
    repository: Path,
    payload: bytes,
    *,
    object_id_bytes: int,
    index_entries: int,
) -> None:
    seen: set[str] = set()
    tree_ids: set[bytes] = set()
    records = 0
    blocked_directories = {
        _nfkc_casefold(item) for item in load_knowledge_policy().blocked_directory_names
    }

    def parse_node(offset: int, parent: tuple[str, ...], *, root: bool) -> int:
        nonlocal records
        records += 1
        if records > index_entries + 1 or len(parent) > 256:
            raise PublicDataPreflightError(
                "public preflight rejected oversized cache-tree"
            )
        nul = payload.find(b"\0", offset)
        newline = payload.find(b"\n", nul + 1) if nul >= 0 else -1
        if nul < offset or newline <= nul + 2:
            raise PublicDataPreflightError(
                "public preflight rejected malformed cache-tree"
            )
        try:
            component = payload[offset:nul].decode("utf-8", errors="strict")
            counts = payload[nul + 1 : newline].decode("ascii", errors="strict")
            raw_entries, raw_subtrees = counts.split(" ", 1)
            entry_count = int(raw_entries)
            subtree_count = int(raw_subtrees)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicDataPreflightError(
                "public preflight rejected malformed cache-tree"
            ) from exc
        if (
            (root and component != "")
            or (not root and (not component or "/" in component or "\\" in component))
            or (not root and _nfkc_casefold(component) in blocked_directories)
            or raw_entries != str(entry_count)
            or raw_subtrees != str(subtree_count)
            or entry_count < -1
            or entry_count > index_entries
            or subtree_count < 0
            or subtree_count > index_entries
        ):
            raise PublicDataPreflightError(
                "public preflight rejected invalid cache-tree record"
            )
        current = parent if root else (*parent, component)
        if not root:
            relative = "/".join(current)
            _validate_repository_path(relative, kind="cache-tree")
            folded = _nfkc_casefold(relative)
            if folded in seen:
                raise PublicDataPreflightError(
                    "public preflight rejected duplicate cache-tree path"
                )
            seen.add(folded)
        cursor = newline + 1
        if entry_count >= 0:
            object_end = cursor + object_id_bytes
            if object_end > len(payload):
                raise PublicDataPreflightError(
                    "public preflight rejected truncated cache-tree object"
                )
            object_id = payload[cursor:object_end]
            if not any(object_id):
                raise PublicDataPreflightError(
                    "public preflight rejected null cache-tree object"
                )
            tree_ids.add(object_id.hex().encode("ascii"))
            cursor = object_end
        for _ in range(subtree_count):
            cursor = parse_node(cursor, current, root=False)
        return cursor

    end = parse_node(0, (), root=True)
    if end != len(payload):
        raise PublicDataPreflightError(
            "public preflight rejected trailing cache-tree data"
        )
    for object_id in sorted(tree_ids):
        object_type = run_git(
            repository,
            ["cat-file", "-t", object_id.decode("ascii")],
            max_output_bytes=16_384,
        ).stdout.strip()
        if object_type != b"tree":
            raise PublicDataPreflightError(
                "public preflight rejected invalid cache-tree object"
            )


def _logical_index_entries(
    repository: Path,
    *,
    git_dir: Path,
    work_tree: Path,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    raw = run_git(
        repository,
        [
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(work_tree),
            "ls-files",
            "--stage",
            "-z",
        ],
        timeout=30,
        max_output_bytes=16 * 1024 * 1024,
    ).stdout
    result: list[tuple[bytes, bytes, bytes]] = []
    for record in (item for item in raw.split(b"\0") if item):
        try:
            metadata, path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ")
        except ValueError as exc:
            raise PublicDataPreflightError(
                "public preflight rejected malformed logical index"
            ) from exc
        if stage != b"0":
            raise PublicDataPreflightError(
                "public preflight rejected non-stage-zero logical index"
            )
        result.append((mode, object_id.lower(), path))
    return tuple(result)


def _validate_git_index(
    repository: Path,
    payload: bytes,
    *,
    object_hash: str,
    object_id_bytes: int,
    git_dir: Path,
    work_tree: Path,
    max_entries: int,
) -> None:
    trailer = object_id_bytes
    if len(payload) < 12 + trailer or payload[:4] != b"DIRC":
        raise PublicDataPreflightError("public preflight rejected malformed Git index")
    version, entry_count = struct.unpack("!II", payload[4:12])
    if version != 2 or entry_count > max_entries:
        raise PublicDataPreflightError(
            "public preflight supports only structurally authenticated Git index v2"
        )
    expected_checksum = hashlib.new(object_hash, payload[:-trailer]).digest()
    if payload[-trailer:] != expected_checksum:
        raise PublicDataPreflightError("public preflight rejected Git index checksum")
    limit = len(payload) - trailer
    offset = 12
    parsed: list[tuple[bytes, bytes, bytes]] = []
    normalized_paths: set[str] = set()
    for _ in range(entry_count):
        entry_start = offset
        fixed_size = 40 + object_id_bytes + 2
        if offset + fixed_size > limit:
            raise PublicDataPreflightError(
                "public preflight rejected truncated Git index entry"
            )
        mode = struct.unpack("!I", payload[offset + 24 : offset + 28])[0]
        object_start = offset + 40
        object_id = payload[object_start : object_start + object_id_bytes]
        flags = struct.unpack(
            "!H", payload[object_start + object_id_bytes : offset + fixed_size]
        )[0]
        if flags & 0x4000 or flags & 0x3000:
            raise PublicDataPreflightError(
                "public preflight rejected extended or non-stage-zero Git index entry"
            )
        name_start = offset + fixed_size
        nul = payload.find(b"\0", name_start, limit)
        if nul < name_start:
            raise PublicDataPreflightError(
                "public preflight rejected unterminated Git index path"
            )
        raw_path = payload[name_start:nul]
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicDataPreflightError(
                "public preflight rejected non-UTF-8 Git index path"
            ) from exc
        _validate_repository_path(path, kind="Git index")
        folded = _nfkc_casefold(path)
        declared_length = flags & 0x0FFF
        if (
            (declared_length < 0x0FFF and declared_length != len(raw_path))
            or (declared_length == 0x0FFF and len(raw_path) < 0x0FFF)
            or mode not in {0o100644, 0o100755}
            or not any(object_id)
            or folded in normalized_paths
        ):
            raise PublicDataPreflightError(
                "public preflight rejected invalid Git index entry"
            )
        normalized_paths.add(folded)
        parsed.append(
            (f"{mode:o}".encode("ascii"), object_id.hex().encode("ascii"), raw_path)
        )
        entry_size = fixed_size + len(raw_path) + 1
        padded_size = (entry_size + 7) & ~7
        offset = entry_start + padded_size
        if offset > limit or any(payload[nul + 1 : offset]):
            raise PublicDataPreflightError(
                "public preflight rejected non-zero Git index padding"
            )

    seen_extensions: set[bytes] = set()
    while offset < limit:
        if offset + 8 > limit:
            raise PublicDataPreflightError(
                "public preflight rejected truncated Git index extension"
            )
        signature = payload[offset : offset + 4]
        extension_size = struct.unpack("!I", payload[offset + 4 : offset + 8])[0]
        extension_end = offset + 8 + extension_size
        if (
            extension_end > limit
            or signature in seen_extensions
            or signature != b"TREE"
        ):
            raise PublicDataPreflightError(
                "public preflight rejected unknown or duplicate Git index extension"
            )
        seen_extensions.add(signature)
        _parse_cache_tree(
            repository,
            payload[offset + 8 : extension_end],
            object_id_bytes=object_id_bytes,
            index_entries=entry_count,
        )
        offset = extension_end
    if offset != limit or tuple(parsed) != _logical_index_entries(
        repository,
        git_dir=git_dir,
        work_tree=work_tree,
    ):
        raise PublicDataPreflightError(
            "public preflight rejected Git index/logical listing mismatch"
        )


def _validate_hash_trailer(
    payload: bytes,
    *,
    object_hash: str,
    object_id_bytes: int,
    kind: str,
) -> None:
    if (
        len(payload) <= object_id_bytes
        or payload[-object_id_bytes:]
        != hashlib.new(object_hash, payload[:-object_id_bytes]).digest()
    ):
        raise PublicDataPreflightError(f"public preflight rejected {kind} checksum")


def _validate_loose_object(
    payload: bytes,
    *,
    relative: str,
    object_hash: str,
    max_expanded_bytes: int,
) -> None:
    import zlib

    try:
        inflater = zlib.decompressobj()
        expanded = inflater.decompress(payload, max_expanded_bytes + 1)
    except zlib.error as exc:
        raise PublicDataPreflightError(
            "public preflight rejected malformed loose Git object"
        ) from exc
    if (
        len(expanded) > max_expanded_bytes
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise PublicDataPreflightError(
            "public preflight rejected trailing loose Git object data"
        )
    nul = expanded.find(b"\0")
    if nul <= 0:
        raise PublicDataPreflightError(
            "public preflight rejected malformed loose Git object header"
        )
    header = expanded[:nul]
    try:
        object_type, raw_size = header.split(b" ", 1)
        size = int(raw_size.decode("ascii", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublicDataPreflightError(
            "public preflight rejected malformed loose Git object header"
        ) from exc
    if (
        object_type not in {b"blob", b"commit", b"tree", b"tag"}
        or raw_size != str(size).encode("ascii")
        or size != len(expanded) - nul - 1
        or hashlib.new(object_hash, expanded).hexdigest() != relative.replace("/", "")
    ):
        raise PublicDataPreflightError(
            "public preflight rejected unauthenticated loose Git object"
        )


def _validate_pack_family(
    repository: Path,
    family: dict[str, tuple[Path, bytes]],
    *,
    object_hash: str,
    object_id_bytes: int,
) -> None:
    if set(family).difference({"pack", "idx", "rev", "mtimes"}):
        raise PublicDataPreflightError(
            "public preflight rejected unsupported Git pack control file"
        )
    if "pack" not in family or "idx" not in family:
        raise PublicDataPreflightError(
            "public preflight rejected incomplete Git pack family"
        )
    pack_path, pack = family["pack"]
    index_path, index = family["idx"]
    if len(pack) < 12 + object_id_bytes or pack[:4] != b"PACK":
        raise PublicDataPreflightError("public preflight rejected malformed Git pack")
    pack_version, object_count = struct.unpack("!II", pack[4:12])
    if pack_version not in {2, 3}:
        raise PublicDataPreflightError(
            "public preflight rejected unsupported Git pack version"
        )
    _validate_hash_trailer(
        pack,
        object_hash=object_hash,
        object_id_bytes=object_id_bytes,
        kind="Git pack",
    )
    if len(index) < 8 + 256 * 4 + 2 * object_id_bytes or index[:4] != b"\xfftOc":
        raise PublicDataPreflightError(
            "public preflight requires authenticated Git pack index v2"
        )
    if struct.unpack("!I", index[4:8])[0] != 2:
        raise PublicDataPreflightError(
            "public preflight rejected unsupported Git pack index"
        )
    indexed_count = struct.unpack("!I", index[8 + 255 * 4 : 8 + 256 * 4])[0]
    if indexed_count != object_count:
        raise PublicDataPreflightError(
            "public preflight rejected Git pack/index count mismatch"
        )
    _validate_hash_trailer(
        index,
        object_hash=object_hash,
        object_id_bytes=object_id_bytes,
        kind="Git pack index",
    )
    if index[-2 * object_id_bytes : -object_id_bytes] != pack[-object_id_bytes:]:
        raise PublicDataPreflightError(
            "public preflight rejected Git pack/index checksum mismatch"
        )
    hash_identifier = 1 if object_hash == "sha1" else 2
    for suffix, magic in (("rev", b"RIDX"), ("mtimes", b"MTME")):
        item = family.get(suffix)
        if item is None:
            continue
        _, control = item
        expected_size = 12 + object_count * 4 + 2 * object_id_bytes
        if (
            len(control) != expected_size
            or control[:4] != magic
            or struct.unpack("!II", control[4:12]) != (1, hash_identifier)
        ):
            raise PublicDataPreflightError(
                f"public preflight rejected malformed Git pack {suffix}"
            )
        _validate_hash_trailer(
            control,
            object_hash=object_hash,
            object_id_bytes=object_id_bytes,
            kind=f"Git pack {suffix}",
        )
        if control[-2 * object_id_bytes : -object_id_bytes] != pack[-object_id_bytes:]:
            raise PublicDataPreflightError(
                f"public preflight rejected Git pack/{suffix} checksum mismatch"
            )
        if suffix == "rev":
            positions = struct.unpack(
                f"!{object_count}I",
                control[12 : 12 + object_count * 4],
            )
            if set(positions) != set(range(object_count)):
                raise PublicDataPreflightError(
                    "public preflight rejected invalid Git reverse-index table"
                )
    # Let Git verify all object offsets, CRCs, deltas and inflated object IDs.
    run_git(
        repository,
        ["verify-pack", str(index_path)],
        timeout=120,
        max_output_bytes=16 * 1024 * 1024,
    )
    if not pack_path.is_file():
        raise PublicDataPreflightError("public preflight observed changing Git pack")


def _scan_object_storage_metadata(
    repository: Path,
    *,
    common_dir: Path,
    policy: KnowledgePolicy,
    object_hash: str,
    object_id_bytes: int,
) -> tuple[tuple[tuple[str, str, int], ...], int]:
    objects = common_dir / "objects"
    try:
        root_info = objects.lstat()
    except OSError as exc:
        raise PublicDataPreflightError(
            "public preflight could not inventory Git object storage"
        ) from exc
    if _metadata_is_indirect(objects, root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise PublicDataPreflightError(
            "public preflight rejected unsafe Git object storage"
        )
    expected_hex = object_id_bytes * 2
    records: list[tuple[str, Path]] = []
    entries = 0
    stack = [("", objects)]
    while stack:
        prefix, directory = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise PublicDataPreflightError(
                "public preflight could not inventory Git object storage"
            ) from exc
        for child in children:
            entries += 1
            if entries > policy.max_tracked_files:
                raise PublicDataPreflightError(
                    "public preflight Git object-storage entry limit exceeded"
                )
            path = Path(child.path)
            relative = f"{prefix}/{child.name}".lstrip("/")
            try:
                info = path.lstat()
            except OSError as exc:
                raise PublicDataPreflightError(
                    "public preflight could not inventory Git object storage"
                ) from exc
            if _metadata_is_indirect(path, info):
                raise PublicDataPreflightError(
                    "public preflight rejected indirect Git object storage"
                )
            if stat.S_ISDIR(info.st_mode):
                if (
                    (not prefix and child.name in {"info", "pack"})
                    or (not prefix and re.fullmatch(r"[0-9a-f]{2}", child.name))
                    or (prefix == "info" and child.name == "commit-graphs")
                ):
                    stack.append((relative, path))
                    continue
                raise PublicDataPreflightError(
                    "public preflight rejected unknown Git object-storage directory"
                )
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                raise PublicDataPreflightError(
                    "public preflight rejected unsafe Git object-storage entry"
                )
            records.append((relative, path))

    allowed_info = {
        "info/packs",
        "info/commit-graph",
        "info/commit-graphs/commit-graph-chain",
    }
    total = 0
    manifest: list[tuple[str, str, int]] = []
    payloads: dict[str, bytes] = {}
    pack_families: dict[str, dict[str, tuple[Path, bytes]]] = {}
    for relative, path in sorted(records, key=lambda item: item[0]):
        if _private_metadata_path(f"common/objects/{relative}"):
            raise PublicDataPreflightError(
                "public preflight rejected privacy-sensitive Git object metadata path"
            )
        remaining = policy.max_git_output_bytes - total
        if remaining < 0:
            raise PublicDataPreflightError(
                "public preflight Git object-storage byte limit exceeded"
            )
        payload = _read_metadata_file(
            path,
            maximum=min(policy.max_git_output_bytes, remaining),
        )
        if detect_secret(payload) is not None:
            raise PublicDataPreflightError(
                "public preflight matched Git object-storage privacy rule"
            )
        payloads[relative] = payload
        total += len(payload)
        manifest.append((relative, hashlib.sha256(payload).hexdigest(), len(payload)))
        loose = _LOOSE_OBJECT.fullmatch(relative)
        pack_match = (
            _PACK_FILE.fullmatch(relative.removeprefix("pack/"))
            if relative.startswith("pack/")
            else None
        )
        if loose is not None:
            if len(relative.replace("/", "")) != expected_hex:
                raise PublicDataPreflightError(
                    "public preflight rejected malformed loose-object name"
                )
            _validate_loose_object(
                payload,
                relative=relative,
                object_hash=object_hash,
                max_expanded_bytes=policy.max_file_bytes + 128,
            )
        elif pack_match is not None:
            if len(pack_match.group("object")) != expected_hex:
                raise PublicDataPreflightError(
                    "public preflight rejected malformed Git pack name"
                )
            suffix = pack_match.group("suffix")
            if suffix == "bitmap":
                raise PublicDataPreflightError(
                    "public preflight rejects unsupported Git pack bitmap metadata"
                )
            family = pack_families.setdefault(pack_match.group("object"), {})
            family[suffix] = (path, payload)
        elif relative in allowed_info:
            pass
        elif _COMMIT_GRAPH_FILE.fullmatch(relative) is not None:
            pass
        else:
            raise PublicDataPreflightError(
                "public preflight rejected unknown Git object-storage control file"
            )

    for family in pack_families.values():
        _validate_pack_family(
            repository,
            family,
            object_hash=object_hash,
            object_id_bytes=object_id_bytes,
        )

    expected_packs = {
        f"P pack-{name}.pack"
        for name, family in pack_families.items()
        if "pack" in family
    }
    packs_payload = payloads.get("info/packs")
    if packs_payload is not None:
        packs_text = _decode_public_text(packs_payload, kind="Git object pack list")
        if packs_payload and not packs_payload.endswith(b"\n"):
            raise PublicDataPreflightError(
                "public preflight rejected malformed Git object pack list"
            )
        pack_lines = packs_text.splitlines()
        if pack_lines and pack_lines[-1] == "":
            pack_lines.pop()
        if (
            any(not line for line in pack_lines)
            or len(pack_lines) != len(set(pack_lines))
            or set(pack_lines) != expected_packs
        ):
            raise PublicDataPreflightError(
                "public preflight rejected Git object pack-list mismatch"
            )

    graph_paths = [
        relative
        for relative in payloads
        if relative == "info/commit-graph" or _COMMIT_GRAPH_FILE.fullmatch(relative)
    ]
    for relative in graph_paths:
        graph = payloads[relative]
        if len(graph) <= object_id_bytes or graph[:4] != b"CGPH":
            raise PublicDataPreflightError(
                "public preflight rejected malformed Git commit graph"
            )
        _validate_hash_trailer(
            graph,
            object_hash=object_hash,
            object_id_bytes=object_id_bytes,
            kind="Git commit graph",
        )
        if relative.startswith("info/commit-graphs/") and not relative.endswith(
            f"{graph[-object_id_bytes:].hex()}.graph"
        ):
            raise PublicDataPreflightError(
                "public preflight rejected Git commit-graph filename mismatch"
            )
    chain = payloads.get("info/commit-graphs/commit-graph-chain")
    if chain is not None:
        chain_text = _decode_public_text(chain, kind="Git commit-graph chain")
        if chain and not chain.endswith(b"\n"):
            raise PublicDataPreflightError(
                "public preflight rejected malformed Git commit-graph chain"
            )
        expected_chain = {
            Path(relative).stem.removeprefix("graph-")
            for relative in graph_paths
            if relative.startswith("info/commit-graphs/")
        }
        lines = chain_text.splitlines()
        if (
            len(lines) != len(set(lines))
            or set(lines) != expected_chain
            or any(
                len(line) != expected_hex or not re.fullmatch(r"[0-9a-f]+", line)
                for line in lines
            )
        ):
            raise PublicDataPreflightError(
                "public preflight rejected Git commit-graph chain mismatch"
            )
    elif any(relative.startswith("info/commit-graphs/") for relative in graph_paths):
        raise PublicDataPreflightError(
            "public preflight requires a Git commit-graph chain"
        )
    if graph_paths:
        run_git(
            repository,
            ["commit-graph", "verify", "--object-dir", str(objects), "--no-progress"],
            timeout=120,
            max_output_bytes=16 * 1024 * 1024,
        )
    return tuple(manifest), total


def _scan_git_metadata(
    repository: Path,
    *,
    policy: KnowledgePolicy,
) -> tuple[str, int]:
    git_dir, common_dir = _resolve_git_directories(repository)
    object_hash, object_id_bytes = _object_hash_spec(repository)
    linked_admin_root = common_dir / "worktrees"
    if os.path.lexists(linked_admin_root):
        try:
            root_info = linked_admin_root.lstat()
            if _metadata_is_indirect(linked_admin_root, root_info) or not stat.S_ISDIR(
                root_info.st_mode
            ):
                raise PublicDataPreflightError(
                    "public preflight rejected unsafe linked-worktree administration"
                )
            linked_entries = list(os.scandir(linked_admin_root))
        except OSError as exc:
            raise PublicDataPreflightError(
                "public preflight could not inventory linked worktrees"
            ) from exc
        exact_current = (
            os.path.normcase(str(git_dir.resolve(strict=True)))
            if git_dir != common_dir and git_dir.parent == linked_admin_root
            else None
        )
        for entry in linked_entries:
            candidate = Path(entry.path)
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise PublicDataPreflightError(
                    "public preflight could not inventory linked worktrees"
                ) from exc
            if (
                _metadata_is_indirect(candidate, info)
                or not stat.S_ISDIR(info.st_mode)
                or exact_current is None
                or os.path.normcase(str(candidate.resolve(strict=True)))
                != exact_current
            ):
                # Another linked worktree exposes a separate index, worktree
                # pointer and possibly staged object graph through the shared
                # common directory. Do not classify that unrelated mutable
                # state as PUBLIC by association.
                raise PublicDataPreflightError(
                    "public preflight rejects additional linked worktrees"
                )
        if exact_current is not None and len(linked_entries) != 1:
            raise PublicDataPreflightError(
                "public preflight rejects additional linked worktrees"
            )
    elif git_dir != common_dir:
        raise PublicDataPreflightError(
            "public preflight rejected unbound linked-worktree administration"
        )
    object_info = common_dir / "objects" / "info"
    if os.path.lexists(object_info / "alternates"):
        raise PublicDataPreflightError(
            "public preflight rejects alternate Git object stores"
        )
    digest = hashlib.sha256()
    effective = _scan_effective_local_config(
        repository,
        git_dir=git_dir,
        common_dir=common_dir,
    )
    _update_manifest(digest, b"effective-local-config", effective)
    total = len(effective)

    object_storage_manifest, object_storage_bytes = _scan_object_storage_metadata(
        repository,
        common_dir=common_dir,
        policy=policy,
        object_hash=object_hash,
        object_id_bytes=object_id_bytes,
    )
    total += object_storage_bytes
    if total > policy.max_git_output_bytes:
        raise PublicDataPreflightError(
            "public preflight Git metadata byte limit exceeded"
        )
    for relative, payload_sha256, size in object_storage_manifest:
        _update_manifest(
            digest,
            f"common/objects/{relative}".encode("utf-8", errors="strict"),
            bytes.fromhex(payload_sha256),
            str(size).encode("ascii"),
        )

    records: list[tuple[str, Path]] = []
    index_records: list[tuple[str, Path, Path, Path]] = []
    direct = (
        ("common/config", common_dir / "config"),
        ("common/packed-refs", common_dir / "packed-refs"),
        ("common/shallow", common_dir / "shallow"),
        ("common/description", common_dir / "description"),
        ("common/info/exclude", common_dir / "info" / "exclude"),
        ("git/config.worktree", git_dir / "config.worktree"),
        ("git/HEAD", git_dir / "HEAD"),
        ("git/ORIG_HEAD", git_dir / "ORIG_HEAD"),
        ("git/FETCH_HEAD", git_dir / "FETCH_HEAD"),
        ("git/MERGE_HEAD", git_dir / "MERGE_HEAD"),
        ("git/CHERRY_PICK_HEAD", git_dir / "CHERRY_PICK_HEAD"),
        ("git/REVERT_HEAD", git_dir / "REVERT_HEAD"),
        ("git/BISECT_LOG", git_dir / "BISECT_LOG"),
        ("git/AUTO_MERGE", git_dir / "AUTO_MERGE"),
    )
    seen_paths: set[str] = set()
    for label, candidate in direct:
        key = os.path.normcase(str(candidate))
        if os.path.lexists(candidate) and key not in seen_paths:
            seen_paths.add(key)
            records.append((label, candidate))

    for label, candidate, index_git_dir, work_tree in (
        ("common/index", common_dir / "index", common_dir, common_dir.parent),
        ("git/index", git_dir / "index", git_dir, repository),
    ):
        key = os.path.normcase(str(candidate))
        if os.path.lexists(candidate) and key not in seen_paths:
            seen_paths.add(key)
            index_records.append((label, candidate, index_git_dir, work_tree))
    if not index_records:
        raise PublicDataPreflightError(
            "public preflight requires a structurally authenticated Git index"
        )
    worktree_pointer = repository / ".git"
    if git_dir != common_dir and os.path.lexists(worktree_pointer):
        key = os.path.normcase(str(worktree_pointer))
        if key not in seen_paths:
            seen_paths.add(key)
            records.append(("worktree/.git", worktree_pointer))

    # Root metadata is directly readable by a native Codex process through
    # the linked .git graph. Inventory every regular root file instead of a
    # name allowlist so credential files, stale commit messages, gc logs or a
    # future Git metadata file cannot sit outside the PUBLIC proof. Binary
    # indexes are inventoried here and authenticated separately below.
    for root_label, root in (("common", common_dir), ("git", git_dir)):
        try:
            children = list(os.scandir(root))
        except OSError as exc:
            raise PublicDataPreflightError(
                "public preflight could not inventory Git metadata root"
            ) from exc
        for child in children:
            child_path = Path(child.path)
            try:
                info = child_path.lstat()
            except OSError as exc:
                raise PublicDataPreflightError(
                    "public preflight could not inventory Git metadata root"
                ) from exc
            if _metadata_is_indirect(child_path, info):
                raise PublicDataPreflightError(
                    "public preflight rejected indirect Git metadata"
                )
            if not stat.S_ISREG(info.st_mode):
                continue
            if child.name == "index":
                continue
            key = os.path.normcase(str(child_path))
            if key not in seen_paths:
                seen_paths.add(key)
                records.append((f"{root_label}/{child.name}", child_path))

    tree_roots: list[tuple[str, Path]] = [
        ("common/refs", common_dir / "refs"),
        ("common/logs", common_dir / "logs"),
        ("common/hooks", common_dir / "hooks"),
        ("git/refs", git_dir / "refs"),
        ("git/logs", git_dir / "logs"),
        ("git/hooks", git_dir / "hooks"),
    ]
    # Cover future/optional metadata (info/refs, rr-cache, sequencer,
    # modules, LFS metadata, maintenance state, etc.) without pretending a
    # fixed Git-version allowlist is complete. Object storage is proven from
    # its logical objects below; linked-worktree administration was restricted
    # to the exact current git_dir above.
    for root_label, root in (("common", common_dir), ("git", git_dir)):
        try:
            children = list(os.scandir(root))
        except OSError as exc:
            raise PublicDataPreflightError(
                "public preflight could not inventory Git metadata root"
            ) from exc
        for child in children:
            child_path = Path(child.path)
            try:
                info = child_path.lstat()
            except OSError as exc:
                raise PublicDataPreflightError(
                    "public preflight could not inventory Git metadata root"
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                continue
            if child.name.casefold() in {"objects", "worktrees"}:
                continue
            tree_roots.append((f"{root_label}/{child.name}", child_path))

    entries = 0
    for label, root in tree_roots:
        root_key = os.path.normcase(str(root))
        if root_key in seen_paths or not os.path.lexists(root):
            continue
        seen_paths.add(root_key)
        try:
            root_info = root.lstat()
        except OSError as exc:
            raise PublicDataPreflightError(
                "public preflight could not inventory Git metadata"
            ) from exc
        if _metadata_is_indirect(root, root_info) or not stat.S_ISDIR(
            root_info.st_mode
        ):
            raise PublicDataPreflightError(
                "public preflight rejected unsafe Git metadata tree"
            )
        stack = [(label, root)]
        while stack:
            relative_root, directory = stack.pop()
            try:
                children = list(os.scandir(directory))
            except OSError as exc:
                raise PublicDataPreflightError(
                    "public preflight could not inventory Git metadata"
                ) from exc
            for child in children:
                entries += 1
                if entries > policy.max_tracked_files:
                    raise PublicDataPreflightError(
                        "public preflight Git metadata entry limit exceeded"
                    )
                child_path = Path(child.path)
                try:
                    # DirEntry stat may return zeroed link metadata on
                    # Windows; direct lstat preserves the hardlink proof.
                    info = child_path.lstat()
                except OSError as exc:
                    raise PublicDataPreflightError(
                        "public preflight could not inventory Git metadata"
                    ) from exc
                if _metadata_is_indirect(child_path, info):
                    raise PublicDataPreflightError(
                        "public preflight rejected indirect Git metadata"
                    )
                child_label = f"{relative_root}/{child.name}"
                if stat.S_ISDIR(info.st_mode):
                    stack.append((child_label, child_path))
                elif stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) == 1:
                    key = os.path.normcase(str(child_path))
                    if key not in seen_paths:
                        seen_paths.add(key)
                        records.append((child_label, child_path))
                else:
                    raise PublicDataPreflightError(
                        "public preflight rejected unsafe Git metadata entry"
                    )

    for label, path in sorted(records, key=lambda item: item[0].casefold()):
        normalized_label = label.replace("\\", "/")
        if _private_metadata_path(normalized_label):
            raise PublicDataPreflightError(
                "public preflight rejected privacy-sensitive Git metadata path"
            )
        remaining = policy.max_git_output_bytes - total
        if remaining < 0:
            raise PublicDataPreflightError(
                "public preflight Git metadata byte limit exceeded"
            )
        payload = _read_metadata_file(
            path,
            maximum=min(policy.max_file_bytes, remaining),
        )
        if normalized_label.endswith("/packed-refs"):
            _validate_packed_refs(payload, object_id_bytes=object_id_bytes)
        else:
            _decode_public_text(payload, kind="Git metadata")
        total += len(payload)
        _update_manifest(
            digest,
            normalized_label.encode("utf-8", errors="strict"),
            hashlib.sha256(payload).digest(),
        )
    for label, path, index_git_dir, work_tree in sorted(
        index_records,
        key=lambda item: item[0],
    ):
        remaining = policy.max_git_output_bytes - total
        if remaining < 0:
            raise PublicDataPreflightError(
                "public preflight Git metadata byte limit exceeded"
            )
        payload = _read_metadata_file(
            path,
            maximum=min(policy.max_git_output_bytes, remaining),
        )
        _validate_git_index(
            repository,
            payload,
            object_hash=object_hash,
            object_id_bytes=object_id_bytes,
            git_dir=index_git_dir,
            work_tree=work_tree,
            max_entries=policy.max_tracked_files,
        )
        total += len(payload)
        _update_manifest(
            digest,
            label.encode("utf-8", errors="strict"),
            hashlib.sha256(payload).digest(),
        )
    repeated_manifest, repeated_bytes = _scan_object_storage_metadata(
        repository,
        common_dir=common_dir,
        policy=policy,
        object_hash=object_hash,
        object_id_bytes=object_id_bytes,
    )
    if (
        repeated_manifest != object_storage_manifest
        or repeated_bytes != object_storage_bytes
    ):
        raise PublicDataPreflightError(
            "public preflight observed a changing Git object store"
        )
    return digest.hexdigest(), total


def _object_listing(
    repository: Path,
    *,
    policy: KnowledgePolicy,
) -> tuple[tuple[bytes, bytes, int], ...]:
    raw = run_git(
        repository,
        [
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            "--batch-all-objects",
        ],
        timeout=120,
        max_output_bytes=policy.max_git_output_bytes,
    ).stdout
    result: list[tuple[bytes, bytes, int]] = []
    seen: set[bytes] = set()
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 3 or not _OBJECT_ID.fullmatch(fields[0]):
            raise PublicDataPreflightError(
                "public preflight encountered malformed Git object inventory"
            )
        object_id, object_type, raw_size = fields
        if object_type not in {b"blob", b"commit", b"tree", b"tag"}:
            raise PublicDataPreflightError(
                "public preflight encountered unsupported Git object type"
            )
        try:
            size = int(raw_size.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicDataPreflightError(
                "public preflight encountered invalid Git object size"
            ) from exc
        object_id = object_id.lower()
        if object_id in seen or size < 0:
            raise PublicDataPreflightError(
                "public preflight encountered invalid Git object inventory"
            )
        seen.add(object_id)
        result.append((object_id, object_type, size))
    if len(result) > policy.max_tracked_files:
        raise PublicDataPreflightError(
            "public preflight Git object count limit exceeded"
        )
    return tuple(sorted(result, key=lambda item: item[0]))


def _scan_tree_payload(
    payload: bytes,
    *,
    object_id_bytes: int,
    policy: KnowledgePolicy,
) -> None:
    blocked_directories = {
        _nfkc_casefold(item) for item in policy.blocked_directory_names
    }
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset)
        nul = payload.find(b"\0", space + 1) if space >= 0 else -1
        if space <= offset or nul <= space + 1:
            raise PublicDataPreflightError(
                "public preflight encountered malformed Git tree"
            )
        mode = payload[offset:space]
        raw_name = payload[space + 1 : nul]
        object_end = nul + 1 + object_id_bytes
        if object_end > len(payload) or mode not in _TREE_MODES:
            raise PublicDataPreflightError(
                "public preflight rejected special or malformed historical tree entry"
            )
        try:
            name = raw_name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicDataPreflightError(
                "public preflight rejected non-UTF-8 historical path"
            ) from exc
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            # Tree objects are scanned without their parent path. A directory
            # component is therefore the final component here, while
            # ``_private_path`` intentionally treats its final component as a
            # file name. Check directory policy explicitly so historical
            # ``.ssh/key.txt``-style paths cannot evade PUBLIC preflight after
            # being deleted from HEAD.
            or (mode == b"40000" and _nfkc_casefold(name) in blocked_directories)
            or _private_path(name)
        ):
            raise PublicDataPreflightError(
                "public preflight rejected privacy-sensitive historical path"
            )
        offset = object_end
    if offset != len(payload):
        raise PublicDataPreflightError(
            "public preflight encountered malformed Git tree"
        )


def _scan_all_git_objects(
    repository: Path,
    *,
    policy: KnowledgePolicy,
) -> tuple[str, int, int]:
    object_format = run_git(
        repository,
        ["rev-parse", "--show-object-format"],
        max_output_bytes=16_384,
    ).stdout.strip()
    if object_format == b"sha1":
        object_id_bytes = 20
    elif object_format == b"sha256":
        object_id_bytes = 32
    else:
        raise PublicDataPreflightError(
            "public preflight encountered unsupported Git object format"
        )
    listing = _object_listing(repository, policy=policy)
    total = sum(item[2] for item in listing)
    if total > policy.max_total_bytes or any(
        size > policy.max_file_bytes for _, _, size in listing
    ):
        raise PublicDataPreflightError(
            "public preflight Git object byte limit exceeded"
        )
    digest = hashlib.sha256()
    for object_id, object_type, size in listing:
        payload = run_git(
            repository,
            [
                "cat-file",
                object_type.decode("ascii", errors="strict"),
                object_id.decode("ascii", errors="strict"),
            ],
            timeout=60,
            max_output_bytes=policy.max_file_bytes,
        ).stdout
        if len(payload) != size:
            raise PublicDataPreflightError(
                "public preflight observed unstable Git object content"
            )
        if object_type == b"tree":
            _scan_tree_payload(
                payload,
                object_id_bytes=object_id_bytes,
                policy=policy,
            )
        else:
            _decode_public_text(payload, kind="Git object")
        _update_manifest(
            digest,
            object_id,
            object_type,
            str(size).encode("ascii"),
            hashlib.sha256(payload).digest(),
        )
    if _object_listing(repository, policy=policy) != listing:
        raise PublicDataPreflightError(
            "public preflight observed a changing Git object store"
        )
    return digest.hexdigest(), len(listing), total


def build_public_data_snapshot(
    repository: Path,
    *,
    knowledge_blocked_files: int,
) -> PublicDataSnapshot:
    """Prove all worktree bytes and all linked Git data visible to Codex are public."""

    if (
        not isinstance(knowledge_blocked_files, int)
        or isinstance(knowledge_blocked_files, bool)
        or knowledge_blocked_files < 0
    ):
        raise PublicDataPreflightError(
            "Knowledge indexing reported an invalid blocked-file count"
        )
    # Knowledge indexing is a bounded retrieval projection, not the PUBLIC DLP
    # authority.  It can intentionally omit otherwise safe paths/extensions
    # such as ``src/``, ``.gitignore`` or a text fixture with a ``.bin`` suffix.
    # The checks below independently authenticate and inspect every worktree
    # byte, tracked blob, reachable/unreachable Git object and protected Git
    # metadata byte before Codex.  Preserve the Knowledge count in the bound
    # snapshot for TOCTOU equality, but do not confuse retrieval omission with
    # uninspected cloud egress.
    try:
        validate_coding_git_config(repository)
        validate_git_scope(repository)
    except (RepositoryError, RuntimeError) as exc:
        raise PublicDataPreflightError(
            "public preflight rejected unsafe Git metadata scope"
        ) from exc
    policy = load_knowledge_policy()
    metadata_manifest, metadata_bytes = _scan_git_metadata(
        repository,
        policy=policy,
    )
    object_manifest, object_count, object_bytes = _scan_all_git_objects(
        repository,
        policy=policy,
    )
    head = (
        run_git(repository, ["rev-parse", "--verify", "HEAD"], max_output_bytes=16_384)
        .stdout.decode("ascii", errors="strict")
        .strip()
        .casefold()
    )
    listing = run_git(
        repository,
        ["ls-tree", "-r", "-z", "--full-tree", "HEAD"],
        max_output_bytes=policy.max_git_output_bytes,
    ).stdout
    records = [item for item in listing.split(b"\0") if item]
    if len(records) > policy.max_tracked_files:
        raise PublicDataPreflightError("public preflight tracked-file limit exceeded")
    digest = hashlib.sha256()
    total = 0
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_object = metadata.split()
            relative = raw_path.decode("utf-8", errors="strict").replace("\\", "/")
            object_id = raw_object.decode("ascii", errors="strict").casefold()
        except (ValueError, UnicodeDecodeError) as exc:
            raise PublicDataPreflightError(
                "public preflight encountered invalid tracked metadata"
            ) from exc
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or _private_path(relative)
        ):
            raise PublicDataPreflightError(
                "public preflight rejected a privacy-sensitive tracked path"
            )
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise PublicDataPreflightError(
                "public preflight rejects symlink, submodule, or special tracked entries"
            )
        size_raw = run_git(
            repository,
            ["cat-file", "-s", object_id],
            max_output_bytes=16_384,
        ).stdout
        try:
            size = int(size_raw.decode("ascii", errors="strict").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicDataPreflightError("tracked blob size is invalid") from exc
        if size < 0 or size > policy.max_file_bytes:
            raise PublicDataPreflightError("public tracked blob exceeds file limit")
        total += size
        if total > policy.max_total_bytes:
            raise PublicDataPreflightError("public tracked blobs exceed total limit")
        payload = run_git(
            repository,
            ["cat-file", "blob", object_id],
            max_output_bytes=policy.max_file_bytes,
        ).stdout
        if len(payload) != size:
            raise PublicDataPreflightError(
                "public preflight rejected unstable tracked content"
            )
        _decode_public_text(payload, kind="tracked content")
        for value in (raw_path, mode, raw_object, hashlib.sha256(payload).digest()):
            _update_manifest(digest, value)
    changed = scan_changed_content(repository, max_bytes=policy.max_total_bytes)
    return PublicDataSnapshot(
        head_sha=head,
        tracked_manifest_sha256=digest.hexdigest(),
        changed_manifest_sha256=changed,
        git_object_manifest_sha256=object_manifest,
        git_metadata_manifest_sha256=metadata_manifest,
        tracked_files=len(records),
        tracked_bytes=total,
        git_objects=object_count,
        git_object_bytes=object_bytes,
        git_metadata_bytes=metadata_bytes,
        knowledge_blocked_files=knowledge_blocked_files,
    )


__all__ = [
    "PublicDataPreflightError",
    "PublicDataSnapshot",
    "build_public_data_snapshot",
]
