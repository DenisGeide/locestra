from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from services.common import RUN_DIR
from services.knowledge.config import load_knowledge_policy
from services.knowledge.privacy import KnowledgePolicyError, canonical_project
from services.knowledge.privacy import detect_secret
from services.knowledge.repository import RepositoryError, validate_git_scope


class CodingRepositoryError(RuntimeError):
    """A requested repository or Git operation violated the coding boundary."""


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    canonical_root: Path
    base_commit: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    dirty_fingerprint: str
    git_metadata_fingerprint: str
    excluded_git_refs: tuple[str, ...] = ()


_GIT_EXECUTABLE = Path(shutil.which("git.exe") or shutil.which("git") or "")
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SAFE_LOCAL_CONFIG = {
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
_REMOTE_CONFIG = re.compile(
    r"^remote\.(?P<name>[a-z0-9][a-z0-9._-]{0,127})\.(?P<field>url|fetch)$"
)
_BRANCH_CONFIG = re.compile(
    r"^branch\.(?P<name>[a-z0-9][a-z0-9._/-]{0,255})\.(?P<field>remote|merge)$"
)


def git_environment() -> dict[str, str]:
    # Git never needs application credentials.  Keep the same narrow process
    # boundary as coding executors so a child crash or diagnostic cannot dump
    # the gateway/Telegram environment.
    safe_keys = {
        "ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH",
        "LOCALAPPDATA", "OS", "PATH", "PATHEXT", "PROGRAMDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE", "SYSTEMROOT",
        "TEMP", "TMP", "USERPROFILE", "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in safe_keys
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "NO_COLOR": "1",
        }
    )
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_PROXY_COMMAND",
    ):
        environment.pop(key, None)
    for key in tuple(environment):
        if re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", key) or key.startswith("GIT_TRACE"):
            environment.pop(key, None)
    return environment


def _trusted_git() -> str:
    if not _GIT_EXECUTABLE.is_file():
        raise CodingRepositoryError("trusted Git executable is unavailable")
    return str(_GIT_EXECUTABLE)


def run_git(
    repository: Path,
    arguments: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    max_output_bytes: int = 8 * 1024 * 1024,
    mutation: bool = False,
    isolated_config: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    hooks = RUN_DIR / "coding" / "empty-git-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    command = [
        _trusted_git(),
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "credential.helper=",
        "-c",
        "diff.external=",
        *arguments,
    ]
    environment = git_environment()
    if isolated_config:
        # Parsing one explicitly named config file must not recursively load
        # repository include/includeIf directives or any ambient config. The
        # caller still receives the requested ``--file`` bytes through Git's
        # canonical parser, but startup configuration is an empty regular
        # source.
        environment["GIT_CONFIG"] = os.devnull
    if mutation:
        environment["GIT_OPTIONAL_LOCKS"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodingRepositoryError("bounded Git invocation failed") from exc
    if len(completed.stdout) > max_output_bytes or len(completed.stderr) > max_output_bytes:
        raise CodingRepositoryError("Git output exceeded the coding policy limit")
    if check and completed.returncode != 0:
        raise CodingRepositoryError(f"Git operation failed with exit code {completed.returncode}")
    return completed


def _exact_git_directories(repository: Path) -> tuple[Path, Path]:
    git_raw = run_git(
        repository,
        ["rev-parse", "--absolute-git-dir"],
        timeout=30,
        max_output_bytes=16_384,
        isolated_config=True,
    ).stdout
    common_raw = run_git(
        repository,
        ["rev-parse", "--git-common-dir"],
        timeout=30,
        max_output_bytes=16_384,
        isolated_config=True,
    ).stdout
    git_text = _decode_scalar(git_raw, label="Git directory")
    common_text = _decode_scalar(common_raw, label="Git common directory")
    common_candidate = Path(common_text)
    try:
        git_dir = Path(git_text).resolve(strict=True)
        common_dir = (
            common_candidate.resolve(strict=True)
            if common_candidate.is_absolute()
            else (repository / common_candidate).resolve(strict=True)
        )
    except OSError as exc:
        raise CodingRepositoryError("Git metadata directory is unavailable") from exc
    return git_dir, common_dir


def _safe_config_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CodingRepositoryError("local Git config is unavailable") from exc
    is_junction = getattr(path, "is_junction", lambda: False)
    if (
        path.is_symlink()
        or is_junction()
        or getattr(before, "st_reparse_tag", 0)
        or getattr(before, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_nlink", 1) != 1
        or before.st_size > 8 * 1024 * 1024
    ):
        raise CodingRepositoryError("local Git config is unsafe")
    try:
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise CodingRepositoryError("local Git config is unreadable") from exc
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
        or len(payload) != after.st_size
        or path.is_symlink()
        or is_junction()
        or getattr(after, "st_reparse_tag", 0)
        or getattr(after, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise CodingRepositoryError("local Git config changed during validation")
    return payload


def _safe_standard_remote(value: str) -> bool:
    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value.casefold().startswith("ext::")
    ):
        return False
    if re.fullmatch(
        r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~+/@:-]+",
        value,
    ):
        return True
    # A path remote is data, not a transport-helper name. It remains useful
    # for local/offline repositories and is not available to PUBLIC Codex,
    # whose separate gate accepts HTTPS only.
    if "://" not in value:
        return "::" not in value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    scheme = parsed.scheme.casefold()
    if parsed.query or parsed.fragment or not parsed.path:
        return False
    if scheme == "file":
        return parsed.username is None and parsed.password is None
    if scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
        return False
    if parsed.password is not None:
        return False
    if scheme in {"http", "https", "git"} and parsed.username is not None:
        return False
    if scheme == "ssh" and parsed.username is not None and not re.fullmatch(
        r"[A-Za-z0-9._-]+", parsed.username
    ):
        return False
    return port is None or 0 < port <= 65535


def _safe_fetch_refspec(value: str, *, remote: str) -> bool:
    if not value.isascii() or value.count(":") != 1:
        return False
    source, destination = value.removeprefix("+").split(":", 1)
    return bool(
        source.startswith("refs/heads/")
        and destination.startswith(f"refs/remotes/{remote}/")
        and source.count("*") == destination.count("*")
        and source.count("*") <= 1
        and not any(token in value for token in ("..", "@{", "\\", " ", "\t", "\r", "\n"))
        and not any(character in value for character in "~^?[]")
    )


def _validate_config_value(key: str, value: str) -> None:
    folded = key.casefold()
    if (
        folded.startswith("filter.")
        or folded == "core.attributesfile"
        or re.fullmatch(r"diff\..+\.(?:command|textconv)", folded)
        or re.fullmatch(r"merge\..+\.driver", folded)
    ):
        raise CodingRepositoryError(
            "repository-local Git command/filter drivers are unsupported"
        )
    if folded in _SAFE_LOCAL_CONFIG:
        if value.casefold() not in _SAFE_LOCAL_CONFIG[folded]:
            raise CodingRepositoryError("local Git config value is unsupported")
        return
    if folded in {"user.name", "user.email"}:
        if (
            not value
            or len(value) > 1_024
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise CodingRepositoryError("local Git identity is malformed")
        return
    remote_match = _REMOTE_CONFIG.fullmatch(folded)
    if remote_match is not None:
        remote = remote_match.group("name")
        valid = (
            _safe_standard_remote(value)
            if remote_match.group("field") == "url"
            else _safe_fetch_refspec(value, remote=remote)
        )
        if not valid:
            raise CodingRepositoryError("local Git remote config is unsupported")
        return
    branch_match = _BRANCH_CONFIG.fullmatch(folded)
    if branch_match is not None:
        field = branch_match.group("field")
        if field == "remote":
            valid = value == "." or bool(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            )
        else:
            valid = bool(
                re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}", value)
            ) and not any(token in value for token in ("..", "@{", "//"))
        if valid:
            return
        raise CodingRepositoryError("local Git branch config is unsupported")
    raise CodingRepositoryError("local Git config key is not allowlisted")


def _validate_config_file(repository: Path, path: Path) -> bytes:
    payload = _safe_config_file(path)
    result = run_git(
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
    )
    seen_singletons: set[str] = set()
    for record in (item for item in result.stdout.split(b"\0") if item):
        if b"\n" not in record:
            raise CodingRepositoryError("local Git config record is malformed")
        raw_key, raw_value = record.split(b"\n", 1)
        try:
            key = raw_key.decode("utf-8", errors="strict")
            value = raw_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CodingRepositoryError("local Git config is not UTF-8") from exc
        if (
            not key
            or not key.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
        ):
            raise CodingRepositoryError("local Git config key is malformed")
        _validate_config_value(key, value)
        folded = key.casefold()
        if not folded.endswith(".fetch") and folded in seen_singletons:
            raise CodingRepositoryError("duplicate local Git config is unsupported")
        seen_singletons.add(folded)
    if _safe_config_file(path) != payload:
        raise CodingRepositoryError("local Git config changed during validation")
    return payload


def _validated_config_payloads(
    repository: Path,
) -> tuple[Path, Path, tuple[tuple[str, bytes], ...]]:
    git_dir, common_dir = _exact_git_directories(repository)
    config = common_dir / "config"
    payloads = [("common/config", _validate_config_file(repository, config))]
    worktree_config = git_dir / "config.worktree"
    if os.path.lexists(worktree_config) and worktree_config != config:
        payloads.append(
            (
                "git/config.worktree",
                _validate_config_file(repository, worktree_config),
            )
        )
    return git_dir, common_dir, tuple(payloads)


def validate_coding_git_config(repository: Path) -> None:
    """Admit only inert local/worktree Git config before any model executes."""

    _, common_dir, _ = _validated_config_payloads(repository)

    grafts = common_dir / "info" / "grafts"
    if os.path.lexists(grafts):
        raise CodingRepositoryError("legacy Git grafts are unsupported")
    info_attributes = common_dir / "info" / "attributes"
    if os.path.lexists(info_attributes):
        raise CodingRepositoryError("Git info attributes are unsupported")
    replacements = run_git(
        repository,
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
        timeout=30,
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    if replacements.strip():
        raise CodingRepositoryError("Git replacement refs are unsupported")


def _decode_scalar(value: bytes, *, label: str) -> str:
    try:
        decoded = value.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CodingRepositoryError(f"{label} is not valid UTF-8") from exc
    if not decoded or "\x00" in decoded or "\r" in decoded or "\n" in decoded:
        raise CodingRepositoryError(f"{label} is malformed")
    return decoded


def _decode_path(value: bytes, *, label: str) -> str:
    """Decode one exact ``-z`` path without whitespace normalization."""

    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CodingRepositoryError(f"{label} is not valid UTF-8") from exc
    if (
        not decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        raise CodingRepositoryError(f"{label} is malformed")
    return decoded


def _porcelain_paths(repository: Path) -> tuple[str, ...]:
    validate_coding_git_config(repository)
    output = run_git(
        repository,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout=30,
    ).stdout
    paths: list[str] = []
    records = output.split(b"\x00")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise CodingRepositoryError("Git status returned a malformed record")
        # In porcelain v1 ``-z`` output a rename/copy record is encoded as
        # ``XY destination\0source\0``. Both paths are security-relevant: the
        # destination may escape the declared/allowed scope while the source
        # remains allowed.
        candidate = _decode_path(record[3:], label="Git status destination path")
        if candidate not in paths:
            paths.append(candidate)
        if b"R" in record[:2] or b"C" in record[:2]:
            if index >= len(records) or not records[index]:
                raise CodingRepositoryError(
                    "Git status returned a malformed rename/copy record"
                )
            source = _decode_path(
                records[index], label="Git status rename/copy source path"
            )
            index += 1
            if source not in paths:
                paths.append(source)
        if len(paths) > 10_000:
            raise CodingRepositoryError("dirty path inventory exceeds limit")
    return tuple(paths)


def git_metadata_fingerprint(
    repository: Path,
    *,
    excluded_refs: tuple[str, ...] = (),
) -> str:
    """Bind shared Git configuration and every protected repository ref.

    The exact branch checked out by this worktree is already bound through its
    HEAD. Callers may additionally exclude only exact refs whose ownership was
    proven by the durable worktree registry. Prefix-wide exclusions are unsafe:
    a user branch may legitimately share the platform naming namespace.
    """

    validate_coding_git_config(repository)
    if len(set(excluded_refs)) != len(excluded_refs) or any(
        not item.startswith("refs/")
        or "\x00" in item
        or "\r" in item
        or "\n" in item
        for item in excluded_refs
    ):
        raise CodingRepositoryError("invalid exact Git ref exclusion")
    digest = sha256()
    _, _, config_payloads = _validated_config_payloads(repository)
    current_ref_result = run_git(
        repository,
        ["symbolic-ref", "--quiet", "HEAD"],
        timeout=30,
        check=False,
        max_output_bytes=16_384,
    )
    current_ref = (
        current_ref_result.stdout.strip()
        if current_ref_result.returncode == 0
        else b""
    )
    encoded_exclusions = {item.encode("utf-8") for item in excluded_refs}
    all_refs = run_git(
        repository,
        [
            "for-each-ref",
            "--sort=refname",
            "--format=%(refname)%00%(objectname)%00%(symref)",
            "refs/heads",
            "refs/tags",
            "refs/remotes",
            "refs/replace",
        ],
        timeout=30,
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    protected_ref_records: list[bytes] = []
    for record in all_refs.splitlines():
        refname = record.split(b"\x00", 1)[0]
        if refname == current_ref or refname in encoded_exclusions:
            continue
        protected_ref_records.append(record)
    digest.update(b"git-local-config\0")
    for label, payload in config_payloads:
        encoded_label = label.encode("utf-8", errors="strict")
        digest.update(len(encoded_label).to_bytes(8, "big"))
        digest.update(encoded_label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    digest.update(b"git-protected-refs\0")
    for record in protected_ref_records:
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()


def resolve_repository(
    requested_path: str,
    *,
    excluded_refs: tuple[str, ...] = (),
) -> RepositoryIdentity:
    if not requested_path or "\x00" in requested_path:
        raise CodingRepositoryError("explicit repository path is invalid")
    raw = Path(requested_path)
    if not raw.is_absolute() or not raw.is_dir():
        raise CodingRepositoryError("explicit repository path does not exist")
    try:
        requested = raw.resolve(strict=True)
    except OSError as exc:
        raise CodingRepositoryError("explicit repository path cannot be resolved") from exc
    # Reject local/worktree command configuration before even asking Git for
    # a config-influenced top level. The second validation at the canonical
    # root closes subdirectory and metadata-race ambiguity.
    validate_coding_git_config(requested)
    top = run_git(
        requested,
        ["rev-parse", "--show-toplevel"],
        max_output_bytes=16_384,
    ).stdout
    root_text = _decode_scalar(top, label="Git top-level")
    try:
        root = canonical_project(root_text)
        validate_git_scope(root)
    except (KnowledgePolicyError, RepositoryError, OSError) as exc:
        raise CodingRepositoryError("repository scope failed canonical Git validation") from exc
    try:
        requested.relative_to(root)
    except ValueError as exc:
        raise CodingRepositoryError("explicit path is outside its Git top-level") from exc
    validate_coding_git_config(root)
    commit = _decode_scalar(
        run_git(root, ["rev-parse", "--verify", "HEAD"], max_output_bytes=16_384).stdout,
        label="Git HEAD",
    ).casefold()
    if not _OBJECT_ID.fullmatch(commit):
        raise CodingRepositoryError("repository HEAD is not a full object ID")
    branch_result = run_git(
        root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        max_output_bytes=16_384,
    )
    branch = (
        _decode_scalar(branch_result.stdout, label="Git branch")
        if branch_result.returncode == 0
        else None
    )
    dirty_paths = _porcelain_paths(root)
    # Bind source identity to ignored paths and metadata too.  Otherwise an
    # executor/reviewer could alter a pre-existing ignored artifact while the
    # user's source repository still appears clean to Git.
    metadata_fingerprint = git_metadata_fingerprint(
        root,
        excluded_refs=excluded_refs,
    )
    dirty_fingerprint = worktree_fingerprint(
        root,
        dirty_paths=dirty_paths,
        include_ignored=True,
        metadata_fingerprint=metadata_fingerprint,
        excluded_refs=excluded_refs,
    )
    return RepositoryIdentity(
        canonical_root=root,
        base_commit=commit,
        branch=branch,
        dirty_paths=dirty_paths,
        dirty_fingerprint=dirty_fingerprint,
        git_metadata_fingerprint=metadata_fingerprint,
        excluded_git_refs=excluded_refs,
    )


def git_status_paths(repository: Path) -> list[str]:
    return list(_porcelain_paths(repository))


def git_ignored_paths(repository: Path) -> tuple[str, ...]:
    validate_coding_git_config(repository)
    output = run_git(
        repository,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        timeout=60,
        max_output_bytes=64 * 1024 * 1024,
    ).stdout
    paths: list[str] = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        path = _decode_path(raw_path, label="ignored Git path")
        if path not in paths:
            paths.append(path)
        if len(paths) > 10_000:
            raise CodingRepositoryError("ignored path inventory exceeds limit")
    return tuple(paths)


def _changed_path_is_private(relative: str) -> bool:
    policy = load_knowledge_policy()
    parts = PurePath(relative).parts
    folded = tuple(part.casefold() for part in parts)
    blocked_dirs = {item.casefold() for item in policy.blocked_directory_names}
    blocked_files = {item.casefold() for item in policy.blocked_file_names}
    name = folded[-1]
    return (
        any(part in blocked_dirs for part in folded[:-1])
        or name in blocked_files
        or name.startswith(".env")
        or any(name.endswith(item.casefold()) for item in policy.blocked_file_suffixes)
        or detect_secret(relative.encode("utf-8", errors="strict")) is not None
    )


def _read_changed_file(
    repository: Path, relative: str, *, maximum: int
) -> tuple[bytes, str]:
    candidate = repository / relative
    try:
        absolute = candidate.absolute()
        canonical = candidate.resolve(strict=True)
        canonical.relative_to(repository.resolve(strict=True))
        if os.path.normcase(str(absolute)) != os.path.normcase(str(canonical)):
            raise CodingRepositoryError("changed file uses filesystem indirection")
        before = candidate.lstat()
    except (OSError, ValueError) as exc:
        raise CodingRepositoryError("changed file escapes or is unavailable") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        candidate.is_symlink()
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_nlink", 1) > 1
    ):
        raise CodingRepositoryError("changed file is not an independent regular file")
    if before.st_size > maximum:
        raise CodingRepositoryError("changed content exceeds policy limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CodingRepositoryError("changed file could not be read safely") from exc
    identities = (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns),
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
    )
    if identities[0] != identities[1] or identities[1] != identities[2] or len(payload) != after.st_size:
        raise CodingRepositoryError("changed file changed while it was scanned")
    if len(payload) > maximum:
        raise CodingRepositoryError("changed content exceeds policy limit")
    mode = "100755" if before.st_mode & 0o111 else "100644"
    return payload, mode


def _read_tree_blob(
    repository: Path,
    treeish: str,
    relative: str,
    *,
    maximum: int,
) -> tuple[bytes, str]:
    record = run_git(
        repository,
        ["ls-tree", "-z", treeish, "--", relative],
        max_output_bytes=16_384,
    ).stdout
    fields = record.split(b"\t", 1)[0].split()
    if len(fields) != 3 or fields[1] != b"blob" or not _OBJECT_ID.fullmatch(
        fields[2].decode("ascii", errors="strict")
    ):
        raise CodingRepositoryError("deleted file preimage is not a bounded blob")
    object_id = fields[2].decode("ascii")
    size_raw = run_git(
        repository,
        ["cat-file", "-s", object_id],
        max_output_bytes=16_384,
    ).stdout
    try:
        size = int(_decode_scalar(size_raw, label="deleted blob size"))
    except ValueError as exc:
        raise CodingRepositoryError("deleted blob size is invalid") from exc
    if size < 0 or size > maximum:
        raise CodingRepositoryError("deleted content exceeds policy limit")
    payload = run_git(
        repository,
        ["cat-file", "blob", object_id],
        max_output_bytes=maximum,
    ).stdout
    if len(payload) != size:
        raise CodingRepositoryError("deleted blob changed while it was scanned")
    return payload, fields[0].decode("ascii", errors="strict")


def _read_deleted_preimage(
    repository: Path, relative: str, *, maximum: int
) -> tuple[bytes, str]:
    return _read_tree_blob(
        repository,
        "HEAD",
        relative,
        maximum=maximum,
    )


def _update_changed_manifest(
    digest,
    *,
    relative: str,
    state: str,
    mode: str,
    payload: bytes,
) -> None:
    encoded = relative.encode("utf-8", errors="strict")
    for value in (encoded, state.encode("ascii"), mode.encode("ascii")):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(sha256(payload).digest())


def scan_changed_content(
    repository: Path,
    *,
    max_bytes: int,
    max_files: int = 10_000,
) -> str:
    """Scan exact changed bytes independently of diff attributes/rendering."""

    validate_coding_git_config(repository)
    tracked = run_git(
        repository,
        ["diff", "--name-only", "-z", "--no-renames", "HEAD", "--"],
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    untracked = run_git(
        repository,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    paths: list[str] = []
    for raw in (*tracked.split(b"\0"), *untracked.split(b"\0")):
        if not raw:
            continue
        relative = _decode_path(raw, label="changed content path")
        path = PurePath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise CodingRepositoryError("changed content path is unsafe")
        normalized = relative.replace("\\", "/")
        if normalized not in paths:
            paths.append(normalized)
        if len(paths) > max_files:
            raise CodingRepositoryError("changed content file inventory exceeds limit")
    digest = sha256()
    remaining = max_bytes
    for relative in sorted(paths, key=str.casefold):
        if _changed_path_is_private(relative):
            raise CodingRepositoryError("changed content path is privacy-sensitive")
        attribute = run_git(
            repository,
            ["check-attr", "-z", "diff", "--", relative],
            max_output_bytes=16_384,
        ).stdout.split(b"\0")
        if (
            len(attribute) < 4
            or attribute[0].decode("utf-8", errors="strict").replace("\\", "/")
            != relative
            or attribute[1] != b"diff"
            or attribute[2] not in {b"unspecified", b"set"}
        ):
            raise CodingRepositoryError(
                "changed content has an unreviewable Git diff attribute"
            )
        candidate = repository / relative
        exists = os.path.lexists(candidate)
        payload, mode = (
            _read_changed_file(repository, relative, maximum=remaining)
            if exists
            else _read_deleted_preimage(repository, relative, maximum=remaining)
        )
        if b"\0" in payload:
            raise CodingRepositoryError("changed binary content is not independently reviewable")
        try:
            payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CodingRepositoryError(
                "changed non-UTF-8 content is not independently reviewable"
            ) from exc
        finding = detect_secret(payload)
        if finding is not None:
            raise CodingRepositoryError(f"changed content matched privacy rule {finding}")
        remaining -= len(payload)
        if remaining < 0:
            raise CodingRepositoryError("changed content exceeds policy limit")
        _update_changed_manifest(
            digest,
            relative=relative,
            state="present" if exists else "deleted",
            mode=mode,
            payload=payload,
        )
    return digest.hexdigest()


def scan_commit_changed_content(
    repository: Path,
    *,
    old_commit: str,
    new_commit: str,
    max_bytes: int,
    max_files: int = 10_000,
) -> str:
    """Recompute the exact changed path/mode/blob manifest from commit trees."""

    raw_paths = run_git(
        repository,
        ["diff", "--name-only", "-z", "--no-renames", old_commit, new_commit, "--"],
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    paths = [
        _decode_path(raw, label="commit changed content path").replace("\\", "/")
        for raw in raw_paths.split(b"\0")
        if raw
    ]
    if len(paths) > max_files or len(set(paths)) != len(paths):
        raise CodingRepositoryError("commit changed content inventory is invalid")
    digest = sha256()
    remaining = max_bytes
    for relative in sorted(paths, key=str.casefold):
        new_record = run_git(
            repository,
            ["ls-tree", "-z", new_commit, "--", relative],
            max_output_bytes=16_384,
        ).stdout
        if new_record:
            payload, mode = _read_tree_blob(
                repository,
                new_commit,
                relative,
                maximum=remaining,
            )
            state = "present"
        else:
            payload, mode = _read_tree_blob(
                repository,
                old_commit,
                relative,
                maximum=remaining,
            )
            state = "deleted"
        remaining -= len(payload)
        if remaining < 0:
            raise CodingRepositoryError("commit changed content exceeds policy limit")
        _update_changed_manifest(
            digest,
            relative=relative,
            state=state,
            mode=mode,
            payload=payload,
        )
    return digest.hexdigest()


def worktree_fingerprint(
    repository: Path,
    *,
    dirty_paths: tuple[str, ...] | None = None,
    include_ignored: bool = False,
    metadata_fingerprint: str | None = None,
    excluded_refs: tuple[str, ...] = (),
) -> str:
    """Fingerprint tracked diffs and dirty-file observations without exposing content."""

    paths = dirty_paths if dirty_paths is not None else _porcelain_paths(repository)
    digest = sha256()
    digest.update(b"git-metadata\0")
    digest.update(
        (
            metadata_fingerprint
            or git_metadata_fingerprint(repository, excluded_refs=excluded_refs)
        ).encode("ascii")
    )
    # A clean commit is still a mutation.  Bind the fingerprint to HEAD so a
    # read-only executor cannot hide file changes by committing them before
    # the post-execution comparison.
    digest.update(
        run_git(
            repository,
            ["rev-parse", "--verify", "HEAD"],
            timeout=30,
            max_output_bytes=16_384,
        ).stdout
    )
    status = run_git(
        repository,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout=30,
    ).stdout
    digest.update(status)
    if include_ignored:
        ignored = run_git(
            repository,
            ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            timeout=60,
            max_output_bytes=64 * 1024 * 1024,
        ).stdout
        digest.update(b"ignored\0")
        digest.update(ignored)
        # Hash metadata for ignored files as well as the path set.  This keeps
        # the check practical for large dependency trees while detecting new,
        # removed, resized, or normally modified ignored artifacts.  The OS
        # read-only sandbox remains the primary write barrier.
        for raw_relative in ignored.split(b"\0"):
            if not raw_relative:
                continue
            relative = os.fsdecode(raw_relative)
            candidate = repository / relative
            try:
                info = candidate.lstat()
                link_target = os.readlink(candidate) if candidate.is_symlink() else ""
                digest.update(
                    f"{relative}\0{info.st_mode}\0{info.st_size}\0{info.st_mtime_ns}\0{link_target}\0".encode(
                        "utf-8", errors="surrogateescape"
                    )
                )
            except OSError:
                digest.update(f"missing-ignored:{relative}\0".encode("utf-8", errors="surrogateescape"))
    for arguments in (
        ["diff", "--binary", "--no-ext-diff", "--no-color"],
        ["diff", "--cached", "--binary", "--no-ext-diff", "--no-color"],
    ):
        digest.update(run_git(repository, arguments, max_output_bytes=64 * 1024 * 1024).stdout)
    for relative in paths:
        candidate = repository / relative
        try:
            canonical = candidate.resolve(strict=True)
            canonical.relative_to(repository.resolve(strict=True))
            info = canonical.stat()
        except (OSError, ValueError):
            digest.update(f"missing:{relative}\0".encode("utf-8"))
            continue
        digest.update(
            f"{relative}\0{info.st_size}\0{info.st_mtime_ns}\0".encode("utf-8")
        )
        if candidate.is_file() and not candidate.is_symlink() and info.st_size <= 16 * 1024 * 1024:
            try:
                with candidate.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                digest.update(b"unreadable")
    return digest.hexdigest()


def git_diff(repository: Path, *, max_bytes: int) -> bytes:
    validate_coding_git_config(repository)
    tracked = run_git(
        repository,
        [
            "diff",
            "--binary",
            "--text",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "HEAD",
            "--",
        ],
        max_output_bytes=max_bytes,
    ).stdout
    untracked_raw = run_git(
        repository,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        max_output_bytes=max_bytes,
    ).stdout
    chunks = [tracked] if tracked else []
    for raw_path in untracked_raw.split(b"\x00"):
        if not raw_path:
            continue
        relative = _decode_path(raw_path, label="untracked Git path")
        candidate = (repository / relative).resolve(strict=True)
        try:
            candidate.relative_to(repository.resolve(strict=True))
        except ValueError as exc:
            raise CodingRepositoryError("untracked path escapes repository") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise CodingRepositoryError("untracked diff source must be a regular file")
        remaining = max_bytes - sum(len(item) for item in chunks)
        if remaining <= 0:
            raise CodingRepositoryError("combined diff exceeds policy limit")
        addition = run_git(
            repository,
            [
                "diff", "--no-index", "--binary", "--text", "--no-ext-diff", "--no-textconv",
                "--no-color", "--", os.devnull, relative,
            ],
            check=False,
            max_output_bytes=remaining,
        )
        if addition.returncode not in {0, 1}:
            raise CodingRepositoryError("failed to render untracked file diff")
        if addition.stdout:
            chunks.append(addition.stdout)
    # Every Git patch chunk is already newline-terminated.  Concatenating
    # without an injected separator keeps this byte-for-byte equivalent to
    # ``git diff --cached HEAD`` after the same files are staged.
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise CodingRepositoryError("combined diff exceeds policy limit")
    return payload


def applicable_agent_rules(repository: Path, target_paths: list[str] | None = None) -> list[Path]:
    targets = target_paths or [""]
    selected: set[Path] = set()
    root_rule = repository / "AGENTS.md"
    if is_regular_repository_file(repository, root_rule):
        selected.add(root_rule)
    for target in targets:
        candidate = repository / target
        cursor = candidate if candidate.is_dir() else candidate.parent
        try:
            cursor.relative_to(repository)
        except ValueError:
            continue
        while cursor != repository:
            rule = cursor / "AGENTS.md"
            if is_regular_repository_file(repository, rule):
                selected.add(rule)
            cursor = cursor.parent
    return sorted(selected, key=lambda item: (len(item.relative_to(repository).parts), item.as_posix().casefold()))


def is_regular_repository_file(repository: Path, path: Path) -> bool:
    """Return true only for a non-linked regular file inside ``repository``."""

    try:
        root = repository.resolve(strict=True)
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            path.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) > 1
        ):
            return False
        path.resolve(strict=True).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def ensure_regular_owned_file(path: Path) -> None:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise CodingRepositoryError("owned metadata cannot be a reparse point")
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
        raise CodingRepositoryError("owned metadata must be a regular non-hardlinked file")
