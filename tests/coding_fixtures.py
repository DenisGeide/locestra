from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping, Sequence


FIXTURE_SCHEMA_VERSION = "1.0"
FIXTURE_KIND = "local-agent-coding-e2e"
OWNERSHIP_MARKER_NAME = ".local-agent-coding-fixture.json"
FIXTURE_DIRECTORY_PREFIX = "local-agent-coding-e2e-"

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent.resolve()
TEMPLATE_ROOT = TESTS_ROOT / "fixtures" / "coding_repo"

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Local Agent Coding Fixture",
    "GIT_AUTHOR_EMAIL": "coding-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Local Agent Coding Fixture",
    "GIT_COMMITTER_EMAIL": "coding-fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}


class UnsafeFixtureCleanup(RuntimeError):
    """Raised instead of deleting a directory whose ownership cannot be proven."""


@dataclass(frozen=True)
class CodingWorktree:
    task_id: str
    path: Path
    branch: str
    baseline_sha: str


@dataclass(frozen=True)
class CodingFixture:
    root: Path
    repository: Path
    remote: Path
    worktrees_root: Path
    artifacts_root: Path
    marker_path: Path
    run_id: str
    creation_nonce: str
    creator_pid: int
    baseline_sha: str
    remote_ref: str
    remote_baseline_sha: str
    _worktrees: list[CodingWorktree] = field(default_factory=list, repr=False)

    @property
    def worktrees(self) -> tuple[CodingWorktree, ...]:
        return tuple(self._worktrees)

    def add_worktree(self, task_id: str) -> CodingWorktree:
        """Create a collision-safe linked worktree without interpolating shell text."""

        _validate_task_id(task_id)

        ordinal = len(self._worktrees) + 1
        slug = _task_slug(task_id)
        digest = hashlib.sha256(
            f"{self.run_id}\0{task_id}\0{ordinal}".encode("utf-8")
        ).hexdigest()[:12]
        leaf = f"{slug}-{digest}"
        branch = f"codex/e2e-{leaf}"
        path = self.worktrees_root / leaf
        _require_strict_child(path, self.root)

        _run_git(
            self.repository,
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                branch,
                str(path),
                self.baseline_sha,
            ],
        )
        worktree = CodingWorktree(
            task_id=task_id,
            path=path.resolve(strict=True),
            branch=branch,
            baseline_sha=self.baseline_sha,
        )
        _require_strict_child(worktree.path, self.root)
        self._worktrees.append(worktree)
        return worktree

    def git(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run Git with fixture-only identity and no global/system configuration."""

        target = (cwd or self.repository).resolve(strict=True)
        _require_within(target, self.root)
        return _run_git(target, arguments, check=check, completed=True)

    def remote_head(self) -> str:
        completed = _run_git(
            self.root,
            ["--git-dir", str(self.remote), "rev-parse", self.remote_ref],
            completed=True,
        )
        return completed.stdout.strip()

    def assert_remote_unchanged(self) -> None:
        actual = self.remote_head()
        if actual != self.remote_baseline_sha:
            raise AssertionError(
                f"fixture remote changed: expected {self.remote_baseline_sha}, received {actual}"
            )

    def artifact_directory(self, task_id: str) -> Path:
        _validate_task_id(task_id)
        slug = _task_slug(task_id)
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
        destination = self.artifacts_root / f"{slug}-{digest}"
        _require_strict_child(destination, self.root)
        destination.mkdir(parents=False, exist_ok=False)
        return destination.resolve(strict=True)

    def cleanup(self) -> None:
        expected = _expected_marker(
            root=self.root,
            run_id=self.run_id,
            creation_nonce=self.creation_nonce,
            creator_pid=self.creator_pid,
        )
        _remove_validated_fixture_root(self.root, expected)

    def __enter__(self) -> "CodingFixture":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()


def create_coding_fixture(
    *,
    temp_parent: Path | str | None = None,
    run_id: str | None = None,
) -> CodingFixture:
    """Create a synthetic Git repository outside the product repository.

    The returned fixture owns only its generated root. The root is marked before
    repository setup begins, so partial setup can still be cleaned with the same
    exact marker/root validation used by normal cleanup.
    """

    if not TEMPLATE_ROOT.is_dir():
        raise FileNotFoundError(f"coding fixture template is missing: {TEMPLATE_ROOT}")
    if (TEMPLATE_ROOT / ".git").exists():
        raise RuntimeError("coding fixture template must not contain Git metadata")

    resolved_run_id = run_id or uuid.uuid4().hex
    if not _SAFE_RUN_ID.fullmatch(resolved_run_id):
        raise ValueError("run_id must contain only ASCII letters, digits, '_' or '-'")

    parent = Path(temp_parent or tempfile.gettempdir()).expanduser()
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve(strict=True)
    if _is_within(parent, PROJECT_ROOT):
        raise ValueError("coding fixtures must be created outside the product repository")

    root = Path(
        tempfile.mkdtemp(
            prefix=f"{FIXTURE_DIRECTORY_PREFIX}{resolved_run_id}-",
            dir=str(parent),
        )
    ).resolve(strict=True)
    _require_strict_child(root, parent)
    if _is_within(root, PROJECT_ROOT):
        raise RuntimeError("temporary fixture unexpectedly resolved inside the product repository")

    creation_nonce = uuid.uuid4().hex
    creator_pid = os.getpid()
    expected_marker = _expected_marker(
        root=root,
        run_id=resolved_run_id,
        creation_nonce=creation_nonce,
        creator_pid=creator_pid,
    )
    marker_path = root / OWNERSHIP_MARKER_NAME
    with marker_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(expected_marker, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    try:
        repository = root / "repo"
        remote = root / "remote.git"
        worktrees_root = root / "worktrees"
        artifacts_root = root / "artifacts"
        # Validation may compile the fixture template before this suite runs.
        # Generated bytecode is intentionally not part of the synthetic Git
        # baseline and must not make source/worktree snapshots order-dependent.
        shutil.copytree(
            TEMPLATE_ROOT,
            repository,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        for template in repository.rglob("*.template"):
            materialized = template.with_suffix("")
            if materialized.exists():
                raise RuntimeError(f"fixture template target already exists: {materialized}")
            template.replace(materialized)
        worktrees_root.mkdir()
        artifacts_root.mkdir()

        _run_git(repository, ["init", "--quiet", "--initial-branch=main"])
        _run_git(repository, ["add", "--all"])
        _run_git(repository, ["commit", "--quiet", "-m", "coding fixture baseline"])
        baseline_sha = _run_git(
            repository, ["rev-parse", "HEAD"], completed=True
        ).stdout.strip()

        # Keep source-object ownership exact. A local clone otherwise
        # hardlinks loose objects between the source and fixture remote,
        # which is intentionally rejected by the production metadata scope.
        _run_git(
            root,
            [
                "clone",
                "--quiet",
                "--bare",
                "--no-hardlinks",
                str(repository),
                str(remote),
            ],
        )
        _run_git(
            repository,
            [
                "remote",
                "add",
                "origin",
                "https://example.invalid/local-agent-coding-fixture.git",
            ],
        )
        remote_ref = "refs/heads/main"
        remote_baseline_sha = _run_git(
            root,
            ["--git-dir", str(remote), "rev-parse", remote_ref],
            completed=True,
        ).stdout.strip()
        if remote_baseline_sha != baseline_sha:
            raise RuntimeError("local bare remote does not match the fixture baseline")

        return CodingFixture(
            root=root,
            repository=repository.resolve(strict=True),
            remote=remote.resolve(strict=True),
            worktrees_root=worktrees_root.resolve(strict=True),
            artifacts_root=artifacts_root.resolve(strict=True),
            marker_path=marker_path.resolve(strict=True),
            run_id=resolved_run_id,
            creation_nonce=creation_nonce,
            creator_pid=creator_pid,
            baseline_sha=baseline_sha,
            remote_ref=remote_ref,
            remote_baseline_sha=remote_baseline_sha,
        )
    except BaseException:
        _remove_validated_fixture_root(root, expected_marker)
        raise


@contextmanager
def coding_fixture(
    *,
    temp_parent: Path | str | None = None,
    run_id: str | None = None,
) -> Iterator[CodingFixture]:
    fixture = create_coding_fixture(temp_parent=temp_parent, run_id=run_id)
    try:
        yield fixture
    finally:
        fixture.cleanup()


def file_snapshot(root: Path | str) -> dict[str, tuple[int, str]]:
    """Return path -> (size, sha256) without following links or Git metadata."""

    canonical_root = Path(root).resolve(strict=True)
    snapshot: dict[str, tuple[int, str]] = {}
    pending = [canonical_root]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            relative = path.relative_to(canonical_root)
            if relative.parts and relative.parts[0] == ".git":
                continue
            if _is_link_or_reparse(path):
                target = os.readlink(path) if path.is_symlink() else "<reparse-point>"
                encoded = target.encode("utf-8", errors="surrogatepass")
                snapshot[relative.as_posix()] = (
                    len(encoded),
                    hashlib.sha256(encoded).hexdigest(),
                )
            elif entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                snapshot[relative.as_posix()] = (size, digest.hexdigest())
    return dict(sorted(snapshot.items()))


def _task_slug(task_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", task_id).casefold()
    slug = _UNSAFE_SLUG_CHARACTERS.sub("-", normalized).strip("-")[:32]
    return slug or "task"


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    if len(task_id) > 4096:
        raise ValueError("task_id is too long")


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(_GIT_IDENTITY_ENV)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git(
    cwd: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
    completed: bool = False,
) -> subprocess.CompletedProcess[str] | str:
    command = ["git", *[str(argument) for argument in arguments]]
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=_git_environment(),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check and result.returncode != 0:
        evidence = (result.stderr or result.stdout).strip()[-4000:]
        raise RuntimeError(f"Git command failed ({result.returncode}): {evidence}")
    return result if completed else result.stdout.strip()


def _expected_marker(
    *,
    root: Path,
    run_id: str,
    creation_nonce: str,
    creator_pid: int,
) -> dict[str, object]:
    canonical_root = root.resolve(strict=True)
    return {
        "canonical_root": str(canonical_root),
        "creation_token": (
            creation_nonce
        ),
        "creator_pid": creator_pid,
        "kind": FIXTURE_KIND,
        "run_id": run_id,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "temp_parent": str(canonical_root.parent),
    }


def _remove_validated_fixture_root(
    root: Path,
    expected_marker: Mapping[str, object],
) -> None:
    canonical_root, marker = _validate_owned_root(root, expected_marker)
    for child in list(canonical_root.iterdir()):
        if child == marker:
            continue
        _remove_owned_entry(child, canonical_root)

    # Re-read ownership after removing contents and immediately before deleting
    # the proof itself. A changed/missing marker leaves the root for inspection.
    _, marker = _validate_owned_root(canonical_root, expected_marker)
    marker.unlink()
    canonical_root.rmdir()


def _validate_owned_root(
    root: Path,
    expected_marker: Mapping[str, object],
) -> tuple[Path, Path]:
    absolute = Path(os.path.abspath(root))
    if not absolute.exists() or not absolute.is_dir():
        raise UnsafeFixtureCleanup(f"fixture root is missing or not a directory: {absolute}")
    if _is_link_or_reparse(absolute):
        raise UnsafeFixtureCleanup(f"fixture root is a link or reparse point: {absolute}")

    canonical = absolute.resolve(strict=True)
    expected_root = Path(str(expected_marker.get("canonical_root", "")))
    expected_parent = Path(str(expected_marker.get("temp_parent", "")))
    if not _same_path(canonical, expected_root):
        raise UnsafeFixtureCleanup("fixture root does not match the expected canonical root")
    if not _same_path(canonical.parent, expected_parent):
        raise UnsafeFixtureCleanup("fixture root parent does not match the ownership marker")
    if not canonical.name.startswith(FIXTURE_DIRECTORY_PREFIX):
        raise UnsafeFixtureCleanup("fixture root does not use the required directory prefix")
    if _is_within(canonical, PROJECT_ROOT):
        raise UnsafeFixtureCleanup("refusing to delete a fixture inside the product repository")

    marker = canonical / OWNERSHIP_MARKER_NAME
    if not marker.is_file() or _is_link_or_reparse(marker):
        raise UnsafeFixtureCleanup("fixture ownership marker is missing or unsafe")
    try:
        actual_marker = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnsafeFixtureCleanup("fixture ownership marker cannot be verified") from exc
    if actual_marker != dict(expected_marker):
        raise UnsafeFixtureCleanup("fixture ownership marker does not exactly match")
    return canonical, marker


def _remove_owned_entry(path: Path, owned_root: Path) -> None:
    if path.parent == path:
        raise UnsafeFixtureCleanup("refusing to remove a filesystem root")
    if _is_link_or_reparse(path):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                os.rmdir(path)
            else:
                path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                os.rmdir(path)
            else:
                path.unlink()
        return

    resolved = path.resolve(strict=True)
    _require_strict_child(resolved, owned_root)
    if path.is_dir():
        for child in list(path.iterdir()):
            _remove_owned_entry(child, owned_root)
        path.rmdir()
        return
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _require_within(candidate: Path, parent: Path) -> None:
    if not _is_within(candidate, parent):
        raise ValueError(f"path escapes fixture root: {candidate}")


def _require_strict_child(candidate: Path, parent: Path) -> None:
    candidate_absolute = Path(os.path.abspath(candidate))
    parent_absolute = Path(os.path.abspath(parent))
    if _same_path(candidate_absolute, parent_absolute) or not _is_within(
        candidate_absolute, parent_absolute
    ):
        raise ValueError(f"path is not a strict child of fixture root: {candidate}")
