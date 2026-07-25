from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from services.coding.contracts import (
    CodingTaskStateV1,
    CodingTaskStatus,
    WorktreeRecordV1,
)
from services.coding.migrations import (
    migrate_coding_database,
    open_coding_database,
    verify_coding_database,
)
from services.common import ROOT


DEFAULT_DATABASE = ROOT / "data" / "coding.sqlite3"
_EVENT_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class CodingStoreError(RuntimeError):
    """Base failure at the durable Coding Engine boundary."""


class TaskAlreadyExistsError(CodingStoreError):
    pass


class TaskNotFoundError(CodingStoreError):
    pass


class ConcurrentTransitionError(CodingStoreError):
    pass


class InvalidTransitionError(CodingStoreError):
    pass


class WorktreeRegistryError(CodingStoreError):
    pass


_ALLOWED_TRANSITIONS: dict[CodingTaskStatus, frozenset[CodingTaskStatus]] = {
    CodingTaskStatus.CREATED: frozenset(
        {
            CodingTaskStatus.INSPECTED,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
        }
    ),
    CodingTaskStatus.INSPECTED: frozenset(
        {
            CodingTaskStatus.INSPECTED,
            CodingTaskStatus.PLANNED,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
        }
    ),
    CodingTaskStatus.PLANNED: frozenset(
        {
            CodingTaskStatus.PLANNED,
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
        }
    ),
    CodingTaskStatus.ISOLATED: frozenset(
        {
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.ORPHANED,
        }
    ),
    CodingTaskStatus.EXECUTING: frozenset(
        {
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.VERIFYING,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.ORPHANED,
        }
    ),
    CodingTaskStatus.VERIFYING: frozenset(
        {
            CodingTaskStatus.VERIFYING,
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.REVIEWING,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.ORPHANED,
        }
    ),
    CodingTaskStatus.REVIEWING: frozenset(
        {
            CodingTaskStatus.REVIEWING,
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.COMPLETED,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.ORPHANED,
        }
    ),
    CodingTaskStatus.HANDOFF_READY: frozenset(
        {
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.ORPHANED,
        }
    ),
    CodingTaskStatus.BLOCKED: frozenset(
        {
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.PLANNED,
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
        }
    ),
    CodingTaskStatus.ORPHANED: frozenset(
        {
            CodingTaskStatus.ORPHANED,
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.HANDOFF_READY,
            CodingTaskStatus.BLOCKED,
            CodingTaskStatus.FAILED,
            CodingTaskStatus.CANCELLED,
        }
    ),
    CodingTaskStatus.COMPLETED: frozenset(),
    CodingTaskStatus.FAILED: frozenset(),
    CodingTaskStatus.CANCELLED: frozenset(),
}

_WORKTREE_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"active", "complete", "orphaned", "cleanup_blocked"}),
    "orphaned": frozenset(
        {"orphaned", "active", "complete", "cleanup_blocked", "removed"}
    ),
    # complete->orphaned is a compensation/recovery edge only: finalizing the
    # external registry and durable task snapshot cannot be one atomic write.
    # A failure between those boundaries must preserve the owned worktree and
    # restore a consistent, non-cleanable state.
    "complete": frozenset({"complete", "orphaned", "cleanup_blocked", "removed"}),
    "cleanup_blocked": frozenset(
        {"cleanup_blocked", "orphaned", "complete", "removed"}
    ),
    "removed": frozenset(),
}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> str:
    return _utc_text(datetime.now(timezone.utc))


def _canonical_json(model: CodingTaskStateV1 | WorktreeRecordV1) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _branch_key(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


def _validate_event_code(value: str, *, name: str, maximum: int) -> str:
    if len(value) > maximum or not _EVENT_CODE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded dotted identifier")
    return value


def _validate_worktree_record(record: WorktreeRecordV1) -> None:
    if record.heartbeat_at < record.created_at:
        raise WorktreeRegistryError("worktree heartbeat precedes creation")
    if record.status == "active" and record.completed_at is not None:
        raise WorktreeRegistryError("active worktree cannot have a completion time")
    if record.status in {"complete", "removed"} and record.completed_at is None:
        raise WorktreeRegistryError("terminal worktree status requires completion time")
    if record.completed_at is not None:
        if record.completed_at < record.created_at:
            raise WorktreeRegistryError("worktree completion precedes creation")
        if record.completed_at > record.heartbeat_at:
            raise WorktreeRegistryError("worktree completion exceeds its heartbeat")


def _load_state_json(payload: str, expected_hash: str) -> CodingTaskStateV1:
    if _sha256(payload) != expected_hash:
        raise CodingStoreError("coding task snapshot hash mismatch")
    try:
        return CodingTaskStateV1.model_validate_json(payload)
    except ValidationError as exc:
        raise CodingStoreError("stored coding task snapshot is invalid") from exc


def _load_worktree_json(payload: str, expected_hash: str) -> WorktreeRecordV1:
    if _sha256(payload) != expected_hash:
        raise WorktreeRegistryError("stored worktree record hash mismatch")
    try:
        record = WorktreeRecordV1.model_validate_json(payload)
    except ValidationError as exc:
        raise WorktreeRegistryError("stored worktree record is invalid") from exc
    _validate_worktree_record(record)
    return record


class CodingTaskStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or DEFAULT_DATABASE).resolve()
        migrate_coding_database(self.database_path)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(open_coding_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(
            open_coding_database(self.database_path, read_only=True)
        ) as connection:
            yield connection

    @staticmethod
    def _ensure_state_identity(state: CodingTaskStateV1) -> str:
        task_id = state.request.task_id
        if state.worktree is not None and state.worktree.task_id != task_id:
            raise CodingStoreError("task snapshot references another task worktree")
        return task_id

    @staticmethod
    def _ensure_registered_worktree(
        connection: sqlite3.Connection,
        state: CodingTaskStateV1,
    ) -> None:
        record = state.worktree
        if record is None:
            return
        row = connection.execute(
            "SELECT record_json,record_sha256,status FROM coding_worktrees WHERE task_id=?",
            (state.request.task_id,),
        ).fetchone()
        if row is None:
            raise WorktreeRegistryError("task snapshot worktree is not registered")
        registered = _load_worktree_json(
            str(row["record_json"]), str(row["record_sha256"])
        )
        identity = (
            "source_repository",
            "worktree_path",
            "branch",
            "base_commit",
            "owner_token_hash",
        )
        if any(getattr(record, field) != getattr(registered, field) for field in identity):
            raise WorktreeRegistryError("task snapshot worktree identity has changed")
        if str(row["status"]) == "removed" and state.status not in {
            CodingTaskStatus.COMPLETED,
            CodingTaskStatus.CANCELLED,
            CodingTaskStatus.FAILED,
        }:
            raise WorktreeRegistryError("active task cannot reference a removed worktree")

    def create(self, state: CodingTaskStateV1) -> int:
        task_id = self._ensure_state_identity(state)
        if state.status is not CodingTaskStatus.CREATED:
            raise InvalidTransitionError("new coding task must start in created state")
        if state.worktree is not None:
            raise InvalidTransitionError("created coding task cannot already own a worktree")
        payload = _canonical_json(state)
        digest = _sha256(payload)
        try:
            with self._write() as connection:
                connection.execute(
                    "INSERT INTO coding_tasks("
                    "task_id,request_id,status,state_version,state_json,state_sha256,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        state.request.request_id,
                        state.status.value,
                        1,
                        payload,
                        digest,
                        _utc_text(state.created_at),
                        _utc_text(state.updated_at),
                    ),
                )
                connection.execute(
                    "INSERT INTO coding_task_events("
                    "task_id,state_version,event_type,from_status,to_status,reason_code,state_sha256,occurred_at"
                    ") VALUES(?,1,'task.created',NULL,?,NULL,?,?)",
                    (task_id, state.status.value, digest, _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise TaskAlreadyExistsError("coding task already exists") from exc
        return 1

    def load(self, task_id: str) -> CodingTaskStateV1 | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT state_json,state_sha256 FROM coding_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return _load_state_json(str(row["state_json"]), str(row["state_sha256"]))

    def version(self, task_id: str) -> int:
        with self._read() as connection:
            row = connection.execute(
                "SELECT state_version FROM coding_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TaskNotFoundError("coding task does not exist")
        return int(row["state_version"])

    def transition(
        self,
        state: CodingTaskStateV1,
        event_type: str,
        reason_code: str | None = None,
        expected_version: int | None = None,
    ) -> int:
        task_id = self._ensure_state_identity(state)
        event_type = _validate_event_code(
            event_type, name="event_type", maximum=64
        )
        if reason_code is not None:
            reason_code = _validate_event_code(
                reason_code, name="reason_code", maximum=128
            )
        if expected_version is not None and expected_version < 1:
            raise ValueError("expected_version must be positive")
        payload = _canonical_json(state)
        digest = _sha256(payload)

        with self._write() as connection:
            row = connection.execute(
                "SELECT status,state_version,state_json,state_sha256 FROM coding_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TaskNotFoundError("coding task does not exist")
            current_version = int(row["state_version"])
            if expected_version is not None and current_version != expected_version:
                raise ConcurrentTransitionError("coding task state version changed")
            current = _load_state_json(
                str(row["state_json"]), str(row["state_sha256"])
            )
            if current.request != state.request:
                raise InvalidTransitionError("coding task request is immutable")
            if current.source_repository != state.source_repository:
                raise InvalidTransitionError("coding task repository is immutable")
            if current.created_at != state.created_at:
                raise InvalidTransitionError("coding task creation time is immutable")
            if state.updated_at < current.updated_at:
                raise InvalidTransitionError("coding task update time moved backwards")
            if state.status not in _ALLOWED_TRANSITIONS[current.status]:
                raise InvalidTransitionError(
                    f"coding task transition {current.status.value}->{state.status.value} is not allowed"
                )
            self._ensure_registered_worktree(connection, state)

            new_version = current_version + 1
            updated = connection.execute(
                "UPDATE coding_tasks SET status=?,state_version=?,state_json=?,state_sha256=?,updated_at=? "
                "WHERE task_id=? AND state_version=?",
                (
                    state.status.value,
                    new_version,
                    payload,
                    digest,
                    _utc_text(state.updated_at),
                    task_id,
                    current_version,
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrentTransitionError("coding task compare-and-swap failed")
            connection.execute(
                "INSERT INTO coding_task_events("
                "task_id,state_version,event_type,from_status,to_status,reason_code,state_sha256,occurred_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    new_version,
                    event_type,
                    current.status.value,
                    state.status.value,
                    reason_code,
                    digest,
                    _now(),
                ),
            )
        return new_version

    def register_worktree(self, record: WorktreeRecordV1) -> None:
        _validate_worktree_record(record)
        if record.status != "active" or record.completed_at is not None:
            raise WorktreeRegistryError("new worktree must be active and incomplete")
        payload = _canonical_json(record)
        digest = _sha256(payload)
        try:
            with self._write() as connection:
                task = connection.execute(
                    "SELECT state_json,state_sha256 FROM coding_tasks WHERE task_id=?",
                    (record.task_id,),
                ).fetchone()
                if task is None:
                    raise TaskNotFoundError("worktree task does not exist")
                state = _load_state_json(
                    str(task["state_json"]), str(task["state_sha256"])
                )
                if state.source_repository != record.source_repository:
                    raise WorktreeRegistryError(
                        "worktree source repository does not match its task"
                    )
                connection.execute(
                    "INSERT INTO coding_worktrees("
                    "task_id,source_repository,worktree_path,worktree_key,branch,branch_key,"
                    "base_commit,owner_token_hash,status,owner_pid,record_version,record_json,"
                    "record_sha256,created_at,heartbeat_at,completed_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
                    (
                        record.task_id,
                        record.source_repository,
                        record.worktree_path,
                        _path_key(record.worktree_path),
                        record.branch,
                        _branch_key(record.branch),
                        record.base_commit,
                        record.owner_token_hash,
                        record.status,
                        record.owner_pid,
                        payload,
                        digest,
                        _utc_text(record.created_at),
                        _utc_text(record.heartbeat_at),
                        None,
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorktreeRegistryError(
                "worktree task, path, or branch is already registered"
            ) from exc

    def update_worktree(self, record: WorktreeRecordV1) -> None:
        _validate_worktree_record(record)
        payload = _canonical_json(record)
        digest = _sha256(payload)
        with self._write() as connection:
            row = connection.execute(
                "SELECT record_version,record_json,record_sha256 FROM coding_worktrees WHERE task_id=?",
                (record.task_id,),
            ).fetchone()
            if row is None:
                raise WorktreeRegistryError("worktree is not registered")
            current = _load_worktree_json(
                str(row["record_json"]), str(row["record_sha256"])
            )
            immutable = (
                "task_id",
                "source_repository",
                "worktree_path",
                "branch",
                "base_commit",
                "owner_token_hash",
                "created_at",
            )
            if any(getattr(current, field) != getattr(record, field) for field in immutable):
                raise WorktreeRegistryError("worktree identity fields are immutable")
            if record.status not in _WORKTREE_TRANSITIONS[current.status]:
                raise WorktreeRegistryError(
                    f"worktree transition {current.status}->{record.status} is not allowed"
                )
            if record.heartbeat_at < current.heartbeat_at:
                raise WorktreeRegistryError("worktree heartbeat moved backwards")
            if current.completed_at is not None and record.completed_at != current.completed_at:
                raise WorktreeRegistryError("worktree completion time is immutable")
            if record.owner_pid != current.owner_pid and not (
                current.status == "orphaned" and record.status == "active"
            ):
                raise WorktreeRegistryError(
                    "worktree owner PID can change only during orphan recovery"
                )
            current_version = int(row["record_version"])
            try:
                updated = connection.execute(
                    "UPDATE coding_worktrees SET source_repository=?,worktree_path=?,worktree_key=?,"
                    "branch=?,branch_key=?,base_commit=?,owner_token_hash=?,status=?,owner_pid=?,"
                    "record_version=?,record_json=?,record_sha256=?,heartbeat_at=?,completed_at=?,updated_at=? "
                    "WHERE task_id=? AND record_version=?",
                    (
                        record.source_repository,
                        record.worktree_path,
                        _path_key(record.worktree_path),
                        record.branch,
                        _branch_key(record.branch),
                        record.base_commit,
                        record.owner_token_hash,
                        record.status,
                        record.owner_pid,
                        current_version + 1,
                        payload,
                        digest,
                        _utc_text(record.heartbeat_at),
                        _utc_text(record.completed_at) if record.completed_at else None,
                        _now(),
                        record.task_id,
                        current_version,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorktreeRegistryError(
                    "worktree update collides with a live path or branch"
                ) from exc
            if updated.rowcount != 1:
                raise ConcurrentTransitionError("worktree compare-and-swap failed")

    def worktree(self, task_id: str) -> WorktreeRecordV1 | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT record_json,record_sha256 FROM coding_worktrees WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return _load_worktree_json(
            str(row["record_json"]), str(row["record_sha256"])
        )

    def status(self) -> dict[str, object]:
        verification = verify_coding_database(self.database_path)
        with self._read() as connection:
            counts = {
                "tasks": int(
                    connection.execute("SELECT count(*) FROM coding_tasks").fetchone()[0]
                ),
                "events": int(
                    connection.execute("SELECT count(*) FROM coding_task_events").fetchone()[0]
                ),
                "worktrees": int(
                    connection.execute("SELECT count(*) FROM coding_worktrees").fetchone()[0]
                ),
            }
            task_statuses = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,count(*) AS count FROM coding_tasks GROUP BY status"
                )
            }
            worktree_statuses = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,count(*) AS count FROM coding_worktrees GROUP BY status"
                )
            }
        return {
            "schema_version": verification.schema_version,
            "application_id": verification.application_id,
            "integrity_check": verification.integrity_check,
            "foreign_key_violations": verification.foreign_key_violations,
            "journal_mode": verification.journal_mode,
            "secure_delete": verification.secure_delete,
            "event_chain_consistent": verification.event_chain_consistent,
            "counts": counts,
            "task_statuses": task_statuses,
            "worktree_statuses": worktree_statuses,
        }


__all__ = [
    "ConcurrentTransitionError",
    "CodingStoreError",
    "CodingTaskStore",
    "InvalidTransitionError",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "WorktreeRegistryError",
]
