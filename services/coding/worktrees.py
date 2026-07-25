from __future__ import annotations

import json
import errno
import os
import re
import secrets
import tempfile
import threading
import time
import stat
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterator

import psutil

from services.common import ROOT, RUN_DIR
from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import WorktreeRecordV1
from services.coding.git import (
    CodingRepositoryError,
    RepositoryIdentity,
    git_ignored_paths,
    git_status_paths,
    resolve_repository,
    run_git,
    validate_coding_git_config,
)
from services.knowledge.repository import RepositoryError, validate_git_scope


class WorktreeError(RuntimeError):
    pass


class _BranchReservationCollision(WorktreeError):
    pass


class LeaseBusyError(WorktreeError):
    def __init__(self, message: str, *, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}")
_HASH = re.compile(r"[0-9a-f]{64}")
_CREATE_INTENT_KIND = "local-agent-worktree-create-intent"


@dataclass(frozen=True, slots=True)
class _WorktreeCreateIntent:
    task_id: str
    source_repository: str
    source_git_common_dir: str
    worktree_path: str
    branch: str
    base_commit: str
    owner_token_hash: str
    owner_pid: int
    created_at: datetime
    updated_at: datetime
    owner_create_time_ns: int | None = None
    phase: str = "prepared"
    git_dir: str | None = None
    git_common_dir: str | None = None
    git_marker_sha256: str | None = None

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "kind": _CREATE_INTENT_KIND,
            "task_id": self.task_id,
            "source_repository": self.source_repository,
            "source_git_common_dir": self.source_git_common_dir,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "owner_token_hash": self.owner_token_hash,
            "owner_pid": self.owner_pid,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "phase": self.phase,
            "git_dir": self.git_dir,
            "git_common_dir": self.git_common_dir,
            "git_marker_sha256": self.git_marker_sha256,
        }
        if self.owner_create_time_ns is not None:
            payload["owner_create_time_ns"] = self.owner_create_time_ns
        return payload


@dataclass(frozen=True, slots=True)
class _ObservedWorktree:
    path: Path
    git_dir: Path
    git_common_dir: Path
    git_marker_sha256: str
    head: str
    branch_commit: str | None
    clean: bool


@dataclass(frozen=True, slots=True)
class CreateIntentRecovery:
    compensated: int = 0
    paths_deleted: int = 0
    finalized: int = 0
    orphaned_records: tuple[WorktreeRecordV1, ...] = ()
    unresolved: int = 0
    invalid: int = 0
    live: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _process_create_time_ns(pid: int) -> int:
    try:
        value = psutil.Process(pid).create_time()
    except (psutil.Error, OSError, ValueError) as exc:
        raise WorktreeError("process creation identity is unavailable") from exc
    result = int(round(value * 1_000_000_000))
    if result < 1:
        raise WorktreeError("process creation identity is invalid")
    return result


def _canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise WorktreeError("owned path is unavailable") from exc


def _inside(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(parent)), os.path.normcase(str(child)))) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _reported_git_directories(project: Path) -> tuple[Path, Path]:
    values: list[Path] = []
    for arguments in (
        ["rev-parse", "--absolute-git-dir"],
        ["rev-parse", "--git-common-dir"],
    ):
        raw = run_git(project, arguments, max_output_bytes=16_384).stdout
        try:
            text = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise WorktreeError("Git metadata path is not valid UTF-8") from exc
        if not text or "\x00" in text or "\r" in text or "\n" in text:
            raise WorktreeError("Git metadata path is malformed")
        candidate = Path(text)
        values.append(_canonical(candidate if candidate.is_absolute() else project / candidate))
    return values[0], values[1]


def _linked_git_identity(project: Path) -> tuple[Path, Path, str]:
    validate_git_scope(project)
    validate_coding_git_config(project)
    marker = project / ".git"
    try:
        info = marker.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(info.st_mode)
            or marker.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or getattr(info, "st_nlink", 1) > 1
            or info.st_size > 8_192
        ):
            raise WorktreeError("owned worktree Git marker is not an exact regular pointer")
        payload = marker.read_bytes()
    except OSError as exc:
        raise WorktreeError("owned worktree Git marker is unreadable") from exc
    git_dir, common_dir = _reported_git_directories(project)
    return git_dir, common_dir, sha256(payload).hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _exclusive_json(path: Path, payload: dict[str, object]) -> None:
    """Create one durable ownership record without replacing a peer's file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_json(path: Path, *, maximum: int = 128 * 1024) -> dict[str, object]:
    try:
        info = path.lstat()
        if path.is_symlink() or info.st_size > maximum:
            raise WorktreeError("owned metadata has an invalid type or size")
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorktreeError("owned metadata is unreadable") from exc
    if not isinstance(payload, dict):
        raise WorktreeError("owned metadata must be a JSON object")
    return payload


def _parse_aware_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise WorktreeError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorktreeError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorktreeError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _parse_create_intent(payload: dict[str, object]) -> _WorktreeCreateIntent:
    legacy_keys = {
        "schema_version",
        "kind",
        "task_id",
        "source_repository",
        "source_git_common_dir",
        "worktree_path",
        "branch",
        "base_commit",
        "owner_token_hash",
        "owner_pid",
        "created_at",
        "updated_at",
        "phase",
        "git_dir",
        "git_common_dir",
        "git_marker_sha256",
    }
    keys = set(payload)
    if keys not in {frozenset(legacy_keys), frozenset((*legacy_keys, "owner_create_time_ns"))}:
        raise WorktreeError("create intent has an invalid schema")
    if payload.get("schema_version") != "1.0" or payload.get("kind") != _CREATE_INTENT_KIND:
        raise WorktreeError("create intent has an invalid identity")
    task_id = payload.get("task_id")
    source_repository = payload.get("source_repository")
    source_git_common_dir = payload.get("source_git_common_dir")
    worktree_path = payload.get("worktree_path")
    branch = payload.get("branch")
    base_commit = payload.get("base_commit")
    owner_token_hash = payload.get("owner_token_hash")
    owner_pid = payload.get("owner_pid")
    owner_create_time_ns = payload.get("owner_create_time_ns")
    phase = payload.get("phase")
    if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
        raise WorktreeError("create intent task id is invalid")
    for label, value in (
        ("source repository", source_repository),
        ("source Git directory", source_git_common_dir),
        ("worktree path", worktree_path),
    ):
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise WorktreeError(f"create intent {label} is invalid")
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 240
        or any(ord(character) < 32 or ord(character) == 127 for character in branch)
        or branch.startswith(("-", "/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or "\\" in branch
        or " " in branch
    ):
        raise WorktreeError("create intent branch is invalid")
    if not isinstance(base_commit, str) or _OBJECT_ID.fullmatch(base_commit) is None:
        raise WorktreeError("create intent base commit is invalid")
    if not isinstance(owner_token_hash, str) or _HASH.fullmatch(owner_token_hash) is None:
        raise WorktreeError("create intent owner hash is invalid")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid < 1:
        raise WorktreeError("create intent owner PID is invalid")
    if "owner_create_time_ns" in payload and (
        isinstance(owner_create_time_ns, bool)
        or not isinstance(owner_create_time_ns, int)
        or owner_create_time_ns < 1
    ):
        raise WorktreeError("create intent owner process identity is invalid")
    if phase not in {"prepared", "branch_reserved", "added"}:
        raise WorktreeError("create intent phase is invalid")
    created_at = _parse_aware_timestamp(payload.get("created_at"), label="create intent creation time")
    updated_at = _parse_aware_timestamp(payload.get("updated_at"), label="create intent update time")
    if updated_at < created_at:
        raise WorktreeError("create intent timestamps are invalid")
    optional_values: list[str | None] = []
    for label in ("git_dir", "git_common_dir", "git_marker_sha256"):
        value = payload.get(label)
        if value is not None and not isinstance(value, str):
            raise WorktreeError(f"create intent {label} is invalid")
        optional_values.append(value)
    git_dir, git_common_dir, git_marker_sha256 = optional_values
    if phase in {"prepared", "branch_reserved"} and any(
        value is not None for value in optional_values
    ):
        raise WorktreeError("pre-worktree create intent contains finalized identity")
    if phase == "added":
        if (
            git_dir is None
            or git_common_dir is None
            or git_marker_sha256 is None
            or not Path(git_dir).is_absolute()
            or not Path(git_common_dir).is_absolute()
            or _HASH.fullmatch(git_marker_sha256) is None
        ):
            raise WorktreeError("added create intent lacks finalized identity")
    return _WorktreeCreateIntent(
        task_id=task_id,
        source_repository=source_repository,
        source_git_common_dir=source_git_common_dir,
        worktree_path=worktree_path,
        branch=branch,
        base_commit=base_commit,
        owner_token_hash=owner_token_hash,
        owner_pid=owner_pid,
        created_at=created_at,
        updated_at=updated_at,
        owner_create_time_ns=(
            owner_create_time_ns if isinstance(owner_create_time_ns, int) else None
        ),
        phase=phase,
        git_dir=git_dir,
        git_common_dir=git_common_dir,
        git_marker_sha256=git_marker_sha256,
    )


class WorktreeLease(AbstractContextManager["WorktreeLease"]):
    def __init__(
        self,
        *,
        lease_path: Path,
        canonical_worktree: Path,
        task_id: str,
        policy: CodingPolicy,
        timeout_seconds: float,
        on_heartbeat: Callable[[datetime], None] | None = None,
    ) -> None:
        self.lease_path = lease_path
        self.canonical_worktree = canonical_worktree
        self.task_id = task_id
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.on_heartbeat = on_heartbeat
        self._lease_nonce = secrets.token_urlsafe(32)
        self._lease_nonce_hash = sha256(self._lease_nonce.encode("ascii")).hexdigest()
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._acquired = False

    def _payload(self, heartbeat: datetime) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "task_id": self.task_id,
            "worktree_path": str(self.canonical_worktree),
            "owner_pid": os.getpid(),
            "owner_token_hash": self._lease_nonce_hash,
            "heartbeat_at": heartbeat.isoformat(),
        }

    def acquire(self) -> "WorktreeLease":
        deadline = time.monotonic() + self.timeout_seconds
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = _utc_now()
            payload = json.dumps(self._payload(now), ensure_ascii=False, sort_keys=True).encode("utf-8")
            try:
                descriptor = os.open(self.lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                stale = self._existing_is_stale()
                if time.monotonic() >= deadline:
                    raise LeaseBusyError("worktree lease acquisition timed out", stale=stale)
                time.sleep(min(self.policy.process_poll_seconds, max(0.01, deadline - time.monotonic())))
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                self.lease_path.unlink(missing_ok=True)
                raise
            self._acquired = True
            if self.on_heartbeat:
                self.on_heartbeat(now)
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            return self

    def _existing_is_stale(self) -> bool:
        try:
            payload = _read_json(self.lease_path, maximum=32 * 1024)
            heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
            pid = int(payload["owner_pid"])
            age = (_utc_now() - heartbeat.astimezone(timezone.utc)).total_seconds()
            alive = psutil.pid_exists(pid)
            return age > self.policy.lease_stale_seconds or not alive
        except (KeyError, TypeError, ValueError, WorktreeError):
            return True

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.policy.lease_heartbeat_seconds):
            try:
                payload = _read_json(self.lease_path, maximum=32 * 1024)
                if payload.get("owner_token_hash") != self._lease_nonce_hash:
                    return
                now = _utc_now()
                _atomic_json(self.lease_path, self._payload(now))
                if self.on_heartbeat:
                    self.on_heartbeat(now)
            except Exception:
                return

    def release(self) -> None:
        if not self._acquired:
            return
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=self.policy.lease_heartbeat_seconds + 1)
        try:
            payload = _read_json(self.lease_path, maximum=32 * 1024)
            if payload.get("owner_token_hash") != self._lease_nonce_hash:
                raise WorktreeError("lease ownership changed before release")
            self.lease_path.unlink()
        finally:
            self._acquired = False

    def __enter__(self) -> "WorktreeLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class _InterprocessRecordLock(AbstractContextManager["_InterprocessRecordLock"]):
    """One-byte advisory lock retained on disk to serialize registry RMWs."""

    def __init__(self, path: Path, *, timeout_seconds: float, poll_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._descriptor: int | None = None

    @staticmethod
    def _lock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _busy(exc: OSError) -> bool:
        return exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(
            exc, "winerror", None
        ) in {33, 36}

    def acquire(self) -> "_InterprocessRecordLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\x00")
                os.fsync(descriptor)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    self._lock(descriptor)
                    self._descriptor = descriptor
                    return self
                except OSError as exc:
                    if not self._busy(exc):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorktreeError(
                            "timed out serializing a worktree registry update"
                        ) from exc
                    time.sleep(min(self.poll_seconds, remaining))
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)
            self._descriptor = None

    def __enter__(self) -> "_InterprocessRecordLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class WorktreeManager:
    _record_thread_locks_guard = threading.Lock()
    _record_thread_locks: dict[str, threading.RLock] = {}

    def __init__(
        self,
        *,
        registry_root: Path | None = None,
        owned_worktree_root: Path | None = None,
        policy: CodingPolicy | None = None,
    ) -> None:
        self.policy = policy or get_coding_policy()
        self.registry_root = registry_root or RUN_DIR / "coding"
        default_owned = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "LocalAgent" / "coding-worktrees"
        self.owned_worktree_root = owned_worktree_root or default_owned
        self.records_dir = self.registry_root / "worktrees"
        self.record_locks_dir = self.registry_root / "worktree-record-locks"
        self.create_intents_dir = self.registry_root / "worktree-create-intents"
        self.leases_dir = self.registry_root / "leases"
        self._ensure_owned_root()

    def _ensure_owned_root(self) -> None:
        self.owned_worktree_root.mkdir(parents=True, exist_ok=True)
        marker = self.owned_worktree_root / ".local-agent-owned.json"
        canonical = self.owned_worktree_root.resolve(strict=True)
        expected = {
            "schema_version": "1.0",
            "purpose": "local-agent-coding-worktrees",
            "canonical_root": str(canonical),
            "platform_root": str(ROOT.resolve(strict=True)),
        }
        if marker.exists():
            if _read_json(marker) != expected:
                raise WorktreeError("owned worktree root marker does not match this platform")
        else:
            _atomic_json(marker, expected)
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.record_locks_dir.mkdir(parents=True, exist_ok=True)
        self.create_intents_dir.mkdir(parents=True, exist_ok=True)
        self.leases_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, task_id: str) -> Path:
        if _TASK_ID.fullmatch(task_id) is None:
            raise WorktreeError("invalid task id")
        return self.records_dir / f"{task_id}.json"

    def _intent_path(self, task_id: str) -> Path:
        if _TASK_ID.fullmatch(task_id) is None:
            raise WorktreeError("invalid task id")
        return self.create_intents_dir / f"{task_id}.json"

    @contextmanager
    def _record_update_lock(self, task_id: str) -> Iterator[None]:
        path = self._record_path(task_id)
        lock_path = self.record_locks_dir / f"{path.stem}.lock"
        key = os.path.normcase(os.path.normpath(str(lock_path.resolve(strict=False))))
        with self._record_thread_locks_guard:
            thread_lock = self._record_thread_locks.setdefault(key, threading.RLock())
        with thread_lock:
            with _InterprocessRecordLock(
                lock_path,
                timeout_seconds=30.0,
                poll_seconds=max(0.01, min(self.policy.process_poll_seconds, 0.25)),
            ):
                yield

    def _write_record(
        self,
        record: WorktreeRecordV1,
        *,
        expected_absent: bool = False,
    ) -> None:
        with self._record_update_lock(record.task_id):
            self._write_record_unlocked(record, expected_absent=expected_absent)

    def _write_record_unlocked(
        self,
        record: WorktreeRecordV1,
        *,
        expected_absent: bool = False,
    ) -> None:
        path = self._record_path(record.task_id)
        payload = record.model_dump(mode="json")
        if expected_absent:
            try:
                _exclusive_json(path, payload)
            except FileExistsError as exc:
                raise WorktreeError(
                    "worktree record appeared during create finalization"
                ) from exc
            return
        _atomic_json(path, payload)

    def load(self, task_id: str) -> WorktreeRecordV1 | None:
        path = self._record_path(task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return WorktreeRecordV1.model_validate_json(json.dumps(payload, ensure_ascii=False))

    def _load_create_intent(self, task_id: str) -> _WorktreeCreateIntent | None:
        path = self._intent_path(task_id)
        if not path.exists():
            return None
        intent = _parse_create_intent(_read_json(path, maximum=64 * 1024))
        if intent.task_id != task_id:
            raise WorktreeError("create intent task identity changed")
        return intent

    def _write_create_intent(
        self,
        intent: _WorktreeCreateIntent,
        *,
        previous: _WorktreeCreateIntent | None,
    ) -> None:
        path = self._intent_path(intent.task_id)
        if previous is None:
            try:
                _exclusive_json(path, intent.payload())
            except FileExistsError as exc:
                raise WorktreeError("task already has a pending create intent") from exc
            return
        current = self._load_create_intent(intent.task_id)
        if current != previous or current.owner_token_hash != intent.owner_token_hash:
            raise WorktreeError("create intent ownership changed")
        _atomic_json(path, intent.payload())

    def _remove_create_intent(self, intent: _WorktreeCreateIntent) -> None:
        current = self._load_create_intent(intent.task_id)
        if current != intent:
            raise WorktreeError("create intent ownership changed before finalization")
        self._intent_path(intent.task_id).unlink()

    @staticmethod
    def _intent_is_live(intent: _WorktreeCreateIntent, policy: CodingPolicy) -> bool:
        del policy
        if not psutil.pid_exists(intent.owner_pid):
            return False
        # Legacy 1.0 intents did not persist process creation time. A currently
        # occupied PID is therefore ambiguous and must remain non-destructive;
        # recovery can proceed only after that PID is absent.
        if intent.owner_create_time_ns is None:
            return True
        try:
            return (
                _process_create_time_ns(intent.owner_pid)
                == intent.owner_create_time_ns
            )
        except WorktreeError:
            # Access-denied/transient identity failures are not proof that the
            # owner died. Preserve the intent rather than deleting live state.
            return True

    def creation_intent_status(self) -> dict[str, int | bool]:
        prepared = 0
        branch_reserved = 0
        added = 0
        live = 0
        stale = 0
        invalid = 0
        for entry in sorted(
            self.create_intents_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
            if not entry.is_file() or not entry.name.endswith(".json"):
                invalid += 1
                continue
            task_id = entry.name[:-5]
            if _TASK_ID.fullmatch(task_id) is None:
                invalid += 1
                continue
            try:
                intent = self._load_create_intent(task_id)
            except (OSError, ValueError, WorktreeError):
                invalid += 1
                continue
            if intent is None:
                invalid += 1
                continue
            if intent.phase == "prepared":
                prepared += 1
            elif intent.phase == "branch_reserved":
                branch_reserved += 1
            else:
                added += 1
            if self._intent_is_live(intent, self.policy):
                live += 1
            else:
                stale += 1
        pending = prepared + branch_reserved + added
        return {
            "pending": pending,
            "prepared": prepared,
            "branch_reserved": branch_reserved,
            "added": added,
            "live": live,
            "stale": stale,
            "invalid": invalid,
            "requires_recovery": stale > 0 or invalid > 0,
        }

    @staticmethod
    def _branch_commit(source: Path, branch: str) -> str | None:
        probe = run_git(
            source,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
            max_output_bytes=16_384,
        )
        if probe.returncode == 1:
            return None
        if probe.returncode != 0:
            raise WorktreeError("failed to inspect intent branch")
        result = run_git(
            source,
            ["rev-parse", "--verify", f"refs/heads/{branch}"],
            max_output_bytes=16_384,
        )
        try:
            commit = result.stdout.decode("ascii", errors="strict").strip().casefold()
        except UnicodeDecodeError as exc:
            raise WorktreeError("intent branch object id is invalid") from exc
        if _OBJECT_ID.fullmatch(commit) is None:
            raise WorktreeError("intent branch object id is invalid")
        return commit

    @staticmethod
    def _reservation_message(intent: _WorktreeCreateIntent) -> str:
        return f"local-agent-create:{intent.owner_token_hash}"

    def _branch_reservation_owned(
        self,
        source: Path,
        intent: _WorktreeCreateIntent,
    ) -> bool:
        if self._branch_commit(source, intent.branch) != intent.base_commit:
            return False
        result = run_git(
            source,
            [
                "reflog",
                "show",
                "--format=%H%x00%gs",
                "-1",
                f"refs/heads/{intent.branch}",
            ],
            check=False,
            max_output_bytes=16_384,
        )
        if result.returncode != 0:
            return False
        try:
            commit_raw, message_raw = result.stdout.rstrip(b"\r\n").split(b"\x00", 1)
            commit = commit_raw.decode("ascii", errors="strict").casefold()
            message = message_raw.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
        return (
            commit == intent.base_commit
            and message == self._reservation_message(intent)
        )

    def _reserve_intent_branch(
        self,
        source: Path,
        intent: _WorktreeCreateIntent,
    ) -> None:
        validated_source, _common = self._intent_source(intent)
        if validated_source != source:
            raise WorktreeError("create intent source changed before branch reservation")
        zero_object = "0" * len(intent.base_commit)
        reserved = run_git(
            source,
            [
                "update-ref",
                "--create-reflog",
                "-m",
                self._reservation_message(intent),
                f"refs/heads/{intent.branch}",
                intent.base_commit,
                zero_object,
            ],
            check=False,
            max_output_bytes=16_384,
            mutation=True,
        )
        if reserved.returncode != 0:
            raise _BranchReservationCollision("task branch reservation collided")
        if not self._branch_reservation_owned(source, intent):
            raise WorktreeError("failed to reserve the exact task branch")

    def _intent_source(self, intent: _WorktreeCreateIntent) -> tuple[Path, Path]:
        source = _canonical(Path(intent.source_repository))
        if os.path.normcase(str(source)) != os.path.normcase(
            os.path.normpath(intent.source_repository)
        ):
            raise WorktreeError("create intent source repository changed")
        validate_git_scope(source)
        validate_coding_git_config(source)
        _source_git_dir, source_common = _reported_git_directories(source)
        expected_common = _canonical(Path(intent.source_git_common_dir))
        if source_common != expected_common:
            raise WorktreeError("create intent source Git identity changed")
        return source, source_common

    def _observe_intent_worktree(
        self,
        intent: _WorktreeCreateIntent,
    ) -> _ObservedWorktree | None:
        source, source_common = self._intent_source(intent)
        del source
        raw_path = Path(intent.worktree_path)
        root = self.owned_worktree_root.resolve(strict=True)
        try:
            normalized = raw_path.resolve(strict=False)
        except OSError as exc:
            raise WorktreeError("create intent worktree path is unavailable") from exc
        if not _inside(root, normalized):
            raise WorktreeError("create intent worktree escapes the owned root")
        if not os.path.lexists(raw_path):
            return None
        try:
            info = raw_path.lstat()
        except OSError as exc:
            raise WorktreeError("create intent worktree is unreadable") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(info.st_mode)
            or raw_path.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise WorktreeError("create intent path is not an exact owned directory")
        path = _canonical(raw_path)
        if path != normalized or not _inside(root, path):
            raise WorktreeError("create intent worktree canonical identity changed")
        git_dir, common_dir, marker_sha256 = _linked_git_identity(path)
        if (
            common_dir != source_common
            or common_dir.name.casefold() != ".git"
            or git_dir.parent != common_dir / "worktrees"
        ):
            raise WorktreeError("create intent worktree is outside the source metadata graph")
        symbolic_result = run_git(
            path,
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            max_output_bytes=16_384,
        )
        if symbolic_result.returncode != 0:
            raise WorktreeError("create intent worktree has no owned symbolic branch")
        try:
            symbolic = symbolic_result.stdout.decode("utf-8", errors="strict").strip()
            head = (
                run_git(path, ["rev-parse", "--verify", "HEAD"], max_output_bytes=16_384)
                .stdout.decode("ascii", errors="strict")
                .strip()
                .casefold()
            )
        except UnicodeDecodeError as exc:
            raise WorktreeError("create intent worktree Git identity is invalid") from exc
        if symbolic != f"refs/heads/{intent.branch}" or _OBJECT_ID.fullmatch(head) is None:
            raise WorktreeError("create intent worktree branch identity changed")
        branch_commit = self._branch_commit(path, intent.branch)
        clean = not git_status_paths(path) and not git_ignored_paths(path)
        return _ObservedWorktree(
            path=path,
            git_dir=git_dir,
            git_common_dir=common_dir,
            git_marker_sha256=marker_sha256,
            head=head,
            branch_commit=branch_commit,
            clean=clean,
        )

    def _validate_created_worktree(
        self,
        intent: _WorktreeCreateIntent,
    ) -> _ObservedWorktree:
        observed = self._observe_intent_worktree(intent)
        if observed is None:
            raise WorktreeError("Git did not create the intended worktree")
        if (
            observed.head != intent.base_commit
            or observed.branch_commit != intent.base_commit
            or not observed.clean
        ):
            raise WorktreeError("Git created a non-pristine task worktree")
        return observed

    @staticmethod
    def _intent_matches_observation(
        intent: _WorktreeCreateIntent,
        observed: _ObservedWorktree,
    ) -> bool:
        if intent.phase in {"prepared", "branch_reserved"}:
            return True
        return (
            intent.git_dir == str(observed.git_dir)
            and intent.git_common_dir == str(observed.git_common_dir)
            and intent.git_marker_sha256 == observed.git_marker_sha256
        )

    @staticmethod
    def _record_matches_intent(
        record: WorktreeRecordV1,
        intent: _WorktreeCreateIntent,
        observed: _ObservedWorktree,
    ) -> bool:
        return (
            record.task_id == intent.task_id
            and record.source_repository == intent.source_repository
            and record.worktree_path == intent.worktree_path
            and record.branch == intent.branch
            and record.base_commit == intent.base_commit
            and record.owner_token_hash == intent.owner_token_hash
            and record.git_dir == str(observed.git_dir)
            and record.git_common_dir == str(observed.git_common_dir)
            and record.git_marker_sha256 == observed.git_marker_sha256
        )

    @staticmethod
    def _orphan_record(
        intent: _WorktreeCreateIntent,
        observed: _ObservedWorktree,
    ) -> WorktreeRecordV1:
        now = _utc_now()
        return WorktreeRecordV1(
            task_id=intent.task_id,
            source_repository=intent.source_repository,
            worktree_path=str(observed.path),
            branch=intent.branch,
            git_dir=str(observed.git_dir),
            git_common_dir=str(observed.git_common_dir),
            git_marker_sha256=observed.git_marker_sha256,
            base_commit=intent.base_commit,
            owner_token_hash=intent.owner_token_hash,
            status="orphaned",
            owner_pid=intent.owner_pid,
            created_at=intent.created_at,
            heartbeat_at=now,
        )

    def _cas_delete_intent_branch(
        self,
        source: Path,
        intent: _WorktreeCreateIntent,
    ) -> bool:
        validated_source, _common = self._intent_source(intent)
        if validated_source != source:
            raise WorktreeError("create intent source changed before branch compensation")
        current = self._branch_commit(source, intent.branch)
        if current is None:
            return True
        if current != intent.base_commit or not self._branch_reservation_owned(
            source, intent
        ):
            return False
        validated_source, _common = self._intent_source(intent)
        if validated_source != source:
            raise WorktreeError("create intent source changed before branch compensation")
        deleted = run_git(
            source,
            [
                "update-ref",
                "-d",
                f"refs/heads/{intent.branch}",
                intent.base_commit,
            ],
            check=False,
            max_output_bytes=16_384,
            mutation=True,
        )
        return deleted.returncode == 0 and self._branch_commit(source, intent.branch) is None

    def _reconcile_create_intent(
        self,
        intent: _WorktreeCreateIntent,
        *,
        include_live: bool,
    ) -> tuple[str, WorktreeRecordV1 | None]:
        if not include_live and self._intent_is_live(intent, self.policy):
            return "live", None
        source, _source_common = self._intent_source(intent)
        record = self.load(intent.task_id)
        observed = self._observe_intent_worktree(intent)
        branch_commit = self._branch_commit(source, intent.branch)
        reservation_owned = (
            branch_commit is not None
            and self._branch_reservation_owned(source, intent)
        )

        if observed is None:
            if record is not None:
                return "unresolved", None
            if branch_commit is not None:
                if not reservation_owned:
                    return "unresolved", None
                if intent.phase == "prepared":
                    reserved_intent = replace(
                        intent,
                        phase="branch_reserved",
                        updated_at=_utc_now(),
                    )
                    self._write_create_intent(reserved_intent, previous=intent)
                    intent = reserved_intent
                if not self._cas_delete_intent_branch(source, intent):
                    return "unresolved", None
            self._remove_create_intent(intent)
            return "compensated", None

        if not self._intent_matches_observation(intent, observed):
            return "unresolved", None
        if intent.phase == "prepared":
            if not reservation_owned:
                return "unresolved", None
            reserved_intent = replace(
                intent,
                phase="branch_reserved",
                updated_at=_utc_now(),
            )
            self._write_create_intent(reserved_intent, previous=intent)
            intent = reserved_intent
        if intent.phase == "branch_reserved":
            proven = replace(
                intent,
                phase="added",
                git_dir=str(observed.git_dir),
                git_common_dir=str(observed.git_common_dir),
                git_marker_sha256=observed.git_marker_sha256,
                updated_at=_utc_now(),
            )
            self._write_create_intent(proven, previous=intent)
            intent = proven
        if record is not None:
            if not self._record_matches_intent(record, intent, observed):
                return "unresolved", None
            self._remove_create_intent(intent)
            return "finalized", record

        if (
            reservation_owned
            and
            observed.clean
            and observed.head == intent.base_commit
            and observed.branch_commit == intent.base_commit
        ):
            validated_source, _common = self._intent_source(intent)
            if validated_source != source:
                return "unresolved", None
            removed = run_git(
                source,
                ["worktree", "remove", str(observed.path)],
                timeout=120,
                check=False,
                mutation=True,
            )
            if removed.returncode != 0 or os.path.lexists(observed.path):
                return "unresolved", None
            if not self._cas_delete_intent_branch(source, intent):
                return "unresolved", None
            self._remove_create_intent(intent)
            return "compensated_worktree", None

        orphaned = self._orphan_record(intent, observed)
        self._write_record(orphaned, expected_absent=True)
        if self.load(intent.task_id) != orphaned:
            return "unresolved", None
        self._remove_create_intent(intent)
        return "orphaned", orphaned

    def recover_creation_intents(self) -> CreateIntentRecovery:
        compensated = 0
        paths_deleted = 0
        finalized = 0
        orphaned: list[WorktreeRecordV1] = []
        unresolved = 0
        invalid = 0
        live = 0
        for entry in sorted(
            self.create_intents_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
            if not entry.is_file() or not entry.name.endswith(".json"):
                invalid += 1
                continue
            task_id = entry.name[:-5]
            if _TASK_ID.fullmatch(task_id) is None:
                invalid += 1
                continue
            try:
                intent = self._load_create_intent(task_id)
                if intent is None:
                    invalid += 1
                    continue
                action, record = self._reconcile_create_intent(
                    intent,
                    include_live=False,
                )
            except (CodingRepositoryError, RepositoryError, WorktreeError, OSError, ValueError):
                unresolved += 1
                continue
            if action in {"compensated", "compensated_worktree"}:
                compensated += 1
                paths_deleted += int(action == "compensated_worktree")
            elif action == "finalized":
                finalized += 1
            elif action == "orphaned" and record is not None:
                orphaned.append(record)
            elif action == "live":
                live += 1
            else:
                unresolved += 1
        return CreateIntentRecovery(
            compensated=compensated,
            paths_deleted=paths_deleted,
            finalized=finalized,
            orphaned_records=tuple(orphaned),
            unresolved=unresolved,
            invalid=invalid,
            live=live,
        )

    @staticmethod
    def _immutable_identity(record: WorktreeRecordV1) -> tuple[object, ...]:
        return (
            record.task_id,
            record.source_repository,
            record.worktree_path,
            record.branch,
            record.git_dir,
            record.git_common_dir,
            record.git_marker_sha256,
            record.base_commit,
            record.owner_token_hash,
        )

    def validate_owned_git_identity(
        self,
        record: WorktreeRecordV1,
    ) -> WorktreeRecordV1:
        registered = self.load(record.task_id)
        if (
            registered is None
            or registered.status != "active"
            or self._immutable_identity(registered) != self._immutable_identity(record)
        ):
            raise WorktreeError("active worktree registry identity changed")
        if (
            registered.branch is None
            or registered.git_dir is None
            or registered.git_common_dir is None
            or registered.git_marker_sha256 is None
        ):
            raise WorktreeError("active worktree lacks bound Git metadata identity")
        root = self.owned_worktree_root.resolve(strict=True)
        path = _canonical(Path(registered.worktree_path))
        if not _inside(root, path):
            raise WorktreeError("active owned worktree escapes the owned root")
        source = _canonical(Path(registered.source_repository))
        validate_git_scope(source)
        validate_coding_git_config(source)
        _source_git_dir, source_common = _reported_git_directories(source)
        git_dir, common_dir, marker_sha256 = _linked_git_identity(path)
        expected_git_dir = _canonical(Path(registered.git_dir))
        expected_common_dir = _canonical(Path(registered.git_common_dir))
        if (
            common_dir.name.casefold() != ".git"
            or source_common != expected_common_dir
            or common_dir != expected_common_dir
            or git_dir != expected_git_dir
            or git_dir.parent != common_dir / "worktrees"
            or marker_sha256 != registered.git_marker_sha256
        ):
            raise WorktreeError(
                "owned worktree no longer matches its registered linked Git metadata"
            )
        symbolic = run_git(
            path,
            ["symbolic-ref", "--quiet", "HEAD"],
            max_output_bytes=16_384,
        ).stdout.decode("utf-8", errors="strict").strip()
        if symbolic != f"refs/heads/{registered.branch}":
            raise WorktreeError(
                "owned worktree symbolic HEAD no longer matches its registered branch"
            )
        return registered

    def validate_owned_path(self, path: Path) -> WorktreeRecordV1:
        canonical = _canonical(path)
        matches: list[WorktreeRecordV1] = []
        for entry in sorted(self.records_dir.glob("*.json")):
            record = self.load(entry.stem)
            if (
                record is not None
                and record.status == "active"
                and _canonical(Path(record.worktree_path)) == canonical
            ):
                matches.append(record)
        if len(matches) != 1:
            raise WorktreeError(
                "task worktree is not uniquely bound to an active registry record"
            )
        return self.validate_owned_git_identity(matches[0])

    def active_owned_branch_refs(self) -> tuple[str, ...]:
        """Return exact active branch refs with filesystem-registry proof.

        Namespace prefixes are never ownership evidence. Each exclusion must
        correspond to a valid active record whose canonical owned worktree is
        currently checked out on that exact branch.
        """

        refs: set[str] = set()
        for entry in sorted(self.records_dir.glob("*.json")):
            task_id = entry.stem
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", task_id):
                raise WorktreeError("invalid worktree registry entry name")
            record = self.load(task_id)
            if record is None or record.status != "active":
                continue
            record = self.validate_owned_git_identity(record)
            if record.branch is None:
                raise WorktreeError("active owned worktree has no branch")
            expected = f"refs/heads/{record.branch}"
            refs.add(expected)

        # A create intent owns a branch before the active registry record is
        # finalized.  Include that short-lived ref only when the exact CAS
        # reservation is still proven by both its object id and its unique
        # reflog message.  A merely matching namespace/name/base is never
        # ownership evidence.  ``prepared`` is considered as well because a
        # process can die after update-ref succeeds but before it journals the
        # ``branch_reserved`` phase.
        for entry in sorted(self.create_intents_dir.glob("*.json")):
            task_id = entry.stem
            if _TASK_ID.fullmatch(task_id) is None:
                raise WorktreeError("invalid worktree create-intent entry name")
            intent = self._load_create_intent(task_id)
            if intent is None:
                raise WorktreeError("worktree create intent disappeared during ownership scan")
            source, _common = self._intent_source(intent)
            reservation_owned = self._branch_reservation_owned(source, intent)
            if intent.phase == "prepared" and not reservation_owned:
                continue
            if not reservation_owned:
                raise WorktreeError("worktree create intent has no exact branch reservation proof")
            refs.add(f"refs/heads/{intent.branch}")
        return tuple(sorted(refs))

    def _safe_branch(self, task_id: str, repository: RepositoryIdentity) -> str:
        validate_git_scope(repository.canonical_root)
        validate_coding_git_config(repository.canonical_root)
        safe_id = re.sub(r"[^a-z0-9-]+", "-", task_id.casefold()).strip("-")[:32] or "task"
        digest = sha256(f"{repository.canonical_root}\x00{task_id}".encode("utf-8")).hexdigest()[:10]
        base = f"{self.policy.branch_prefix}{safe_id}-{digest}"
        for index in range(100):
            candidate = base if index == 0 else f"{base}-{index}"
            result = run_git(
                repository.canonical_root,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
                check=False,
                max_output_bytes=16_384,
            )
            if result.returncode == 1:
                return candidate
            if result.returncode not in {0, 1}:
                raise WorktreeError("failed to check branch collision")
        raise WorktreeError("could not allocate a collision-safe task branch")

    def create(self, *, task_id: str, repository: RepositoryIdentity) -> WorktreeRecordV1:
        if self.load(task_id) is not None:
            raise WorktreeError("task already has a registered worktree")
        if self._load_create_intent(task_id) is not None:
            raise WorktreeError("task already has a pending create intent")
        try:
            validated_source = validate_git_scope(repository.canonical_root)
            if _canonical(validated_source) != repository.canonical_root:
                raise WorktreeError("repository identity changed before worktree creation")
            validate_coding_git_config(repository.canonical_root)
            _source_git_dir, source_common = _reported_git_directories(
                repository.canonical_root
            )
        except (CodingRepositoryError, RepositoryError, WorktreeError) as exc:
            raise WorktreeError("failed to create a validated task worktree") from exc
        source_hash = sha256(str(repository.canonical_root).casefold().encode("utf-8")).hexdigest()[:16]
        task_hash = sha256(task_id.encode("utf-8")).hexdigest()[:12]
        parent = self.owned_worktree_root / source_hash
        parent.mkdir(parents=True, exist_ok=True)
        path = (parent / f"task-{task_hash}").resolve(strict=False)
        if os.path.lexists(path):
            raise WorktreeError("owned task path already exists without a registry record")
        branch = self._safe_branch(task_id, repository)
        token_hash = sha256(secrets.token_bytes(32)).hexdigest()
        owner_pid = os.getpid()
        owner_create_time_ns = _process_create_time_ns(owner_pid)
        now = _utc_now()
        intent = _WorktreeCreateIntent(
            task_id=task_id,
            source_repository=str(repository.canonical_root),
            source_git_common_dir=str(source_common),
            worktree_path=str(path),
            branch=branch,
            base_commit=repository.base_commit,
            owner_token_hash=token_hash,
            owner_pid=owner_pid,
            created_at=now,
            updated_at=now,
            owner_create_time_ns=owner_create_time_ns,
        )
        self._write_create_intent(intent, previous=None)
        mutation_started = False
        try:
            # Re-check both collision preconditions after the intent is durable.
            # If a peer won either race, this invocation never claims or removes
            # the peer's path/ref.
            self._intent_source(intent)
            if os.path.lexists(path) or self._branch_commit(
                repository.canonical_root, branch
            ) is not None:
                raise WorktreeError("task worktree collision appeared after intent creation")
            self._intent_source(intent)
            mutation_started = True
            self._reserve_intent_branch(repository.canonical_root, intent)
            reserved_intent = replace(
                intent,
                phase="branch_reserved",
                updated_at=_utc_now(),
            )
            self._write_create_intent(reserved_intent, previous=intent)
            intent = reserved_intent
            self._intent_source(intent)
            run_git(
                repository.canonical_root,
                ["worktree", "add", str(path), branch],
                timeout=120,
                mutation=True,
            )
            observed = self._validate_created_worktree(intent)
            added_intent = replace(
                intent,
                phase="added",
                git_dir=str(observed.git_dir),
                git_common_dir=str(observed.git_common_dir),
                git_marker_sha256=observed.git_marker_sha256,
                updated_at=_utc_now(),
            )
            self._write_create_intent(added_intent, previous=intent)
            intent = added_intent
            record = WorktreeRecordV1(
                task_id=task_id,
                source_repository=str(repository.canonical_root),
                worktree_path=str(observed.path),
                branch=branch,
                git_dir=str(observed.git_dir),
                git_common_dir=str(observed.git_common_dir),
                git_marker_sha256=observed.git_marker_sha256,
                base_commit=repository.base_commit,
                owner_token_hash=token_hash,
                status="active",
                owner_pid=os.getpid(),
                created_at=now,
                heartbeat_at=now,
            )
            self._write_record(record, expected_absent=True)
            self._remove_create_intent(intent)
            return record
        except Exception as exc:
            if not mutation_started:
                try:
                    self._remove_create_intent(intent)
                except Exception:
                    pass
                raise WorktreeError("failed to create a validated task worktree") from exc
            if isinstance(exc, _BranchReservationCollision):
                try:
                    current_intent = self._load_create_intent(task_id)
                    if current_intent is not None:
                        self._remove_create_intent(current_intent)
                except Exception:
                    pass
                raise WorktreeError("failed to create a validated task worktree") from exc
            try:
                current_intent = self._load_create_intent(task_id)
                if current_intent is not None:
                    action, recovered_record = self._reconcile_create_intent(
                        current_intent,
                        include_live=True,
                    )
                    if action == "finalized" and recovered_record is not None:
                        return recovered_record
            except Exception:
                # The durable intent remains the ownership evidence. Recovery
                # may preserve it as an orphan, but never guesses a path/ref.
                pass
            raise WorktreeError("failed to create a validated task worktree") from exc

    def _lease_path_for_record(self, record: WorktreeRecordV1) -> tuple[Path, Path]:
        canonical = _canonical(Path(record.worktree_path))
        if not _inside(self.owned_worktree_root.resolve(strict=True), canonical):
            raise WorktreeError("registered worktree escapes the owned root")
        key = sha256(str(canonical).casefold().encode("utf-8")).hexdigest()
        return canonical, self.leases_dir / f"{key}.lease.json"

    def _has_live_lease(self, record: WorktreeRecordV1) -> bool:
        try:
            canonical, lease_path = self._lease_path_for_record(record)
            payload = _read_json(lease_path, maximum=32 * 1024)
            if set(payload) != {
                "schema_version",
                "task_id",
                "worktree_path",
                "owner_pid",
                "owner_token_hash",
                "heartbeat_at",
            }:
                return False
            owner_pid = payload.get("owner_pid")
            owner_hash = payload.get("owner_token_hash")
            reported_path = payload.get("worktree_path")
            if (
                payload.get("schema_version") != "1.0"
                or payload.get("task_id") != record.task_id
                or isinstance(owner_pid, bool)
                or not isinstance(owner_pid, int)
                or owner_pid < 1
                or not isinstance(owner_hash, str)
                or _HASH.fullmatch(owner_hash) is None
                or not isinstance(reported_path, str)
                or os.path.normcase(os.path.normpath(reported_path))
                != os.path.normcase(os.path.normpath(str(canonical)))
            ):
                return False
            heartbeat = _parse_aware_timestamp(
                payload.get("heartbeat_at"),
                label="worktree lease heartbeat",
            )
            age = (_utc_now() - heartbeat).total_seconds()
            allowed_clock_skew = max(5.0, self.policy.lease_heartbeat_seconds * 2.0)
            return (
                -allowed_clock_skew <= age <= self.policy.lease_stale_seconds
                and psutil.pid_exists(owner_pid)
            )
        except (OSError, ValueError, WorktreeError):
            return False

    def lease(
        self,
        record: WorktreeRecordV1,
        *,
        timeout_seconds: float | None = None,
    ) -> WorktreeLease:
        canonical, lease_path = self._lease_path_for_record(record)
        if self.load(record.task_id) != record:
            raise WorktreeError("worktree registry changed before lease acquisition")

        def update_heartbeat(timestamp: datetime) -> None:
            with self._record_update_lock(record.task_id):
                current = self.load(record.task_id)
                if (
                    current is None
                    or current.owner_token_hash != record.owner_token_hash
                    or current.status != "active"
                ):
                    return
                self._write_record_unlocked(
                    current.model_copy(
                        update={
                            "heartbeat_at": timestamp,
                            "owner_pid": os.getpid(),
                        }
                    )
                )

        return WorktreeLease(
            lease_path=lease_path,
            canonical_worktree=canonical,
            task_id=record.task_id,
            policy=self.policy,
            timeout_seconds=timeout_seconds or self.policy.lease_acquire_timeout_seconds,
            on_heartbeat=update_heartbeat,
        )

    def complete(self, task_id: str) -> WorktreeRecordV1:
        with self._record_update_lock(task_id):
            record = self.load(task_id)
            if record is None:
                raise WorktreeError("worktree is not registered")
            now = _utc_now()
            updated = record.model_copy(
                update={
                    "status": "complete",
                    "completed_at": now,
                    "heartbeat_at": now,
                }
            )
            self._write_record_unlocked(updated)
            return updated

    def mark_orphaned(self, task_id: str) -> WorktreeRecordV1:
        """Preserve an owned task worktree after cancellation or an unexpected failure."""

        with self._record_update_lock(task_id):
            record = self.load(task_id)
            if record is None:
                raise WorktreeError("worktree is not registered")
            if record.status not in {"active", "complete", "orphaned"}:
                raise WorktreeError(
                    "only an active, completing, or orphaned owned worktree can be marked orphaned"
                )
            updated = record.model_copy(
                update={"status": "orphaned", "heartbeat_at": _utc_now()}
            )
            self._write_record_unlocked(updated)
            return updated

    def recover_orphans(self) -> list[WorktreeRecordV1]:
        recovered: list[WorktreeRecordV1] = []
        for path in sorted(self.records_dir.glob("*.json")):
            task_id = path.stem
            if _TASK_ID.fullmatch(task_id) is None:
                continue
            try:
                with self._record_update_lock(task_id):
                    record = self.load(task_id)
                    if record is None or record.status != "active":
                        continue
                    # The lease file is the cross-process liveness authority.
                    # Its heartbeat is written before the registry callback,
                    # so recovery cannot orphan a live executor solely because
                    # it observed an older registry heartbeat.
                    if self._has_live_lease(record):
                        continue
                    age = (_utc_now() - record.heartbeat_at).total_seconds()
                    alive = psutil.pid_exists(record.owner_pid)
                    if alive and age <= self.policy.lease_stale_seconds:
                        continue
                    orphaned = record.model_copy(
                        update={"status": "orphaned", "heartbeat_at": _utc_now()}
                    )
                    self._write_record_unlocked(orphaned)
                    recovered.append(orphaned)
            except Exception:
                continue
        return recovered

    def cleanup(self, task_id: str) -> WorktreeRecordV1:
        record = self.load(task_id)
        if record is None:
            raise WorktreeError("worktree is not registered")
        if record.status != "complete":
            raise WorktreeError("only an owned completed worktree can be cleaned")
        source = resolve_repository(record.source_repository)
        path = _canonical(Path(record.worktree_path))
        root = self.owned_worktree_root.resolve(strict=True)
        if not _inside(root, path):
            raise WorktreeError("registered worktree is outside the owned root")
        validate_git_scope(path)
        if git_status_paths(path):
            blocked = record.model_copy(update={"status": "cleanup_blocked", "heartbeat_at": _utc_now()})
            self._write_record(blocked)
            return blocked
        run_git(
            source.canonical_root,
            ["worktree", "remove", str(path)],
            timeout=120,
            mutation=True,
        )
        if path.exists():
            raise WorktreeError("Git reported cleanup but owned worktree still exists")
        removed = record.model_copy(update={"status": "removed", "heartbeat_at": _utc_now()})
        self._write_record(removed)
        return removed
