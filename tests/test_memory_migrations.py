from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path

import pytest
import services.memory.migrations as memory_migrations

from services.memory.migrations import (
    APPLICATION_ID,
    BackupVerificationError,
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    backup_database,
    migrate_database,
    open_database,
    restrict_private_directory,
    restrict_private_file,
    restore_database,
    verify_backup,
    verify_database,
)


LEGACY_TASK_DDL = """
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


def create_legacy_database(path: Path, *, with_task_state: bool = False) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(LEGACY_TASK_DDL)
        if with_task_state:
            connection.execute("ALTER TABLE tasks ADD COLUMN updated_at REAL")
            connection.execute("ALTER TABLE tasks ADD COLUMN schema_version TEXT")
            connection.execute("ALTER TABLE tasks ADD COLUMN state_json TEXT")
            connection.execute(
                """
                INSERT INTO tasks
                    (id, created_at, updated_at, route, status, project_path,
                     prompt, result, metadata, schema_version, state_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "current-1",
                    200.0,
                    205.0,
                    "local_code",
                    "complete",
                    r"C:\fixture\project",
                    "fixture current prompt",
                    "fixture current result",
                    '{"fixture":true}',
                    "1.0",
                    '{"schema_version":"1.0","fixture":true}',
                ),
            )
        else:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-1",
                    100.0,
                    "fast_chat",
                    "complete",
                    None,
                    "fixture legacy prompt",
                    "fixture legacy result",
                    '{"fixture":true}',
                ),
            )
        connection.commit()


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_empty_database_migrates_to_v3_and_repeat_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"

    first = migrate_database(database)

    assert first.from_version == 0
    assert first.to_version == CURRENT_SCHEMA_VERSION == 3
    assert first.applied_versions == (1, 2, 3)
    assert first.backup_path is None
    verification = verify_database(database, require_current=True)
    assert verification.integrity_check == "ok"
    assert verification.foreign_key_violations == 0
    assert verification.application_id == APPLICATION_ID

    with closing(open_database(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1, 2, 3]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "tasks",
            "schema_migrations",
            "memory_records",
            "memory_sources",
            "memory_conflicts",
            "memory_conflict_members",
            "memory_audit_log",
        }.issubset(tables)
        assert table_columns(connection, "memory_records") == {
            "record_id",
            "record_schema_version",
            "record_type",
            "owner_id",
            "scope_type",
            "scope_key",
            "project_realpath",
            "task_id",
            "memory_key",
            "value_json",
            "value_hash",
            "dedupe_key",
            "status",
            "confidence",
            "sensitivity",
            "retention_class",
            "expires_at",
            "valid_from",
            "valid_to",
            "source_commit_sha",
            "author",
            "producer",
            "supersedes_record_id",
            "created_at",
            "observed_at",
            "updated_at",
            "deleted_at",
            "revision",
        }

    second = migrate_database(database)
    assert second.from_version == 3
    assert second.applied_versions == ()
    assert second.backup_path is None


def test_legacy_database_is_backed_up_and_preserved_without_fabricated_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    create_legacy_database(database)

    result = migrate_database(database, backup_directory=tmp_path / "backups")

    assert result.backup_path is not None and result.backup_path.is_file()
    backup_verification = verify_backup(result.backup_path)
    assert backup_verification.user_version == 0
    assert backup_verification.application_id == 0
    with closing(open_database(database, readonly=True)) as connection:
        row = connection.execute(
            """
            SELECT id, created_at, updated_at, route, status, project_path,
                   prompt, result, metadata, schema_version, state_json
            FROM tasks WHERE id = ?
            """,
            ("legacy-1",),
        ).fetchone()
        privacy_marker = connection.execute(
            "SELECT privacy_version, legacy_payload FROM tasks WHERE id = ?",
            ("legacy-1",),
        ).fetchone()
    assert tuple(row) == (
        "legacy-1",
        100.0,
        100.0,
        "fast_chat",
        "complete",
        None,
        "fixture legacy prompt",
        "fixture legacy result",
        '{"fixture":true}',
        None,
        None,
    )
    assert tuple(privacy_marker) == (None, 1)


def test_current_task_state_database_is_adopted_without_payload_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    create_legacy_database(database, with_task_state=True)

    result = migrate_database(database, backup_directory=tmp_path / "backups")

    assert result.applied_versions == (1, 2, 3)
    with closing(open_database(database, readonly=True)) as connection:
        row = connection.execute(
            """
            SELECT created_at, updated_at, prompt, result, metadata,
                   schema_version, state_json
            FROM tasks WHERE id = 'current-1'
            """
        ).fetchone()
    assert tuple(row) == (
        200.0,
        205.0,
        "fixture current prompt",
        "fixture current result",
        '{"fixture":true}',
        "1.0",
        '{"schema_version":"1.0","fixture":true}',
    )


def test_migration_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    migrate_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 2",
            ("0" * 64,),
        )
        connection.commit()

    with pytest.raises(MigrationError, match="checksum mismatch"):
        migrate_database(database)


def test_online_backup_includes_committed_wal_and_explicit_restore_roundtrips(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    migrate_database(database)
    with closing(open_database(database)) as connection:
        connection.execute(
            """
            INSERT INTO tasks
                (id, created_at, updated_at, route, status, project_path,
                 prompt, result, metadata, schema_version, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wal-1",
                300.0,
                300.0,
                "fast_chat",
                "complete",
                None,
                "fixture backup prompt",
                "fixture backup result",
                "{}",
                "1.0",
                None,
            ),
        )
        connection.commit()

    backup_path = tmp_path / "memory.snapshot.sqlite3"
    backup = backup_database(database, backup_path)
    assert backup.backup_path == backup_path.resolve()
    assert verify_backup(backup_path).user_version == 3

    with closing(open_database(database)) as connection:
        connection.execute("UPDATE tasks SET status = 'failed' WHERE id = 'wal-1'")
        connection.commit()

    with pytest.raises(PermissionError, match="confirm=True"):
        restore_database(backup_path, database)

    restored = restore_database(
        backup_path,
        database,
        confirm=True,
        safety_backup_directory=tmp_path / "restore-safety",
    )
    assert restored.schema_version == 3
    assert restored.safety_backup_path is not None
    assert restored.safety_backup_path.is_file()
    with closing(open_database(database, readonly=True)) as connection:
        assert connection.execute(
            "SELECT status FROM tasks WHERE id = 'wal-1'"
        ).fetchone()[0] == "complete"
    assert verify_database(database, require_current=True).foreign_key_violations == 0


def test_pre_migration_backup_and_ddl_hold_one_writer_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "memory.sqlite3"
    create_legacy_database(database)
    backup_finished = threading.Event()
    allow_migration_to_continue = threading.Event()
    writer_attempted = threading.Event()
    writer_committed = threading.Event()
    migration_results = []
    failures: list[BaseException] = []
    real_backup = memory_migrations.backup_database

    def paused_backup(*args, **kwargs):
        result = real_backup(*args, **kwargs)
        backup_finished.set()
        if not allow_migration_to_continue.wait(5):
            raise AssertionError("test did not release the protected migration sequence")
        return result

    monkeypatch.setattr(memory_migrations, "backup_database", paused_backup)

    def run_migration() -> None:
        try:
            migration_results.append(
                memory_migrations.migrate_database(
                    database, backup_directory=tmp_path / "backups"
                )
            )
        except BaseException as exc:  # pragma: no cover - reported by assertions below
            failures.append(exc)

    def concurrent_writer() -> None:
        try:
            with closing(sqlite3.connect(database, timeout=5.0)) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                writer_attempted.set()
                connection.execute(
                    """
                    INSERT INTO tasks
                        (id, created_at, route, status, project_path, prompt, result, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "writer-after-backup",
                        400.0,
                        "fast_chat",
                        "complete",
                        None,
                        "concurrent fixture prompt",
                        "concurrent fixture result",
                        "{}",
                    ),
                )
                connection.commit()
                writer_committed.set()
        except BaseException as exc:  # pragma: no cover - reported by assertions below
            failures.append(exc)

    migration_thread = threading.Thread(target=run_migration)
    writer_thread = threading.Thread(target=concurrent_writer)
    migration_thread.start()
    try:
        assert backup_finished.wait(5)
        writer_thread.start()
        assert writer_attempted.wait(5)
        # The backup helper is paused after producing its snapshot.  A commit
        # here would reproduce the former unprotected backup-to-DDL gap.
        assert not writer_committed.wait(0.2)
    finally:
        allow_migration_to_continue.set()
    migration_thread.join(10)
    writer_thread.join(10)

    assert not migration_thread.is_alive()
    assert not writer_thread.is_alive()
    assert not failures
    assert writer_committed.is_set()
    assert len(migration_results) == 1
    result = migration_results[0]
    assert result.backup_path is not None

    with closing(sqlite3.connect(result.backup_path)) as backup_connection:
        assert backup_connection.execute(
            "SELECT count(*) FROM tasks WHERE id='writer-after-backup'"
        ).fetchone()[0] == 0
    with closing(open_database(database, readonly=True)) as live_connection:
        assert live_connection.execute(
            "SELECT count(*) FROM tasks WHERE id='writer-after-backup'"
        ).fetchone()[0] == 1
        assert live_connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_backup_rejects_claimed_current_schema_damage_and_foreign_key_orphans(
    tmp_path: Path,
) -> None:
    missing_table = tmp_path / "missing-table.sqlite3"
    migrate_database(missing_table, create_backup=False)
    with closing(sqlite3.connect(missing_table)) as connection:
        connection.execute("DROP TABLE memory_conflict_members")
        connection.commit()
    destination = tmp_path / "invalid-schema-backup.sqlite3"
    with pytest.raises(BackupVerificationError):
        backup_database(missing_table, destination)
    assert not destination.exists()

    orphaned = tmp_path / "orphaned.sqlite3"
    migrate_database(orphaned, create_backup=False)
    with closing(sqlite3.connect(orphaned)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO memory_sources
                (source_id,record_id,source_type,source_uri,source_fragment,
                 source_hash,source_commit_sha,source_mtime_ns,observed_at,
                 producer,author,source_dedupe_key)
            VALUES ('orphan','missing','fixture',NULL,NULL,NULL,NULL,NULL,
                    '2026-07-14T00:00:00+00:00','fixture','fixture',?)
            """,
            ("0" * 64,),
        )
        connection.commit()
    with pytest.raises(BackupVerificationError, match="foreign-key"):
        verify_backup(orphaned)


def test_restore_over_corrupt_target_keeps_raw_safety_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    migrate_database(source, create_backup=False)
    backup = backup_database(source, tmp_path / "source.backup.sqlite3")
    target = tmp_path / "target.sqlite3"
    corrupt_bytes = b"synthetic-corrupt-sqlite-target"
    target.write_bytes(corrupt_bytes)

    restored = restore_database(
        backup.backup_path,
        target,
        confirm=True,
        safety_backup_directory=tmp_path / "restore-safety",
    )

    assert restored.safety_backup_path is not None
    assert restored.safety_backup_path.read_bytes() == corrupt_bytes
    assert verify_database(target, require_current=True).integrity_check == "ok"


def test_failed_final_restore_replace_preserves_the_original_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sqlite3"
    migrate_database(source, create_backup=False)
    backup = backup_database(source, tmp_path / "source.backup.sqlite3")
    target = tmp_path / "target.sqlite3"
    migrate_database(target, create_backup=False)
    with closing(open_database(target)) as connection:
        connection.execute(
            """
            INSERT INTO tasks
                (id,created_at,updated_at,route,status,project_path,prompt,
                 result,metadata,schema_version,state_json,privacy_version,
                 legacy_payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "preserve-on-failure",
                1.0,
                1.0,
                "fast_chat",
                "complete",
                None,
                "bounded fixture",
                None,
                "{}",
                "1.0",
                None,
                "stage003-v1",
                0,
            ),
        )
        connection.commit()

    real_replace = memory_migrations.os.replace

    def fail_final_replace(source_path, destination_path):
        source_candidate = Path(source_path)
        destination_candidate = Path(destination_path).resolve()
        if (
            destination_candidate == target.resolve()
            and source_candidate.name.endswith(".restore")
        ):
            raise OSError("synthetic final replace failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(memory_migrations.os, "replace", fail_final_replace)
    with pytest.raises(OSError, match="synthetic final replace failure"):
        restore_database(
            backup.backup_path,
            target,
            confirm=True,
            safety_backup_directory=tmp_path / "restore-safety",
        )

    with closing(open_database(target, readonly=True)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tasks WHERE id='preserve-on-failure'"
        ).fetchone()[0] == 1
    assert verify_database(target, require_current=True).integrity_check == "ok"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL regression")
def test_private_acl_replaces_explicit_broad_grants_and_safe_inheritance(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private-storage"
    directory.mkdir()
    artifact = directory / "memory.sqlite3"
    artifact.write_bytes(b"synthetic ACL fixture")
    for target, grant in (
        (directory, "*S-1-1-0:(OI)(CI)(M)"),
        (artifact, "*S-1-1-0:(R)"),
    ):
        result = subprocess.run(
            ["icacls.exe", str(target), "/grant", grant],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0

    restrict_private_directory(directory)
    restrict_private_file(artifact)
    inherited_child = directory / "memory.sqlite3-wal"
    inherited_child.write_bytes(b"synthetic WAL fixture")

    environment = os.environ.copy()
    environment["LOCAL_AGENT_ACL_TARGETS"] = (
        str(directory) + os.pathsep + str(artifact) + os.pathsep + str(inherited_child)
    )
    verification = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            r"""
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($target in $env:LOCAL_AGENT_ACL_TARGETS.Split([IO.Path]::PathSeparator)) {
    $acl = Get-Acl -LiteralPath $target
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($ownerSid.Value -ne $currentSid.Value) { exit 6 }
    $rules = @(
        $acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )
    )
    $otherAllows = @($rules | Where-Object {
        $_.AccessControlType -eq
            [Security.AccessControl.AccessControlType]::Allow -and
        $_.IdentityReference.Value -ne $currentSid.Value
    })
    if ($otherAllows.Count -ne 0) { exit 7 }
}
""",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert verification.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL regression")
def test_private_acl_failure_diagnostic_is_bounded_and_path_free(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    synthetic_path = str(tmp_path / "must-not-appear")
    environment["LOCAL_AGENT_PRIVATE_PATH"] = synthetic_path
    environment["LOCAL_AGENT_PRIVATE_KIND"] = "invalid"

    failed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            memory_migrations._WINDOWS_PRIVATE_DACL_ENCODED,
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert failed.returncode == 41
    assert "LOCESTRA_ACL_ERROR stage=input type=" in failed.stderr
    assert synthetic_path not in failed.stderr
