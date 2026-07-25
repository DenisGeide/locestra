from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.memory.migrations import restrict_database_storage


APPLICATION_ID: Final = 0x4C414943  # LAIC: Local Agent - Coding
CURRENT_SCHEMA_VERSION: Final = 1
DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000


class CodingMigrationError(RuntimeError):
    """The coding database does not match the owned schema boundary."""


_SCHEMA = """
CREATE TABLE coding_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE coding_tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'created','inspected','planned','isolated','executing','verifying',
        'reviewing','handoff_ready','completed','blocked','failed','cancelled','orphaned'
    )),
    state_version INTEGER NOT NULL CHECK(state_version >= 1),
    state_json TEXT NOT NULL,
    state_sha256 TEXT NOT NULL CHECK(
        length(state_sha256) = 64 AND state_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE coding_task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES coding_tasks(task_id) ON DELETE RESTRICT,
    state_version INTEGER NOT NULL CHECK(state_version >= 1),
    event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 64),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason_code TEXT CHECK(reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 128),
    state_sha256 TEXT NOT NULL CHECK(
        length(state_sha256) = 64 AND state_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    occurred_at TEXT NOT NULL,
    UNIQUE(task_id, state_version)
);

CREATE TABLE coding_worktrees (
    task_id TEXT PRIMARY KEY REFERENCES coding_tasks(task_id) ON DELETE RESTRICT,
    source_repository TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    worktree_key TEXT NOT NULL,
    branch TEXT,
    branch_key TEXT,
    base_commit TEXT NOT NULL,
    owner_token_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'active','complete','orphaned','cleanup_blocked','removed'
    )),
    owner_pid INTEGER NOT NULL CHECK(owner_pid >= 1),
    record_version INTEGER NOT NULL CHECK(record_version >= 1),
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL CHECK(
        length(record_sha256) = 64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_coding_tasks_status_updated
    ON coding_tasks(status, updated_at);
CREATE INDEX idx_coding_events_task_order
    ON coding_task_events(task_id, event_id);
CREATE INDEX idx_coding_worktrees_status_heartbeat
    ON coding_worktrees(status, heartbeat_at);
CREATE UNIQUE INDEX uq_coding_worktrees_live_path
    ON coding_worktrees(worktree_key)
    WHERE status <> 'removed';
CREATE UNIQUE INDEX uq_coding_worktrees_live_branch
    ON coding_worktrees(source_repository COLLATE NOCASE, branch_key)
    WHERE branch_key IS NOT NULL AND status <> 'removed';

CREATE TRIGGER coding_task_events_append_only_update
BEFORE UPDATE ON coding_task_events
BEGIN
    SELECT RAISE(ABORT, 'coding task events are append-only');
END;

CREATE TRIGGER coding_task_events_append_only_delete
BEFORE DELETE ON coding_task_events
BEGIN
    SELECT RAISE(ABORT, 'coding task events are append-only');
END;
"""


@dataclass(frozen=True, slots=True)
class CodingDatabaseVerification:
    database_path: Path
    application_id: int
    schema_version: int
    integrity_check: str
    foreign_key_violations: int
    journal_mode: str
    secure_delete: bool
    event_chain_consistent: bool


def _checksum() -> str:
    return hashlib.sha256(_SCHEMA.encode("utf-8")).hexdigest()


def open_coding_database(
    path: str | Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    database = Path(path).resolve()
    if read_only:
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    else:
        connection = sqlite3.connect(
            database,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    return connection


def _owned_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view','trigger') AND name NOT LIKE 'sqlite_%'"
        )
    }


def _validate_schema_objects(connection: sqlite3.Connection) -> None:
    required_tables = {
        "coding_migrations",
        "coding_tasks",
        "coding_task_events",
        "coding_worktrees",
    }
    required_triggers = {
        "coding_task_events_append_only_update",
        "coding_task_events_append_only_delete",
    }
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    triggers = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    if not required_tables.issubset(tables) or not required_triggers.issubset(triggers):
        raise CodingMigrationError("coding database schema objects are incomplete")

    required_columns = {
        "coding_tasks": {
            "task_id", "request_id", "status", "state_version", "state_json",
            "state_sha256", "created_at", "updated_at",
        },
        "coding_task_events": {
            "event_id", "task_id", "state_version", "event_type", "from_status",
            "to_status", "reason_code", "state_sha256", "occurred_at",
        },
        "coding_worktrees": {
            "task_id", "source_repository", "worktree_path", "worktree_key", "branch",
            "branch_key", "base_commit", "owner_token_hash", "status", "owner_pid",
            "record_version", "record_json", "record_sha256", "created_at", "heartbeat_at",
            "completed_at", "updated_at",
        },
    }
    for table, expected in required_columns.items():
        observed = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not expected.issubset(observed):
            raise CodingMigrationError(f"coding table {table} has incompatible columns")


def migrate_coding_database(
    path: str | Path,
    *,
    harden_permissions: bool = True,
) -> Path:
    database = Path(path).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_coding_database(database)) as connection:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id not in {0, APPLICATION_ID}:
            raise CodingMigrationError("unexpected coding SQLite application_id")
        if version not in {0, CURRENT_SCHEMA_VERSION}:
            raise CodingMigrationError("unsupported coding database schema version")

        if application_id == 0 and version == 0:
            if _owned_objects(connection):
                raise CodingMigrationError(
                    "refusing to adopt a nonempty unrelated SQLite database"
                )
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            ).casefold()
            if journal_mode != "wal":
                raise CodingMigrationError("coding database could not enable WAL mode")
            try:
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                connection.execute(
                    "INSERT INTO coding_migrations(version,name,checksum,applied_at) "
                    "VALUES(1,'initial coding schema',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (_checksum(),),
                )
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        else:
            ledger = connection.execute(
                "SELECT checksum FROM coding_migrations WHERE version=1"
            ).fetchone()
            if ledger is None or str(ledger["checksum"]) != _checksum():
                raise CodingMigrationError("coding migration checksum mismatch")
            _validate_schema_objects(connection)

    verify_coding_database(database)
    if harden_permissions:
        try:
            restrict_database_storage(database)
        except Exception as exc:
            raise CodingMigrationError("coding database permission hardening failed") from exc
    return database


def verify_coding_database(path: str | Path) -> CodingDatabaseVerification:
    database = Path(path).resolve()
    if not database.is_file():
        raise CodingMigrationError("coding database does not exist")
    try:
        with closing(open_coding_database(database, read_only=True)) as connection:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            secure_delete = bool(connection.execute("PRAGMA secure_delete").fetchone()[0])
            _validate_schema_objects(connection)
            ledger = connection.execute(
                "SELECT checksum FROM coding_migrations WHERE version=1"
            ).fetchone()
            inconsistent_events = int(
                connection.execute(
                    "SELECT count(*) FROM coding_tasks t "
                    "LEFT JOIN coding_task_events e "
                    "ON e.task_id=t.task_id AND e.state_version=t.state_version "
                    "WHERE e.event_id IS NULL OR e.state_sha256<>t.state_sha256"
                ).fetchone()[0]
            )
    except CodingMigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise CodingMigrationError("coding database verification failed") from exc

    if application_id != APPLICATION_ID or version != CURRENT_SCHEMA_VERSION:
        raise CodingMigrationError("coding database identity mismatch")
    if ledger is None or str(ledger["checksum"]) != _checksum():
        raise CodingMigrationError("coding migration ledger mismatch")
    if integrity.casefold() != "ok" or foreign_keys or journal_mode != "wal":
        raise CodingMigrationError("coding database structural verification failed")
    if not secure_delete or inconsistent_events:
        raise CodingMigrationError("coding database safety invariants failed")
    return CodingDatabaseVerification(
        database_path=database,
        application_id=application_id,
        schema_version=version,
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        journal_mode=journal_mode,
        secure_delete=secure_delete,
        event_chain_consistent=True,
    )


__all__ = [
    "APPLICATION_ID",
    "CURRENT_SCHEMA_VERSION",
    "CodingDatabaseVerification",
    "CodingMigrationError",
    "migrate_coding_database",
    "open_coding_database",
    "verify_coding_database",
]
