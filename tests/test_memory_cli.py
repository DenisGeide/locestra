import json
import io
import sys

from services.memory.cli import main
from services.memory.contracts import MemorySourceV1, MemoryUpsertV1
from services.memory.migrations import open_database
from services.memory.store import MemoryStore


def run_cli(database, *args):
    return main(["--database", str(database), *map(str, args)])


def test_cli_add_list_confirm_retrieve_and_export(tmp_path, capsys):
    database = tmp_path / "memory.sqlite3"
    project = tmp_path / "project"
    project.mkdir()
    assert run_cli(
        database,
        "add",
        "--scope",
        "project",
        "--project",
        project,
        "--type",
        "project_knowledge",
        "--subject",
        "project.python_version",
        "--value-json",
        '{"version":"3.12"}',
    ) == 0
    created = json.loads(capsys.readouterr().out)
    record_id = created["record_id"]

    assert run_cli(database, "confirm", record_id) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "confirmed"
    assert run_cli(
        database, "retrieve", "python version", "--project", project, "--max-chars", "500"
    ) == 0
    retrieved = json.loads(capsys.readouterr().out)
    assert retrieved["items"][0]["record_id"] == record_id

    output = tmp_path / "export.json"
    assert run_cli(
        database,
        "export",
        "--scope",
        "project",
        "--project",
        project,
        "--output",
        output,
    ) == 0
    capsys.readouterr()
    assert record_id in output.read_text(encoding="utf-8")


def test_cli_enforces_scope_and_exact_purge_confirmation(tmp_path, capsys):
    database = tmp_path / "memory.sqlite3"
    assert run_cli(database, "list", "--scope", "project") == 2
    assert "ERROR" in capsys.readouterr().err

    store = MemoryStore(database)
    record = store.upsert(
        MemoryUpsertV1(
            record_type="project_knowledge",
            scope="project",
            subject="project.cli_purge",
            value="fixture",
            source=MemorySourceV1(source_type="user_assertion", uri="user://manual"),
            project_path=str(tmp_path),
        )
    )
    assert run_cli(database, "purge", record.record_id, "--confirm-record-id", "wrong") == 2
    capsys.readouterr()
    assert store.get(record.record_id)
    assert run_cli(
        database,
        "purge",
        record.record_id,
        "--confirm-record-id",
        record.record_id,
    ) == 0


def test_cli_legacy_purge_is_preview_first_and_explicit(tmp_path, capsys):
    database = tmp_path / "memory.sqlite3"
    store = MemoryStore(database)
    with open_database(database) as connection:
        connection.execute(
            """
            INSERT INTO tasks
                (id,created_at,updated_at,route,status,project_path,prompt,result,
                 metadata,privacy_version,legacy_payload)
            VALUES ('legacy',1,1,'fast_chat','complete',NULL,'old','old','{}',NULL,1)
            """
        )
        connection.commit()

    assert run_cli(database, "legacy-purge", "--task-id", "legacy") == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview == {"deleted": 0, "matched": 1, "preview": True}
    assert run_cli(
        database,
        "legacy-purge",
        "--task-id",
        "legacy",
        "--apply",
        "--confirm",
        "PURGE-LEGACY-TASKS",
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["deleted"] == 1
    with open_database(database, readonly=True) as connection:
        assert connection.execute("SELECT count(*) FROM tasks WHERE id='legacy'").fetchone()[0] == 0


def test_cli_status_reports_schema_without_payload(tmp_path, capsys):
    database = tmp_path / "memory.sqlite3"
    assert run_cli(database, "status") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 3
    assert payload["journal_mode"] == "wal"
    assert payload["task_rows"] == 0
    assert payload["filtered_task_rows"] == 0

    assert run_cli(database, "verify-backup", database) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["user_version"] == 3
    assert verified["integrity_check"] == "ok"


def test_cli_accepts_value_from_stdin_without_command_line_payload(
    tmp_path, capsys, monkeypatch
):
    database = tmp_path / "memory.sqlite3"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"version":"3.13"}'))

    assert run_cli(
        database,
        "add",
        "--scope",
        "project",
        "--project",
        project,
        "--type",
        "project_knowledge",
        "--subject",
        "project.python_version",
        "--value-stdin",
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["value"] == {"version": "3.13"}
