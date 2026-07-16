import sqlite3

from services import common
from services.contracts import TaskStateV1


def use_database(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    return tmp_path / "memory.sqlite3"


def test_new_database_has_additive_v1_state_columns(tmp_path, monkeypatch):
    database = use_database(tmp_path, monkeypatch)
    with common.db() as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }

    assert database.exists()
    assert {"updated_at", "schema_version", "state_json"}.issubset(columns)


def test_legacy_database_migrates_without_losing_or_fabricating_state(tmp_path, monkeypatch):
    database = use_database(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
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
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-1", 100.0, "fast_chat", "complete", None, "hello", "world", "{}"),
        )

    with common.db() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = 'legacy-1'").fetchone()

    assert row["prompt"] == "hello"
    assert row["result"] == "world"
    assert row["updated_at"] == 100.0
    assert row["state_json"] is None
    assert common.load_task_state("legacy-1") is None


def test_state_transition_preserves_created_at_and_excludes_raw_payload(tmp_path, monkeypatch):
    use_database(tmp_path, monkeypatch)
    times = iter([1000.0, 1010.0])
    monkeypatch.setattr(common.time, "time", lambda: next(times))

    common.save_task(
        "task-1",
        "local_code",
        "running",
        "private prompt body",
        r"C:\work\project",
    )
    common.save_task(
        "task-1",
        "local_code",
        "complete",
        "private prompt body",
        r"C:\work\project",
        "large raw result body",
    )

    state = common.load_task_state("task-1")
    assert isinstance(state, TaskStateV1)
    assert state.status == "complete"
    assert state.attempts == 1
    assert state.created_at.timestamp() == 1000.0
    assert state.updated_at.timestamp() == 1010.0

    with common.db() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = 'task-1'").fetchone()
    assert row["created_at"] == 1000.0
    assert row["updated_at"] == 1010.0
    assert row["schema_version"] == "1.0"
    assert "private prompt body" not in row["state_json"]
    assert "large raw result body" not in row["state_json"]


def test_failed_state_is_valid_and_database_health_is_objective(tmp_path, monkeypatch):
    use_database(tmp_path, monkeypatch)
    common.save_task("task-2", "browser", "failed", "open page", result="timeout")

    state = common.load_task_state("task-2")
    assert state is not None
    assert state.status == "failed"
    assert state.unresolved_errors
    assert common.task_store_ready()[0] is True


def test_fallback_transition_updates_executor_to_current_route(tmp_path, monkeypatch):
    use_database(tmp_path, monkeypatch)
    common.save_task("task-3", "local_code", "running", "change code", r"C:\work\project")
    common.save_task(
        "task-3",
        "codex_bundle",
        "ready",
        "change code",
        r"C:\work\project",
        metadata={"bundle": "inbox/task-3-codex.md"},
    )

    state = common.load_task_state("task-3")
    assert state is not None
    assert state.status == "ready"
    assert state.executor == "codex_bundle"
    assert state.attempts == 1


def test_new_task_persistence_redacts_secrets_and_marks_legacy_boundary(tmp_path, monkeypatch):
    use_database(tmp_path, monkeypatch)
    synthetic = "sk-proj-" + "Aa9Zz8Yy7Xx6Ww5Vv4Uu" * 2
    common.save_task(
        "task-private",
        "browser",
        "failed",
        f"inspect token {synthetic}",
        result=f"failure contained {synthetic}",
        metadata={"authorization": synthetic, "safe": "bounded evidence"},
        error_summary=f"worker returned {synthetic}",
    )

    with common.db() as connection:
        row = connection.execute(
            "SELECT prompt,result,metadata,state_json,privacy_version,legacy_payload FROM tasks WHERE id=?",
            ("task-private",),
        ).fetchone()
    serialized = "\n".join(str(row[key]) for key in row.keys())
    assert synthetic not in serialized
    assert "REDACTED" in serialized
    assert row["privacy_version"] == "stage003-v1"
    assert row["legacy_payload"] == 0


def test_optional_memory_schema_damage_does_not_block_task_journal(tmp_path, monkeypatch):
    database = use_database(tmp_path, monkeypatch)
    with common.db() as connection:
        connection.execute("DROP TABLE memory_sources")
        connection.commit()
    common._INITIALIZED_DATABASES.discard(database.resolve())

    assert common.task_store_ready()[0] is True
    common.save_task("task-after-memory-damage", "fast_chat", "complete", "still works")
    assert common.load_task_state("task-after-memory-damage") is not None


def test_legacy_payload_marker_quarantines_even_parseable_state_json(tmp_path, monkeypatch):
    use_database(tmp_path, monkeypatch)
    common.save_task("legacy-shaped", "fast_chat", "complete", "new task")
    with common.db() as connection:
        connection.execute(
            "UPDATE tasks SET legacy_payload=1 WHERE id='legacy-shaped'"
        )
        connection.commit()

    assert common.load_task_state("legacy-shaped") is None
