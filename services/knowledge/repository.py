from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from services.knowledge.config import KnowledgePolicy
from services.knowledge.contracts import (
    BlockedRepositorySourceV1,
    RepositoryFileV1,
    RepositoryMapV1,
    SourceKind,
)
from services.knowledge.parsers import ParsedFragment, extract_facts, parse_source
from services.knowledge.privacy import (
    KnowledgePolicyError,
    ReadSource,
    read_registered_source,
    preflight_registered_source_size,
    registered_source_observation,
    reject_secret_text,
)


_GIT_EXECUTABLE = Path(shutil.which("git") or "").resolve()
_RG_EXECUTABLE = Path(shutil.which("rg") or "").resolve()


def tracked_source_size(project: Path, entry: TrackedFile, policy: KnowledgePolicy) -> int:
    return preflight_registered_source_size(
        project,
        entry.path,
        policy,
        repository_tracked=True,
    )


def tracked_source_observation(
    project: Path,
    entry: TrackedFile,
    policy: KnowledgePolicy,
) -> tuple[int, int]:
    return registered_source_observation(
        project,
        entry.path,
        policy,
        repository_tracked=True,
    )


class RepositoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrackedFile:
    path: str
    git_mode: str
    git_object: str
    head_object: str | None


@dataclass(frozen=True, slots=True)
class PreparedSource:
    read: ReadSource
    source_uri: str
    source_kind: SourceKind
    source_hash: str
    parser: str
    fragments: tuple[ParsedFragment, ...]
    facts_by_ordinal: dict[int, tuple]
    file: RepositoryFileV1
    reused: bool = False


_LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".html": "HTML",
    ".go": "Go",
    ".dart": "Dart",
    ".erl": "Erlang",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".fs": "F#",
    ".fsx": "F#",
    ".gql": "GraphQL",
    ".graphql": "GraphQL",
    ".h": "C/C++ Header",
    ".hh": "C++ Header",
    ".hpp": "C++ Header",
    ".hrl": "Erlang Header",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".lua": "Lua",
    ".md": "Markdown",
    ".mjs": "JavaScript",
    ".php": "PHP",
    ".proto": "Protocol Buffers",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".txt": "Text",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".yaml": "YAML",
    ".yml": "YAML",
}
_MANIFEST_NAMES = {
    "cmakelists.txt",
    "composer.json",
    "docker-compose.yml",
    "compose.yml",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "cargo.toml",
    "go.mod",
    "gemfile",
    "makefile",
}
_ENTRY_NAMES = {
    "__main__.py",
    "app.py",
    "main.py",
    "server.py",
    "index.js",
    "index.ts",
    "main.js",
    "main.ts",
}


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
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
    ):
        environment.pop(key, None)
    for key in tuple(environment):
        if (
            re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", key)
            or key.startswith("GIT_TRACE")
            or key in {
                "GIT_REDIRECT_STDERR", "GIT_PAGER", "GIT_ASKPASS", "SSH_ASKPASS",
                "GIT_SSH", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND",
            }
        ):
            environment.pop(key, None)
    return environment


def _git(
    project: Path,
    arguments: list[str],
    *,
    timeout: int = 15,
    max_output_bytes: int = 16_777_216,
) -> bytes:
    command = [
        str(_GIT_EXECUTABLE),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=",
        *arguments,
    ]
    try:
        if not _GIT_EXECUTABLE.is_file():
            raise RepositoryError("trusted Git executable is unavailable")
        try:
            _GIT_EXECUTABLE.relative_to(project)
        except ValueError:
            pass
        else:
            raise RepositoryError("Git executable may not come from the registered project")
        process = subprocess.Popen(
            command,
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
        )
        assert process.stdout is not None
        captured: list[bytes] = []

        def read_stdout() -> None:
            captured.append(process.stdout.read(max_output_bytes + 1))

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            process.kill()
            process.wait(timeout=5)
            raise RepositoryError("git invocation timed out")
        output = captured[0] if captured else b""
        if len(output) > max_output_bytes:
            process.kill()
            process.wait(timeout=5)
            raise RepositoryError("git output exceeds configured limit")
        return_code = process.wait(timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryError("git invocation failed") from exc
    if return_code != 0:
        raise RepositoryError("project is not a readable Git repository")
    return output


def git_commit(project: Path) -> str | None:
    try:
        value = _git(project, ["rev-parse", "--verify", "HEAD"]).decode("ascii", errors="strict").strip()
    except (RepositoryError, UnicodeDecodeError):
        return None
    return value if re.fullmatch(r"[a-fA-F0-9]{40,64}", value) else None


@dataclass(frozen=True, slots=True)
class GitScope:
    git_dir: Path
    common_dir: Path
    linked_worktree: bool


def _metadata_path_is_indirect(path: Path, info: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda value: False)
    return bool(
        path.is_symlink()
        or is_junction(path)
        or getattr(info, "st_reparse_tag", 0)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _metadata_entry_is_safe(path: Path, *, regular_file: bool | None = None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RepositoryError("Git metadata is unreadable") from exc
    if _metadata_path_is_indirect(path, info):
        raise RepositoryError("reparse Git metadata is not supported")
    if regular_file is True and not stat.S_ISREG(info.st_mode):
        raise RepositoryError("Git metadata file has an invalid type")
    if regular_file is False and not stat.S_ISDIR(info.st_mode):
        raise RepositoryError("Git metadata directory has an invalid type")
    if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1:
        raise RepositoryError("hardlinked Git metadata is not supported")
    return info


def _read_metadata_pointer(path: Path) -> str:
    info = _metadata_entry_is_safe(path, regular_file=True)
    if info.st_size > 8_192:
        raise RepositoryError("Git metadata pointer exceeds limit")
    try:
        value = path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RepositoryError("Git metadata pointer is unreadable") from exc
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise RepositoryError("Git metadata pointer is malformed")
    return value


def _resolve_metadata_pointer(base: Path, raw: str) -> Path:
    if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise RepositoryError("network or device Git metadata is not supported")
    candidate = Path(raw)
    try:
        return (candidate if candidate.is_absolute() else base / candidate).resolve(strict=True)
    except OSError as exc:
        raise RepositoryError("Git metadata pointer target is unavailable") from exc


def _validate_config_file(path: Path) -> None:
    if not path.exists():
        return
    info = _metadata_entry_is_safe(path, regular_file=True)
    if info.st_size > 1_048_576:
        raise RepositoryError("Git config exceeds limit")
    try:
        config_text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise RepositoryError("Git config is unreadable") from exc
    if re.search(r"(?im)^\s*\[\s*include(?:if)?\b", config_text):
        raise RepositoryError("Git config includes are not supported")


def _validate_metadata_directory_shallow(path: Path, *, max_entries: int = 16_384) -> None:
    """Reject indirection at bounded Git metadata boundaries without walking objects."""

    if not path.exists():
        return
    _metadata_entry_is_safe(path, regular_file=False)
    seen = 0
    try:
        iterator = os.scandir(path)
    except OSError as exc:
        raise RepositoryError("Git metadata is unreadable") from exc
    with iterator:
        for entry in iterator:
            seen += 1
            if seen > max_entries:
                raise RepositoryError("Git metadata boundary exceeds limit")
            try:
                # ``DirEntry.stat(follow_symlinks=False)`` reports zeroed
                # inode/link fields on some Windows filesystems.  A direct
                # lstat is required for the hardlink boundary.
                info = Path(entry.path).lstat()
            except OSError as exc:
                raise RepositoryError("Git metadata is unreadable") from exc
            if _metadata_path_is_indirect(Path(entry.path), info):
                raise RepositoryError("reparse Git metadata is not supported")
            if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1:
                raise RepositoryError("hardlinked Git metadata is not supported")


def _validate_mutable_metadata_trees(
    scope: GitScope,
    *,
    max_entries: int = 300_000,
    max_seconds: float = 5.0,
) -> None:
    """Boundedly reject indirection in every host-Git mutation namespace."""

    deadline = time.monotonic() + max_seconds
    seen = 0
    roots: list[Path] = []
    for candidate in (
        scope.common_dir / "refs",
        scope.common_dir / "logs",
        scope.common_dir / "worktrees",
    ):
        if os.path.lexists(candidate) and candidate not in roots:
            roots.append(candidate)

    for root in roots:
        _metadata_entry_is_safe(root, regular_file=False)
        stack = [root]
        while stack:
            if time.monotonic() > deadline:
                raise RepositoryError("Git mutable metadata validation timed out")
            directory = stack.pop()
            _metadata_entry_is_safe(directory, regular_file=False)
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise RepositoryError("Git mutable metadata is unreadable") from exc
            with iterator:
                for entry in iterator:
                    seen += 1
                    if seen > max_entries:
                        raise RepositoryError("Git mutable metadata exceeds entry limit")
                    if time.monotonic() > deadline:
                        raise RepositoryError("Git mutable metadata validation timed out")
                    try:
                        info = Path(entry.path).lstat()
                        linked = entry.is_symlink()
                    except OSError as exc:
                        raise RepositoryError("Git mutable metadata is unreadable") from exc
                    if linked or _metadata_path_is_indirect(Path(entry.path), info):
                        raise RepositoryError("reparse Git mutable metadata is not supported")
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        if getattr(info, "st_nlink", 1) > 1:
                            raise RepositoryError(
                                "hardlinked Git mutable metadata is not supported"
                            )
                    else:
                        raise RepositoryError("Git mutable metadata has an invalid type")

    objects = scope.common_dir / "objects"
    if os.path.lexists(objects):
        _metadata_entry_is_safe(objects, regular_file=False)
        try:
            iterator = os.scandir(objects)
        except OSError as exc:
            raise RepositoryError("Git object metadata is unreadable") from exc
        with iterator:
            for entry in iterator:
                seen += 1
                if seen > max_entries:
                    raise RepositoryError("Git mutable metadata exceeds entry limit")
                if time.monotonic() > deadline:
                    raise RepositoryError("Git mutable metadata validation timed out")
                try:
                    info = Path(entry.path).lstat()
                    linked = entry.is_symlink()
                except OSError as exc:
                    raise RepositoryError("Git object metadata is unreadable") from exc
                if linked or _metadata_path_is_indirect(Path(entry.path), info):
                    raise RepositoryError("reparse Git object metadata is not supported")
                name = entry.name.casefold()
                if re.fullmatch(r"[0-9a-f]{2}", name):
                    if not stat.S_ISDIR(info.st_mode):
                        raise RepositoryError("Git loose-object fanout has an invalid type")
                    fanout = Path(entry.path)
                    _metadata_entry_is_safe(fanout, regular_file=False)
                    try:
                        fanout_iterator = os.scandir(fanout)
                    except OSError as exc:
                        raise RepositoryError("Git loose objects are unreadable") from exc
                    with fanout_iterator:
                        for loose in fanout_iterator:
                            seen += 1
                            if seen > max_entries:
                                raise RepositoryError(
                                    "Git mutable metadata exceeds entry limit"
                                )
                            if time.monotonic() > deadline:
                                raise RepositoryError(
                                    "Git mutable metadata validation timed out"
                                )
                            try:
                                loose_info = Path(loose.path).lstat()
                                linked = loose.is_symlink()
                            except OSError as exc:
                                raise RepositoryError(
                                    "Git loose objects are unreadable"
                                ) from exc
                            if (
                                linked
                                or _metadata_path_is_indirect(
                                    Path(loose.path), loose_info
                                )
                                or not stat.S_ISREG(loose_info.st_mode)
                            ):
                                raise RepositoryError(
                                    "Git loose object has an invalid or indirect type"
                                )
                            if getattr(loose_info, "st_nlink", 1) > 1:
                                raise RepositoryError(
                                    "hardlinked Git loose objects are not supported"
                                )
                elif name in {"info", "pack"}:
                    if not stat.S_ISDIR(info.st_mode):
                        raise RepositoryError("Git object metadata has an invalid type")
                    _validate_metadata_directory_shallow(
                        Path(entry.path), max_entries=max_entries
                    )
                elif not stat.S_ISREG(info.st_mode):
                    raise RepositoryError("Git object metadata has an invalid type")
                elif getattr(info, "st_nlink", 1) > 1:
                    raise RepositoryError("hardlinked Git object metadata is not supported")

    for candidate in (
        scope.git_dir / "index",
        scope.git_dir / "index.lock",
        scope.git_dir / "HEAD",
        scope.git_dir / "HEAD.lock",
        scope.common_dir / "packed-refs",
        scope.common_dir / "packed-refs.lock",
    ):
        if os.path.lexists(candidate):
            _metadata_entry_is_safe(candidate, regular_file=True)


def _validate_git_scope(project: Path) -> None:
    marker = project / ".git"
    if marker.is_dir():
        _metadata_entry_is_safe(marker, regular_file=False)
        scope = GitScope(git_dir=marker, common_dir=marker, linked_worktree=False)
    elif marker.is_file():
        pointer = _read_metadata_pointer(marker)
        match = re.fullmatch(r"(?i)gitdir:\s*(.+)", pointer)
        if not match:
            raise RepositoryError("linked worktree marker is malformed")
        git_dir = _resolve_metadata_pointer(project, match.group(1))
        _metadata_entry_is_safe(git_dir, regular_file=False)
        if git_dir.parent.name.casefold() != "worktrees":
            raise RepositoryError("linked worktree metadata has an unexpected layout")
        common_pointer = _read_metadata_pointer(git_dir / "commondir")
        common_dir = _resolve_metadata_pointer(git_dir, common_pointer)
        _metadata_entry_is_safe(common_dir, regular_file=False)
        if common_dir.name.casefold() != ".git" or git_dir.parent.parent != common_dir:
            raise RepositoryError("linked worktree common metadata has an unexpected layout")
        backlink = _resolve_metadata_pointer(git_dir, _read_metadata_pointer(git_dir / "gitdir"))
        if os.path.normcase(str(backlink)) != os.path.normcase(str(marker.resolve(strict=True))):
            raise RepositoryError("linked worktree metadata does not point back to the project")
        scope = GitScope(git_dir=git_dir, common_dir=common_dir, linked_worktree=True)
    else:
        raise RepositoryError("project must be a Git worktree")

    for config_path in (scope.common_dir / "config", scope.git_dir / "config.worktree"):
        _validate_config_file(config_path)
    for boundary in (
        scope.git_dir,
        scope.common_dir / "objects",
        scope.common_dir / "objects" / "info",
        scope.common_dir / "objects" / "pack",
    ):
        _validate_metadata_directory_shallow(boundary)
    _validate_mutable_metadata_trees(scope)
    alternates = scope.common_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise RepositoryError("Git object alternates are not supported")
    grafts = scope.common_dir / "info" / "grafts"
    if os.path.lexists(grafts):
        raise RepositoryError("legacy Git grafts are not supported")
    info_attributes = scope.common_dir / "info" / "attributes"
    if os.path.lexists(info_attributes):
        raise RepositoryError("Git info attributes are not supported")
    replacements = _git(
        project,
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
        max_output_bytes=16_777_216,
    )
    if replacements.strip():
        raise RepositoryError("Git replacement refs are not supported")
    root_raw = _git(project, ["rev-parse", "--show-toplevel"], max_output_bytes=16_384)
    git_dir_raw = _git(project, ["rev-parse", "--absolute-git-dir"], max_output_bytes=16_384)
    common_dir_raw = _git(project, ["rev-parse", "--git-common-dir"], max_output_bytes=16_384)
    try:
        root = Path(root_raw.decode("utf-8", errors="strict").strip()).resolve(strict=True)
        git_dir = Path(git_dir_raw.decode("utf-8", errors="strict").strip()).resolve(strict=True)
        common_raw = Path(common_dir_raw.decode("utf-8", errors="strict").strip())
        common_dir = (project / common_raw).resolve(strict=True) if not common_raw.is_absolute() else common_raw.resolve(strict=True)
    except (UnicodeDecodeError, OSError) as exc:
        raise RepositoryError("Git scope metadata is invalid") from exc
    if os.path.normcase(str(root)) != os.path.normcase(str(project)):
        raise RepositoryError("Git top-level escapes the registered project")
    if (
        os.path.normcase(str(git_dir)) != os.path.normcase(str(scope.git_dir.resolve(strict=True)))
        or os.path.normcase(str(common_dir)) != os.path.normcase(str(scope.common_dir.resolve(strict=True)))
    ):
        raise RepositoryError("Git-reported metadata does not match the validated worktree")


def validate_git_scope(project: Path) -> Path:
    """Public, read-only validation boundary reused by the Coding Engine."""

    _validate_git_scope(project)
    return project


def tracked_files(
    project: Path,
    *,
    max_files: int,
    max_output_bytes: int,
) -> list[TrackedFile]:
    _validate_git_scope(project)
    output = _git(
        project,
        ["ls-files", "--stage", "-z"],
        max_output_bytes=max_output_bytes,
    )
    head_objects: dict[str, str] = {}
    try:
        head_output = _git(
            project,
            ["ls-tree", "-r", "-z", "--full-tree", "HEAD"],
            max_output_bytes=max_output_bytes,
        )
        for raw in head_output.split(b"\x00"):
            if not raw:
                continue
            metadata, path_bytes = raw.split(b"\t", 1)
            _mode, _kind, object_id = metadata.decode("ascii").split(" ")
            head_objects[path_bytes.decode("utf-8", errors="strict")] = object_id
    except (RepositoryError, ValueError, UnicodeDecodeError):
        head_objects = {}
    entries: list[TrackedFile] = []
    for raw in output.split(b"\x00"):
        if not raw:
            continue
        try:
            metadata, path_bytes = raw.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split(" ")
            path = path_bytes.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RepositoryError("malformed Git tracked-file inventory") from exc
        if stage != "0" or mode in {"120000", "160000"}:
            continue
        if len(path) > 4_096:
            raise RepositoryError("tracked path exceeds configured limit")
        entries.append(
            TrackedFile(
                path=path,
                git_mode=mode,
                git_object=object_id,
                head_object=head_objects.get(path),
            )
        )
        if len(entries) > max_files:
            raise RepositoryError("tracked file count exceeds configured limit")
    return sorted(entries, key=lambda item: item.path.casefold())


def git_changed_paths(project: Path) -> set[str]:
    _validate_git_scope(project)
    changed: set[str] = set()
    commands = (
        ["diff-files", "--name-only", "-z"],
        ["diff-index", "--cached", "--name-only", "-z", "HEAD"],
    )
    for command in commands:
        try:
            output = _git(project, command)
        except RepositoryError:
            continue
        try:
            changed.update(
                item.decode("utf-8", errors="strict")
                for item in output.split(b"\x00")
                if item
            )
        except UnicodeDecodeError as exc:
            raise RepositoryError("malformed Git change inventory") from exc
    return changed


def git_worktree_object_ids(project: Path, paths: list[str]) -> dict[str, str]:
    """Hash raw worktree bytes with Git without mutating its index stat cache."""

    if not paths:
        return {}
    _validate_git_scope(project)
    result: dict[str, str] = {}
    for offset in range(0, len(paths), 64):
        batch = paths[offset:offset + 64]
        output = _git(
            project,
            ["hash-object", "--no-filters", "--", *batch],
            max_output_bytes=max(16_384, len(batch) * 80),
        )
        try:
            object_ids = output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise RepositoryError("malformed Git worktree hashes") from exc
        if len(object_ids) != len(batch) or any(
            not re.fullmatch(r"[a-fA-F0-9]{40,64}", value)
            for value in object_ids
        ):
            raise RepositoryError("malformed Git worktree hashes")
        result.update(zip(batch, object_ids, strict=True))
    return result


def ripgrep_allowed_files(
    project: Path,
    query: str,
    allowed_paths: list[str],
    *,
    max_matches: int = 100,
    max_output_bytes: int = 4_194_304,
) -> list[dict[str, object]]:
    """Run fixed-string rg only over files already admitted to a fresh map."""

    if not _RG_EXECUTABLE.is_file():
        raise RepositoryError("trusted ripgrep executable is unavailable")
    if not query or len(query) > 2_048 or not (1 <= max_matches <= 1_000):
        raise RepositoryError("ripgrep query or match limit is invalid")
    try:
        _RG_EXECUTABLE.relative_to(project)
    except ValueError:
        pass
    else:
        raise RepositoryError("ripgrep executable may not come from the registered project")
    environment = os.environ.copy()
    environment["RIPGREP_CONFIG_PATH"] = os.devnull
    results: list[dict[str, object]] = []
    captured_bytes = 0
    ordered_paths = sorted(set(allowed_paths), key=str.casefold)
    for offset in range(0, len(ordered_paths), 64):
        batch = ordered_paths[offset:offset + 64]
        command = [
            str(_RG_EXECUTABLE),
            "--fixed-strings",
            "--line-number",
            "--column",
            "--no-heading",
            "--with-filename",
            "--color=never",
            "--sort=path",
            "--max-count=3",
            "--max-columns=2048",
            "--",
            query,
            *batch,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepositoryError("ripgrep invocation failed") from exc
        if completed.returncode not in {0, 1}:
            raise RepositoryError("ripgrep could not search the approved file set")
        captured_bytes += len(completed.stdout)
        if captured_bytes > max_output_bytes:
            raise RepositoryError("ripgrep output exceeds configured limit")
        try:
            lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise RepositoryError("ripgrep output is not UTF-8") from exc
        for line in lines:
            parts = line.split(":", 3)
            if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
                continue
            path, line_number, column, text = parts
            if path not in batch:
                raise RepositoryError("ripgrep returned a path outside the approved file set")
            try:
                reject_secret_text(text)
            except KnowledgePolicyError:
                continue
            results.append(
                {
                    "path": path,
                    "line": int(line_number),
                    "column": int(column),
                    "text": text[:2_048],
                    "untrusted": True,
                    "local_only": True,
                }
            )
            if len(results) >= max_matches:
                return results
    return results


def sanitize_git_remote(value: str | None) -> str | None:
    if not value:
        return None
    remote = value.strip()
    if not remote or "\x00" in remote:
        return None
    if "://" in remote:
        parsed = urlsplit(remote)
        host = parsed.hostname
        if not parsed.scheme or not host:
            return None
        try:
            parsed_port = parsed.port
        except ValueError:
            return None
        if parsed.scheme.casefold() not in {"https", "ssh", "git"}:
            return None
        port = f":{parsed_port}" if parsed_port else ""
        return urlunsplit((parsed.scheme.casefold(), f"{host}{port}", parsed.path, "", ""))[:2048]
    match = re.fullmatch(r"(?:[^@\s]+@)?(?P<host>[^:\s]+):(?P<path>[^\s]+)", remote)
    if match:
        return f"{match.group('host')}:{match.group('path')}"[:2048]
    candidate = Path(remote)
    if candidate.is_absolute():
        return "local-path://redacted"
    return None


def git_remote(project: Path) -> str | None:
    try:
        output = _git(
            project,
            ["config", "--local", "--no-includes", "--get", "remote.origin.url"],
            max_output_bytes=16_384,
        ).decode("utf-8", errors="strict")
    except (RepositoryError, UnicodeDecodeError):
        return None
    sanitized = sanitize_git_remote(output)
    if sanitized:
        try:
            reject_secret_text(sanitized)
        except KnowledgePolicyError:
            return None
    return sanitized


def git_history_payload(project: Path, limit: int) -> bytes:
    try:
        output = _git(
            project,
            [
                "log",
                "--no-show-signature",
                f"--max-count={limit}",
                "--format=%H%x1f%aI%x1f%s",
            ],
        )
    except RepositoryError:
        return b""
    lines: list[str] = []
    for raw in output.decode("utf-8", errors="replace").splitlines():
        parts = raw.split("\x1f", 2)
        if len(parts) != 3 or not re.fullmatch(r"[a-fA-F0-9]{40,64}", parts[0]):
            continue
        subject = " ".join(parts[2].split())[:512]
        lines.append(f"{parts[0]} {parts[1]} {subject}")
    return "\n".join(lines).encode("utf-8")


def infer_source_kind(path: str) -> SourceKind:
    relative = Path(path)
    suffix = relative.suffix.casefold()
    if suffix == ".md":
        return SourceKind.MARKDOWN
    if suffix == ".txt":
        return SourceKind.TEXT
    if relative.parts and relative.parts[0].casefold() == "config":
        return SourceKind.PROJECT_CONFIG
    if len(relative.parts) == 1 and relative.name.casefold() in _MANIFEST_NAMES:
        return SourceKind.PROJECT_CONFIG
    return SourceKind.REPOSITORY_FILE


def _symbols(path: str, payload: bytes) -> list[str]:
    suffix = Path(path).suffix.casefold()
    text = payload.decode("utf-8", errors="strict")
    values: list[str] = []
    if suffix == ".py":
        try:
            tree = ast.parse(text, filename=path)
        except (SyntaxError, ValueError):
            return []
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                values.append(node.name)
    elif suffix == ".md":
        values.extend(match.group(1).strip() for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", text))
    elif suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        values.extend(match.group(1) for match in re.finditer(r"(?m)^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)", text))
    elif suffix == ".ps1":
        values.extend(match.group(1) for match in re.finditer(r"(?im)^\s*function\s+([A-Za-z0-9_-]+)", text))
    elif suffix in {
        ".c", ".cc", ".cpp", ".cs", ".dart", ".ex", ".exs", ".fs", ".fsx",
        ".go", ".h", ".hh", ".hpp", ".java", ".kt", ".kts", ".php", ".proto",
        ".rb", ".rs", ".scala", ".swift",
    }:
        patterns = (
            r"(?m)^\s*(?:(?:pub|public|private|protected|internal|static|async|open|sealed|final)\s+)*(?:class|struct|enum|interface|trait|record|module|protocol|actor)\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"(?m)^\s*(?:(?:pub|public|private|protected|internal|static|async|unsafe|extern|override|virtual)\s+)*(?:fn|func|function|def)\s+([A-Za-z_][A-Za-z0-9_]*)",
        )
        for pattern in patterns:
            values.extend(match.group(1) for match in re.finditer(pattern, text))
    return list(dict.fromkeys(value[:256] for value in values))[:2000]


def _category(path: str) -> str:
    relative = Path(path)
    if relative.name.casefold() == "agents.md":
        return "instructions"
    if relative.suffix.casefold() == ".md":
        return "documentation"
    if "test" in relative.name.casefold() or any(part.casefold() in {"test", "tests"} for part in relative.parts):
        return "test"
    if relative.name.casefold() in _MANIFEST_NAMES:
        return "manifest"
    if relative.name.casefold() in _ENTRY_NAMES:
        return "entry_point"
    if relative.parts and relative.parts[0].casefold() == "config":
        return "configuration"
    return "source"


def prepare_tracked_source(
    project: Path,
    entry: TrackedFile,
    policy: KnowledgePolicy,
    commit_sha: str | None,
    *,
    previous_file: RepositoryFileV1 | None = None,
    reuse_allowed: bool = False,
    fast_reuse: bool = False,
    observed_size_bytes: int | None = None,
    observed_mtime_ns: int | None = None,
) -> PreparedSource:
    source_kind = infer_source_kind(entry.path)
    if fast_reuse and previous_file is not None:
        if observed_size_bytes is None or observed_mtime_ns is None:
            raise RepositoryError("fast reuse requires a validated file observation")
        read = ReadSource(
            project_path=project,
            source_path=project / entry.path,
            relative_path=entry.path,
            payload=b"",
            size_bytes=observed_size_bytes,
            mtime_ns=observed_mtime_ns,
        )
        clean_at_commit = bool(
            commit_sha and entry.head_object and entry.git_object == entry.head_object
        )
        reused_file = previous_file.model_copy(
            update={
                "mtime_ns": observed_mtime_ns,
                "size_bytes": observed_size_bytes,
                "git_commit_sha": commit_sha if clean_at_commit else None,
                "dirty": not clean_at_commit,
                "git_object_id": entry.head_object,
                "git_index_object_id": entry.git_object,
                "git_worktree_object_id": previous_file.git_worktree_object_id,
            }
        )
        return PreparedSource(
            read=read,
            source_uri=f"project://{entry.path}",
            source_kind=source_kind,
            source_hash=previous_file.content_hash,
            parser=f"{source_kind.value}-parser",
            fragments=(),
            facts_by_ordinal={},
            file=reused_file,
            reused=True,
        )
    read = read_registered_source(project, entry.path, policy, repository_tracked=True)
    source_hash = hashlib.sha256(read.payload).hexdigest()
    algorithm = hashlib.sha256 if len(entry.git_object) == 64 else hashlib.sha1
    git_blob = algorithm(
        f"blob {len(read.payload)}\0".encode("ascii") + read.payload
    ).hexdigest()
    normalized_payload = read.payload.replace(b"\r\n", b"\n")
    normalized_blob = algorithm(
        f"blob {len(normalized_payload)}\0".encode("ascii") + normalized_payload
    ).hexdigest()
    clean_at_commit = bool(
        commit_sha
        and entry.head_object
        and entry.git_object == entry.head_object
        and entry.head_object in {git_blob, normalized_blob}
    )
    if Path(entry.path).suffix.casefold() == ".json":
        try:
            json.loads(read.payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise KnowledgePolicyError("format.malformed_structured_payload") from exc
    if reuse_allowed and previous_file is not None and previous_file.content_hash == source_hash:
        reused_file = previous_file.model_copy(
            update={
                "mtime_ns": read.mtime_ns,
                "size_bytes": read.size_bytes,
                "git_commit_sha": commit_sha if clean_at_commit else None,
                "dirty": not clean_at_commit,
                "git_object_id": entry.head_object,
                "git_index_object_id": entry.git_object,
                "git_worktree_object_id": git_blob,
            }
        )
        return PreparedSource(
            read=read,
            source_uri=f"project://{entry.path}",
            source_kind=source_kind,
            source_hash=source_hash,
            parser=f"{source_kind.value}-parser",
            fragments=(),
            facts_by_ordinal={},
            file=reused_file,
            reused=True,
        )
    fragments = tuple(parse_source(read.payload, source_kind, policy))
    for fragment in fragments:
        reject_secret_text(fragment.content)
        reject_secret_text(fragment.locator)
        if fragment.title:
            reject_secret_text(fragment.title)
    facts = {fragment.ordinal: tuple(extract_facts(fragment)) for fragment in fragments}
    language = _LANGUAGES.get(Path(entry.path).suffix.casefold())
    file = RepositoryFileV1(
        path=entry.path,
        content_hash=source_hash,
        size_bytes=read.size_bytes,
        mtime_ns=read.mtime_ns,
        language=language,
        category=_category(entry.path),
        symbols=_symbols(entry.path, read.payload),
        indexed=True,
        git_commit_sha=commit_sha if clean_at_commit else None,
        dirty=not clean_at_commit,
        git_object_id=entry.head_object,
        git_index_object_id=entry.git_object,
        git_worktree_object_id=git_blob,
    )
    return PreparedSource(
        read=read,
        source_uri=f"project://{entry.path}",
        source_kind=source_kind,
        source_hash=source_hash,
        parser=f"{source_kind.value}-parser",
        fragments=fragments,
        facts_by_ordinal=facts,
        file=file,
        reused=False,
    )


def _commands(files: list[PreparedSource]) -> list[str]:
    commands: list[str] = []
    for source in files:
        name = source.read.relative_path.casefold()
        try:
            if name == "package.json":
                payload = json.loads(source.read.payload)
                scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
                if isinstance(scripts, dict):
                    commands.extend(f"npm run {key}" for key in scripts if isinstance(key, str))
            elif name == "pyproject.toml":
                payload = tomllib.loads(source.read.payload.decode("utf-8"))
                project_table = payload.get("project", {})
                scripts = project_table.get("scripts", {}) if isinstance(project_table, dict) else {}
                if isinstance(scripts, dict):
                    commands.extend(str(key) for key in scripts)
                tool_table = payload.get("tool", {})
                if isinstance(tool_table, dict) and tool_table.get("pytest") is not None:
                    commands.append("uv run pytest")
        except (AttributeError, ValueError, TypeError, tomllib.TOMLDecodeError):
            continue
    return sorted(set(commands))[:128]


def build_repository_map(
    *,
    owner_id: str,
    project: Path,
    commit_sha: str | None,
    remote: str | None,
    prepared: list[PreparedSource],
    worktree_revision_value: str,
    policy_version: str,
    tracked_files_count: int,
    blocked_files_count: int,
    blocked_sources: list[BlockedRepositorySourceV1],
) -> RepositoryMapV1:
    files = [source.file for source in prepared]
    languages = Counter(file.language for file in files if file.language)
    paths = [file.path for file in files]
    manifests = sorted(path for path in paths if Path(path).name.casefold() in _MANIFEST_NAMES)
    entry_points = sorted(path for path in paths if Path(path).name.casefold() in _ENTRY_NAMES)
    tests = sorted(file.path for file in files if file.category == "test")
    documentation = sorted(file.path for file in files if file.category in {"documentation", "instructions"})
    agents = sorted(path for path in paths if Path(path).name.casefold() == "agents.md")
    modules = sorted({Path(path).parts[0] for path in paths if len(Path(path).parts) > 1})
    return RepositoryMapV1(
        owner_id=owner_id,
        project_path=str(project),
        git_commit_sha=commit_sha,
        git_remote=remote,
        worktree_revision=worktree_revision_value,
        policy_version=policy_version,
        tracked_files_count=tracked_files_count,
        blocked_files_count=blocked_files_count,
        generated_at=datetime.now(timezone.utc),
        languages=dict(sorted(languages.items())),
        manifests=manifests,
        entry_points=entry_points,
        modules=modules,
        tests=tests,
        commands=_commands(prepared),
        documentation=documentation,
        agents_hierarchy=agents,
        files=files,
        blocked_sources=blocked_sources,
    )


def worktree_revision(
    commit_sha: str | None,
    prepared: list[PreparedSource],
    policy_version: str,
    *,
    derivation_version: str,
    sensitivity: str,
    remote: str | None,
    tracked_entries: list[TrackedFile],
    blocked_sources: list[BlockedRepositorySourceV1],
) -> str:
    digest = hashlib.sha256()
    digest.update((commit_sha or "unborn").encode("ascii"))
    digest.update(policy_version.encode("utf-8"))
    digest.update(derivation_version.encode("utf-8"))
    digest.update(sensitivity.encode("ascii"))
    digest.update((remote or "no-remote").encode("utf-8"))
    # Inventory is part of the revision even when a path is privacy-blocked.
    # Only hashes/reason codes are persisted in this digest, so a secret-shaped
    # blocked path never leaks through revision metadata.
    for entry in sorted(tracked_entries, key=lambda item: item.path.casefold()):
        digest.update(hashlib.sha256(entry.path.encode("utf-8")).digest())
        digest.update(entry.git_mode.encode("ascii"))
        digest.update(entry.git_object.encode("ascii"))
        digest.update((entry.head_object or "unborn").encode("ascii"))
    for blocked in sorted(
        blocked_sources,
        key=lambda item: (item.path_hash, item.reason_code),
    ):
        digest.update(blocked.path_hash.encode("ascii"))
        digest.update(blocked.reason_code.encode("utf-8"))
    for source in sorted(prepared, key=lambda item: item.read.relative_path.casefold()):
        digest.update(source.read.relative_path.encode("utf-8"))
        digest.update(source.source_hash.encode("ascii"))
        digest.update(str(source.read.size_bytes).encode("ascii"))
        digest.update(str(source.read.mtime_ns).encode("ascii"))
    return digest.hexdigest()
