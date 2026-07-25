from __future__ import annotations

import base64
import hashlib
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final


CURRENT_SCHEMA_VERSION: Final = 3
APPLICATION_ID: Final = 0x4C41494D  # "LAIM" (Local Agent In-Memory metadata)
DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000


class MigrationError(RuntimeError):
    """The database cannot be migrated without violating a storage invariant."""


class BackupVerificationError(MigrationError):
    """A backup or restore candidate failed structural verification."""


_WINDOWS_PRIVATE_DACL: Final = r"""
$ErrorActionPreference = 'Stop'
$stage = 'input'
try {
    $target = $env:LOCAL_AGENT_PRIVATE_PATH
    $kind = $env:LOCAL_AGENT_PRIVATE_KIND
    if ($kind -notin @('directory', 'file')) {
        throw 'invalid private path kind'
    }

    $stage = 'identity'
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentSid) {
        throw 'current Windows identity has no SID'
    }

    $stage = 'read'
    $acl = Get-Acl -LiteralPath $target
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($ownerSid.Value -ne $currentSid.Value) {
        throw 'private path is not owned by the current identity'
    }

    $stage = 'replace'
    $acl.SetAccessRuleProtection($true, $false)
    $existingRules = @(
        $acl.GetAccessRules(
            $true,
            $false,
            [Security.Principal.SecurityIdentifier]
        )
    )
    foreach ($existingRule in $existingRules) {
        [void]$acl.RemoveAccessRuleSpecific($existingRule)
    }
    if ($kind -eq 'directory') {
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $currentSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    } else {
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $currentSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
    }
    [void]$acl.AddAccessRule($rule)

    $stage = 'write'
    if ($kind -eq 'directory') {
        [IO.Directory]::SetAccessControl($target, $acl)
    } else {
        [IO.File]::SetAccessControl($target, $acl)
    }

    $stage = 'verify'
    $observed = Get-Acl -LiteralPath $target
    $observedOwner = $observed.GetOwner(
        [Security.Principal.SecurityIdentifier]
    )
    $rules = @(
        $observed.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )
    )
    if (
        $observedOwner.Value -ne $currentSid.Value -or
        -not $observed.AreAccessRulesProtected -or
        $rules.Count -ne 1
    ) {
        throw 'private DACL verification failed'
    }
    $observedRule = $rules[0]
    if (
        $observedRule.IsInherited -or
        $observedRule.AccessControlType -ne
            [Security.AccessControl.AccessControlType]::Allow -or
        $observedRule.IdentityReference.Value -ne $currentSid.Value -or
        $observedRule.InheritanceFlags -ne $inheritance -or
        $observedRule.PropagationFlags -ne
            [Security.AccessControl.PropagationFlags]::None -or
        (($observedRule.FileSystemRights -band
            [Security.AccessControl.FileSystemRights]::FullControl) -ne
            [Security.AccessControl.FileSystemRights]::FullControl)
    ) {
        throw 'private DACL rule verification failed'
    }
} catch {
    $exceptionType = $_.Exception.GetType().FullName
    [Console]::Error.WriteLine(
        "LOCESTRA_ACL_ERROR stage=$stage type=$exceptionType"
    )
    $exitCode = switch ($stage) {
        'input' { 41 }
        'identity' { 42 }
        'read' { 43 }
        'replace' { 44 }
        'write' { 45 }
        'verify' { 46 }
        default { 49 }
    }
    exit $exitCode
}
"""
_WINDOWS_PRIVATE_DACL_ENCODED: Final = base64.b64encode(
    _WINDOWS_PRIVATE_DACL.encode("utf-16-le")
).decode("ascii")


def _windows_powershell_executable() -> str:
    for name in ("pwsh.exe", "powershell.exe"):
        candidate = shutil.which(name)
        if candidate:
            resolved = Path(candidate).resolve(strict=True)
            if resolved.is_file():
                return str(resolved)
    raise MigrationError("Windows PowerShell executable is unavailable")


def _restrict_private_path(path: str | Path, *, directory: bool) -> Path:
    target = Path(path).resolve()
    if directory and not target.is_dir():
        raise MigrationError("private permission hardening requires a directory")
    if not directory and not target.is_file():
        raise MigrationError("private permission hardening requires a file")
    try:
        os.chmod(target, 0o700 if directory else 0o600)
        if os.name == "nt":
            environment = os.environ.copy()
            environment["LOCAL_AGENT_PRIVATE_PATH"] = str(target)
            environment["LOCAL_AGENT_PRIVATE_KIND"] = (
                "directory" if directory else "file"
            )
            hardened = subprocess.run(
                [
                    _windows_powershell_executable(),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    _WINDOWS_PRIVATE_DACL_ENCODED,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
            )
            if hardened.returncode != 0:
                stage = {
                    41: "input",
                    42: "identity",
                    43: "read",
                    44: "replace",
                    45: "write",
                    46: "verify",
                    49: "unknown",
                }.get(hardened.returncode, "payload")
                reported = re.search(
                    r"LOCESTRA_ACL_ERROR "
                    r"stage=(input|identity|read|replace|write|verify|unknown) "
                    r"type=([A-Za-z0-9_.+]+)",
                    hardened.stderr,
                )
                exception_type = (
                    reported.group(2)[:120] if reported is not None else "unreported"
                )
                detail = (
                    f"LOCESTRA_ACL_ERROR stage={stage} type={exception_type}"
                )
                raise OSError(f"Windows ACL update failed ({detail})")
    except Exception as exc:
        raise MigrationError("private permission hardening failed") from exc
    return target


def restrict_private_file(path: str | Path) -> Path:
    """Replace inherited and explicit grants with one owner-only file DACL."""

    return _restrict_private_path(path, directory=False)


def restrict_private_directory(path: str | Path) -> Path:
    """Protect a storage directory and the inheritance policy for new files."""

    return _restrict_private_path(path, directory=True)


def restrict_database_storage(path: str | Path) -> Path:
    """Protect the database directory plus every currently materialized sidecar."""

    database_path = Path(path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_directory(database_path.parent)
    for suffix in ("", "-wal", "-shm"):
        candidate = database_path.with_name(database_path.name + suffix)
        if candidate.is_file():
            restrict_private_file(candidate)
    backup_directory = database_path.parent / "backups"
    if backup_directory.is_dir():
        restrict_private_directory(backup_directory)
        for artifact in backup_directory.iterdir():
            if artifact.is_symlink():
                raise MigrationError("backup directory cannot contain symbolic links")
            if artifact.is_file():
                restrict_private_file(artifact)
    return database_path


@dataclass(frozen=True)
class MigrationResult:
    database_path: Path
    from_version: int
    to_version: int
    applied_versions: tuple[int, ...]
    backup_path: Path | None


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    user_version: int
    application_id: int
    size_bytes: int


@dataclass(frozen=True)
class VerificationResult:
    database_path: Path
    user_version: int
    application_id: int
    integrity_check: str
    foreign_key_violations: int


@dataclass(frozen=True)
class RestoreResult:
    database_path: Path
    restored_from: Path
    schema_version: int
    safety_backup_path: Path | None


@dataclass(frozen=True)
class _Migration:
    version: int
    name: str
    fingerprint: str
    apply: Callable[[sqlite3.Connection], None]

    @property
    def checksum(self) -> str:
        material = (
            f"{self.version}\n{self.name}\n{self.fingerprint}\n"
            f"{inspect.getsource(self.apply)}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


_LEGACY_TASK_COLUMNS: Final = {
    "id": "TEXT",
    "created_at": "REAL",
    "route": "TEXT",
    "status": "TEXT",
    "project_path": "TEXT",
    "prompt": "TEXT",
    "result": "TEXT",
    "metadata": "TEXT",
}

_TASK_STATE_COLUMNS: Final = {
    "updated_at": "REAL",
    "schema_version": "TEXT",
    "state_json": "TEXT",
}

_TASK_PRIVACY_COLUMNS: Final = {
    "privacy_version": "TEXT",
    "legacy_payload": "INTEGER",
}

_MEMORY_RECORD_COLUMNS: Final = {
    "record_id": "TEXT", "record_schema_version": "TEXT", "record_type": "TEXT",
    "owner_id": "TEXT", "scope_type": "TEXT", "scope_key": "TEXT",
    "project_realpath": "TEXT", "task_id": "TEXT", "memory_key": "TEXT",
    "value_json": "TEXT", "value_hash": "TEXT", "dedupe_key": "TEXT",
    "status": "TEXT", "confidence": "REAL", "sensitivity": "TEXT",
    "retention_class": "TEXT", "expires_at": "TEXT", "valid_from": "TEXT",
    "valid_to": "TEXT", "source_commit_sha": "TEXT", "author": "TEXT",
    "producer": "TEXT", "supersedes_record_id": "TEXT", "created_at": "TEXT",
    "observed_at": "TEXT", "updated_at": "TEXT", "deleted_at": "TEXT",
    "revision": "INTEGER",
}

_MEMORY_SOURCE_COLUMNS: Final = {
    "source_id": "TEXT", "record_id": "TEXT", "source_type": "TEXT",
    "source_uri": "TEXT", "source_fragment": "TEXT", "source_hash": "TEXT",
    "source_commit_sha": "TEXT", "source_mtime_ns": "INTEGER",
    "observed_at": "TEXT", "producer": "TEXT", "author": "TEXT",
    "source_dedupe_key": "TEXT",
}

_MEMORY_AUDIT_COLUMNS: Final = {
    "event_id": "INTEGER", "operation_id": "TEXT", "record_id": "TEXT",
    "record_type": "TEXT", "scope_hash": "TEXT", "actor": "TEXT",
    "action": "TEXT", "outcome": "TEXT", "reason_code": "TEXT",
    "before_status": "TEXT", "after_status": "TEXT", "occurred_at": "TEXT",
    "affected_count": "INTEGER", "policy_version": "TEXT",
}

_MEMORY_CONFLICT_COLUMNS: Final = {
    "conflict_id": "TEXT", "owner_id": "TEXT", "scope_type": "TEXT",
    "scope_key": "TEXT", "record_type": "TEXT", "memory_key": "TEXT",
    "status": "TEXT", "resolution_record_id": "TEXT", "reason_code": "TEXT",
    "created_at": "TEXT", "resolved_at": "TEXT",
}

_MEMORY_CONFLICT_MEMBER_COLUMNS: Final = {
    "conflict_id": "TEXT", "record_id": "TEXT", "added_at": "TEXT",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1_000,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1_000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def open_database(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a configured connection without implicitly running migrations."""

    return _connect(Path(path), readonly=readonly)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row["name"]): str(row["type"]).upper()
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _require_columns(
    connection: sqlite3.Connection, table: str, required: dict[str, str]
) -> None:
    actual = _columns(connection, table)
    missing = sorted(set(required) - set(actual))
    if missing:
        raise MigrationError(f"{table} is missing required columns: {', '.join(missing)}")
    incompatible = sorted(
        name for name, expected in required.items() if actual[name] != expected
    )
    if incompatible:
        raise MigrationError(
            f"{table} has incompatible column types: {', '.join(incompatible)}"
        )


def _apply_legacy_tasks(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "tasks"):
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                route TEXT NOT NULL,
                status TEXT NOT NULL,
                project_path TEXT,
                prompt TEXT NOT NULL,
                result TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
    _require_columns(connection, "tasks", _LEGACY_TASK_COLUMNS)


def _apply_task_state_v1(connection: sqlite3.Connection) -> None:
    _require_columns(connection, "tasks", _LEGACY_TASK_COLUMNS)
    existing = _columns(connection, "tasks")
    additions = {
        "updated_at": "ALTER TABLE tasks ADD COLUMN updated_at REAL",
        "schema_version": "ALTER TABLE tasks ADD COLUMN schema_version TEXT",
        "state_json": "ALTER TABLE tasks ADD COLUMN state_json TEXT",
    }
    for name, statement in additions.items():
        if name not in existing:
            connection.execute(statement)
    connection.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL")
    _require_columns(connection, "tasks", {**_LEGACY_TASK_COLUMNS, **_TASK_STATE_COLUMNS})


def _execute_script_transactionally(connection: sqlite3.Connection, script: str) -> None:
    """Execute a DDL script without sqlite3.executescript's implicit COMMIT."""

    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
            pending = ""
    if pending.strip():
        raise MigrationError("incomplete SQL statement in controlled memory migration")


def _apply_controlled_memory_v1(connection: sqlite3.Connection) -> None:
    task_columns = _columns(connection, "tasks")
    if "privacy_version" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN privacy_version TEXT")
    if "legacy_payload" not in task_columns:
        # Existing rows are explicitly quarantined metadata.  New writes set
        # this flag to zero only after passing the Stage 003 persistence filter.
        connection.execute(
            "ALTER TABLE tasks ADD COLUMN legacy_payload INTEGER NOT NULL DEFAULT 1"
        )
    _execute_script_transactionally(
        connection,
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            record_id TEXT PRIMARY KEY NOT NULL,
            record_schema_version TEXT NOT NULL,
            record_type TEXT NOT NULL CHECK (
                record_type IN (
                    'user_profile', 'project_knowledge', 'task_history',
                    'operational_state', 'archive_reference'
                )
            ),
            owner_id TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK (scope_type IN ('user', 'project', 'task')),
            scope_key TEXT NOT NULL,
            project_realpath TEXT,
            task_id TEXT,
            memory_key TEXT NOT NULL,
            value_json TEXT NOT NULL CHECK (json_valid(value_json) AND length(value_json) <= 262144),
            value_hash TEXT NOT NULL CHECK (length(value_hash) = 64),
            dedupe_key TEXT NOT NULL UNIQUE CHECK (length(dedupe_key) = 64),
            status TEXT NOT NULL CHECK (
                status IN (
                    'candidate', 'confirmed', 'conflicted', 'stale',
                    'rejected', 'superseded', 'deleted'
                )
            ),
            confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
            sensitivity TEXT NOT NULL CHECK (sensitivity IN ('public', 'internal', 'sensitive')),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN ('session', 'task', 'ttl', 'manual', 'permanent')
            ),
            expires_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            source_commit_sha TEXT,
            author TEXT NOT NULL,
            producer TEXT NOT NULL,
            supersedes_record_id TEXT REFERENCES memory_records(record_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            CHECK ((retention_class = 'ttl') = (expires_at IS NOT NULL)),
            CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
            CHECK ((status = 'deleted') = (deleted_at IS NOT NULL)),
            CHECK (scope_type != 'project' OR project_realpath IS NOT NULL),
            CHECK (scope_type != 'task' OR task_id IS NOT NULL)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS memory_sources (
            source_id TEXT PRIMARY KEY NOT NULL,
            record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE CASCADE,
            source_type TEXT NOT NULL,
            source_uri TEXT,
            source_fragment TEXT,
            source_hash TEXT CHECK (source_hash IS NULL OR length(source_hash) = 64),
            source_commit_sha TEXT,
            source_mtime_ns INTEGER CHECK (source_mtime_ns IS NULL OR source_mtime_ns >= 0),
            observed_at TEXT NOT NULL,
            producer TEXT NOT NULL,
            author TEXT NOT NULL,
            source_dedupe_key TEXT NOT NULL UNIQUE CHECK (length(source_dedupe_key) = 64)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS memory_conflicts (
            conflict_id TEXT PRIMARY KEY NOT NULL,
            owner_id TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK (scope_type IN ('user', 'project', 'task')),
            scope_key TEXT NOT NULL,
            record_type TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
            resolution_record_id TEXT REFERENCES memory_records(record_id) ON DELETE SET NULL,
            reason_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            CHECK ((status = 'resolved') = (resolved_at IS NOT NULL))
        ) STRICT;

        CREATE TABLE IF NOT EXISTS memory_conflict_members (
            conflict_id TEXT NOT NULL REFERENCES memory_conflicts(conflict_id) ON DELETE CASCADE,
            record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY (conflict_id, record_id)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS memory_audit_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            record_id TEXT,
            record_type TEXT,
            scope_hash TEXT NOT NULL CHECK (length(scope_hash) = 64),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            before_status TEXT,
            after_status TEXT,
            occurred_at TEXT NOT NULL,
            affected_count INTEGER NOT NULL CHECK (affected_count >= 0),
            policy_version TEXT NOT NULL
        ) STRICT;

        CREATE INDEX IF NOT EXISTS idx_memory_records_scope_retrieval
            ON memory_records(owner_id, scope_type, scope_key, status, record_type, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_records_project
            ON memory_records(project_realpath, status, memory_key)
            WHERE project_realpath IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_records_task
            ON memory_records(task_id, status)
            WHERE task_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_records_expiry
            ON memory_records(expires_at)
            WHERE expires_at IS NOT NULL AND status NOT IN ('deleted', 'rejected', 'superseded');
        CREATE INDEX IF NOT EXISTS idx_memory_sources_record
            ON memory_sources(record_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_sources_invalidation
            ON memory_sources(source_hash, source_commit_sha, source_mtime_ns);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_open_conflict
            ON memory_conflicts(owner_id, scope_type, scope_key, record_type, memory_key)
            WHERE status = 'open';
        CREATE INDEX IF NOT EXISTS idx_memory_conflict_members_record
            ON memory_conflict_members(record_id);
        CREATE INDEX IF NOT EXISTS idx_memory_audit_record_time
            ON memory_audit_log(record_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_audit_time
            ON memory_audit_log(occurred_at DESC);
        """
    )


_MIGRATIONS: Final = (
    _Migration(
        1,
        "legacy_tasks",
        "create-or-validate legacy tasks columns: id created_at route status project_path prompt result metadata",
        _apply_legacy_tasks,
    ),
    _Migration(
        2,
        "task_state_v1",
        "add-or-validate updated_at schema_version state_json; backfill updated_at from created_at",
        _apply_task_state_v1,
    ),
    _Migration(
        3,
        "controlled_memory_v1",
        "records sources conflicts conflict-members payload-free-audit scoped indexes and task privacy markers; schema 2026-07-14",
        _apply_controlled_memory_v1,
    ),
)


def _create_migration_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL CHECK (length(checksum) = 64),
            applied_at TEXT NOT NULL,
            application_version TEXT NOT NULL
        ) STRICT
        """
    )


def _validate_migration_ledger(
    connection: sqlite3.Connection, *, require_complete: bool = False
) -> None:
    known = {migration.version: migration for migration in _MIGRATIONS}
    observed: set[int] = set()
    for row in connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ):
        version = int(row["version"])
        observed.add(version)
        migration = known.get(version)
        if migration is None:
            raise MigrationError(f"database contains unknown migration {row['version']}")
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationError(f"migration checksum mismatch at version {row['version']}")
    if observed and observed != set(range(1, max(observed) + 1)):
        raise MigrationError("schema_migrations must be a contiguous version prefix")
    if require_complete and observed != set(known):
        raise MigrationError("schema_migrations does not contain every current migration")


def _validate_final_schema(connection: sqlite3.Connection) -> None:
    _require_columns(
        connection,
        "tasks",
        {**_LEGACY_TASK_COLUMNS, **_TASK_STATE_COLUMNS, **_TASK_PRIVACY_COLUMNS},
    )
    required_tables = {
        "schema_migrations",
        "memory_records",
        "memory_sources",
        "memory_conflicts",
        "memory_conflict_members",
        "memory_audit_log",
    }
    missing = sorted(table for table in required_tables if not _table_exists(connection, table))
    if missing:
        raise MigrationError(f"database is missing required tables: {', '.join(missing)}")
    _require_columns(connection, "memory_records", _MEMORY_RECORD_COLUMNS)
    _require_columns(connection, "memory_sources", _MEMORY_SOURCE_COLUMNS)
    _require_columns(connection, "memory_conflicts", _MEMORY_CONFLICT_COLUMNS)
    _require_columns(
        connection, "memory_conflict_members", _MEMORY_CONFLICT_MEMBER_COLUMNS
    )
    _require_columns(connection, "memory_audit_log", _MEMORY_AUDIT_COLUMNS)
    required_indexes = {
        "idx_memory_records_scope_retrieval",
        "idx_memory_records_project",
        "idx_memory_records_task",
        "idx_memory_records_expiry",
        "idx_memory_sources_record",
        "idx_memory_sources_invalidation",
        "uq_memory_open_conflict",
        "idx_memory_conflict_members_record",
        "idx_memory_audit_record_time",
        "idx_memory_audit_time",
    }
    observed_indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_indexes = sorted(required_indexes - observed_indexes)
    if missing_indexes:
        raise MigrationError(
            f"database is missing required indexes: {', '.join(missing_indexes)}"
        )


def _connection_needs_migration(connection: sqlite3.Connection) -> bool:
    """Inspect schema state on a caller-owned snapshot or write reservation."""

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
        )
    if version != CURRENT_SCHEMA_VERSION or not _table_exists(connection, "schema_migrations"):
        return True
    _validate_migration_ledger(connection)
    versions = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations")
    }
    return versions != {migration.version for migration in _MIGRATIONS}


def _needs_migration(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    with closing(_connect(path, readonly=True)) as connection:
        return _connection_needs_migration(connection)


def migrate_database(
    path: str | Path,
    *,
    backup_directory: str | Path | None = None,
    create_backup: bool = True,
) -> MigrationResult:
    """Migrate an empty, legacy, or current database transactionally to schema v3."""

    database_path = Path(path).resolve()
    restrict_database_storage(database_path)
    had_existing_database = database_path.exists() and database_path.stat().st_size > 0
    backup_path: Path | None = None

    with closing(_connect(database_path)) as connection:
        observed_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if observed_version > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                f"database schema {observed_version} is newer than supported {CURRENT_SCHEMA_VERSION}"
            )
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise MigrationError(f"cannot enable WAL journal mode; SQLite returned {journal_mode!r}")
        applied: list[int] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            # BEGIN IMMEDIATE is deliberately acquired before the backup.  It
            # allows readers (including SQLite's online backup connection) but
            # prevents another writer from committing after the backup snapshot
            # and before this transaction applies the DDL.
            from_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            needs_migration = _connection_needs_migration(connection)
            has_existing_schema = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone() is not None
            if (
                needs_migration
                and create_backup
                and (had_existing_database or has_existing_schema)
            ):
                directory = (
                    Path(backup_directory).resolve()
                    if backup_directory is not None
                    else database_path.parent / "backups"
                )
                backup_path = backup_database(
                    database_path, destination_directory=directory
                ).backup_path
            _create_migration_ledger(connection)
            _validate_migration_ledger(connection)
            existing = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in _MIGRATIONS:
                if migration.version in existing:
                    continue
                migration.apply(connection)
                connection.execute(
                    """
                    INSERT INTO schema_migrations
                        (version, name, checksum, applied_at, application_version)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        _utc_now(),
                        "local-agent-stage-003",
                    ),
                )
                applied.append(migration.version)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            _validate_migration_ledger(connection, require_complete=True)
            _validate_final_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    verification = verify_database(database_path, require_current=True)
    if verification.foreign_key_violations:
        raise MigrationError("migrated database contains foreign-key violations")
    restrict_database_storage(database_path)
    return MigrationResult(
        database_path=database_path,
        from_version=from_version,
        to_version=CURRENT_SCHEMA_VERSION,
        applied_versions=tuple(applied),
        backup_path=backup_path,
    )


def verify_database(
    path: str | Path,
    *,
    require_current: bool = False,
    allow_unidentified_legacy: bool = True,
) -> VerificationResult:
    """Verify integrity without reading any prompt, result, or memory payload."""

    database_path = Path(path).resolve()
    if not database_path.is_file():
        raise BackupVerificationError(f"database does not exist: {database_path}")
    try:
        with closing(_connect(database_path, readonly=True)) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise BackupVerificationError(f"integrity_check failed: {integrity}")
            foreign_key_violations = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_violations:
                raise BackupVerificationError(
                    "database contains foreign-key violations"
                )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if version > CURRENT_SCHEMA_VERSION:
                raise BackupVerificationError(
                    f"database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            if require_current and version != CURRENT_SCHEMA_VERSION:
                raise BackupVerificationError(
                    f"database schema {version} is not current schema {CURRENT_SCHEMA_VERSION}"
                )
            if application_id == 0:
                if not allow_unidentified_legacy or version >= CURRENT_SCHEMA_VERSION:
                    raise BackupVerificationError("unidentified database is not a compatible legacy backup")
                if not _table_exists(connection, "tasks"):
                    raise BackupVerificationError("legacy backup is missing the tasks table")
                try:
                    _require_columns(connection, "tasks", _LEGACY_TASK_COLUMNS)
                except MigrationError as exc:
                    raise BackupVerificationError("legacy backup has an incompatible tasks schema") from exc
            elif application_id != APPLICATION_ID:
                raise BackupVerificationError(
                    f"unexpected SQLite application_id {application_id}"
                )
            if application_id == APPLICATION_ID and version == CURRENT_SCHEMA_VERSION:
                _validate_migration_ledger(connection, require_complete=True)
                _validate_final_schema(connection)
            elif application_id == APPLICATION_ID:
                _validate_migration_ledger(connection)
                _require_columns(connection, "tasks", _LEGACY_TASK_COLUMNS)
                if version >= 2:
                    _require_columns(connection, "tasks", _TASK_STATE_COLUMNS)
            if require_current and application_id != APPLICATION_ID:
                raise BackupVerificationError("current database has no Local Agent application_id")
    except BackupVerificationError:
        raise
    except (sqlite3.DatabaseError, MigrationError) as exc:
        raise BackupVerificationError(f"SQLite verification failed: {type(exc).__name__}") from exc
    return VerificationResult(
        database_path=database_path,
        user_version=version,
        application_id=application_id,
        integrity_check=integrity,
        foreign_key_violations=foreign_key_violations,
    )


def backup_database(
    source: str | Path,
    destination: str | Path | None = None,
    *,
    destination_directory: str | Path | None = None,
) -> BackupResult:
    """Create and verify a consistent SQLite online backup, including WAL contents."""

    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if destination is not None and destination_directory is not None:
        raise ValueError("specify destination or destination_directory, not both")
    if destination is None:
        directory = (
            Path(destination_directory).resolve()
            if destination_directory is not None
            else source_path.parent / "backups"
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination_path = directory / (
            f"{source_path.stem}.v{_read_user_version(source_path)}.{timestamp}."
            f"{uuid.uuid4().hex[:8]}.bak.sqlite3"
        )
    else:
        destination_path = Path(destination).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_directory(destination_path.parent)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    partial = destination_path.with_name(destination_path.name + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    published = False
    try:
        with closing(_connect(source_path, readonly=True)) as source_connection:
            with closing(sqlite3.connect(partial)) as destination_connection:
                restrict_private_file(partial)
                source_connection.backup(destination_connection)
                destination_connection.commit()
        verification = verify_database(partial)
        os.replace(partial, destination_path)
        published = True
    except Exception:
        partial.unlink(missing_ok=True)
        if published:
            destination_path.unlink(missing_ok=True)
        raise
    return BackupResult(
        backup_path=destination_path,
        user_version=verification.user_version,
        application_id=verification.application_id,
        size_bytes=destination_path.stat().st_size,
    )


def _read_user_version(path: Path) -> int:
    with closing(_connect(path, readonly=True)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def verify_backup(path: str | Path) -> VerificationResult:
    """Public explicit name for verification used by backup/restore callers."""

    return verify_database(path)


def _raw_safety_copy(
    target_path: Path, destination_directory: str | Path | None
) -> Path:
    """Preserve an offline damaged target and its sidecars without parsing it."""

    directory = (
        Path(destination_directory).resolve()
        if destination_directory is not None
        else target_path.parent / "backups"
    )
    directory.mkdir(parents=True, exist_ok=True)
    restrict_private_directory(directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / (
        f"{target_path.stem}.raw.{timestamp}.{uuid.uuid4().hex[:8]}.safety.sqlite3"
    )
    copied: list[Path] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            source = target_path.with_name(target_path.name + suffix)
            if not source.exists():
                continue
            output = destination.with_name(destination.name + suffix)
            partial = output.with_name(output.name + ".partial")
            if output.exists() or partial.exists():
                raise FileExistsError(output)
            with source.open("rb") as input_stream, partial.open("xb") as output_stream:
                restrict_private_file(partial)
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            os.replace(partial, output)
            copied.append(output)
        if destination not in copied:
            raise FileNotFoundError(target_path)
        return destination
    except Exception:
        for path in copied:
            path.unlink(missing_ok=True)
        for partial in directory.glob(destination.name + "*.partial"):
            partial.unlink(missing_ok=True)
        raise


def restore_database(
    backup: str | Path,
    target: str | Path,
    *,
    confirm: bool = False,
    safety_backup_directory: str | Path | None = None,
) -> RestoreResult:
    """Explicitly restore a verified backup and migrate it to the current schema.

    Callers must stop all processes holding the target database before invoking
    this helper.  ``confirm=True`` is deliberately required even for a new path.
    """

    if not confirm:
        raise PermissionError("restore requires confirm=True")
    backup_path = Path(backup).resolve()
    target_path = Path(target).resolve()
    if backup_path == target_path:
        raise ValueError("backup and restore target must be different paths")
    verify_database(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    restrict_database_storage(target_path)

    safety_backup: Path | None = None
    has_target = target_path.exists() and target_path.stat().st_size > 0
    if has_target:
        try:
            safety_backup = backup_database(
                target_path,
                destination_directory=(
                    Path(safety_backup_directory).resolve()
                    if safety_backup_directory is not None
                    else target_path.parent / "backups"
                ),
            ).backup_path
        except (BackupVerificationError, sqlite3.DatabaseError, MigrationError):
            safety_backup = _raw_safety_copy(
                target_path, safety_backup_directory
            )

    temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.restore")
    try:
        with closing(_connect(backup_path, readonly=True)) as source_connection:
            with closing(sqlite3.connect(temporary)) as destination_connection:
                restrict_private_file(temporary)
                source_connection.backup(destination_connection)
                destination_connection.commit()
        verify_database(temporary)
        prepared = migrate_database(temporary, create_backup=False)
        verify_database(temporary, require_current=True)
        # Consolidate the prepared WAL before the single atomic replacement.
        with closing(_connect(temporary)) as prepared_connection:
            prepared_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            journal_mode = str(
                prepared_connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            ).lower()
            if journal_mode != "delete":
                raise BackupVerificationError(
                    f"cannot consolidate restore candidate; SQLite returned {journal_mode!r}"
                )
        # The caller has stopped writers.  Consolidating a valid target before
        # parking sidecars preserves committed WAL rows even if replace fails.
        if has_target:
            try:
                with closing(_connect(target_path)) as target_connection:
                    checkpoint = target_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise BackupVerificationError(
                            "target WAL could not be consolidated for restore"
                        )
            except sqlite3.DatabaseError:
                # A raw safety copy already preserves an unparsable target.
                pass

        parked_sidecars: list[tuple[Path, Path]] = []
        for suffix in ("-wal", "-shm"):
            sidecar = target_path.with_name(target_path.name + suffix)
            if sidecar.exists():
                parked = sidecar.with_name(
                    sidecar.name + f".{uuid.uuid4().hex}.restore-parked"
                )
                os.replace(sidecar, parked)
                parked_sidecars.append((sidecar, parked))
        try:
            os.replace(temporary, target_path)
        except Exception:
            for sidecar, parked in reversed(parked_sidecars):
                if parked.exists():
                    os.replace(parked, sidecar)
            raise
        for _, parked in parked_sidecars:
            parked.unlink(missing_ok=True)
        migration = migrate_database(target_path, create_backup=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return RestoreResult(
        database_path=target_path,
        restored_from=backup_path,
        schema_version=max(prepared.to_version, migration.to_version),
        safety_backup_path=safety_backup,
    )


__all__ = [
    "APPLICATION_ID",
    "BackupResult",
    "BackupVerificationError",
    "CURRENT_SCHEMA_VERSION",
    "MigrationError",
    "MigrationResult",
    "RestoreResult",
    "VerificationResult",
    "backup_database",
    "migrate_database",
    "open_database",
    "restrict_database_storage",
    "restrict_private_directory",
    "restrict_private_file",
    "restore_database",
    "verify_backup",
    "verify_database",
]
