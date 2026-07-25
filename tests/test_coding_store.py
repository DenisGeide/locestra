from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest
import services.coding.migrations as coding_migrations

from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskStateV1,
    CodingTaskStatus,
    WorktreeRecordV1,
)
from services.coding.store import (
    CodingTaskStore,
    ConcurrentTransitionError,
    InvalidTransitionError,
    TaskAlreadyExistsError,
    WorktreeRegistryError,
)
from tests.coding_fixtures import coding_fixture


def _created_state(repository: Path, *, task_id: str) -> CodingTaskStateV1:
    now = datetime.now(timezone.utc)
    request = CodingTaskRequestV1(
        task_id=task_id,
        request_id=f"request-{task_id}",
        goal="Fix the synthetic calculator and verify the bounded diff.",
        repository_path=str(repository),
        mode=CodingMode.WRITE,
        risk=CodingRisk.MEDIUM,
        constraints=["Never push."],
        acceptance_criteria=["The standard-library tests pass."],
        verification_plan=["Run unittest discovery."],
        permissions=CodingPermissionsV1(modify_files=True),
    )
    return CodingTaskStateV1(
        request=request,
        status=CodingTaskStatus.CREATED,
        source_repository=str(repository),
        created_at=now,
        updated_at=now,
    )


def _advance(
    state: CodingTaskStateV1,
    status: CodingTaskStatus,
    *,
    worktree: WorktreeRecordV1 | None = None,
) -> CodingTaskStateV1:
    updates: dict[str, object] = {
        "status": status,
        "updated_at": state.updated_at + timedelta(seconds=1),
    }
    if worktree is not None:
        updates["worktree"] = worktree
    return CodingTaskStateV1.model_validate(
        state.model_copy(update=updates).model_dump(mode="python")
    )


def _record(state: CodingTaskStateV1, worktree_path: Path, branch: str) -> WorktreeRecordV1:
    now = state.updated_at + timedelta(seconds=1)
    return WorktreeRecordV1(
        task_id=state.request.task_id,
        source_repository=state.source_repository,
        worktree_path=str(worktree_path),
        branch=branch,
        base_commit="a" * 40,
        owner_token_hash=sha256(f"owner:{state.request.task_id}".encode()).hexdigest(),
        status="active",
        owner_pid=os.getpid(),
        created_at=now,
        heartbeat_at=now,
    )


def test_store_permission_hardening_is_default_and_test_bypass_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardened_paths: list[Path] = []

    def reject_hardening(path: str | Path) -> None:
        hardened_paths.append(Path(path).resolve())
        raise OSError("synthetic permission failure")

    monkeypatch.setattr(
        coding_migrations,
        "restrict_database_storage",
        reject_hardening,
    )
    protected = tmp_path / "protected" / "coding.sqlite3"
    with pytest.raises(
        coding_migrations.CodingMigrationError,
        match="permission hardening failed",
    ):
        CodingTaskStore(protected)

    unit_only = tmp_path / "unit-only" / "coding.sqlite3"
    store = CodingTaskStore(unit_only, harden_permissions=False)

    assert hardened_paths == [protected.resolve()]
    assert store.database_path == unit_only.resolve()
    assert store.database_path.is_file()


def test_store_compare_and_swap_and_append_only_event_chain(tmp_path: Path):
    with coding_fixture(run_id="store-cas") as fixture:
        store = CodingTaskStore(
            tmp_path / "coding.sqlite3",
            harden_permissions=False,
        )
        created = _created_state(fixture.repository, task_id="store-cas-task")

        assert store.create(created) == 1
        with pytest.raises(TaskAlreadyExistsError):
            store.create(created)

        inspected = _advance(created, CodingTaskStatus.INSPECTED)
        assert store.transition(
            inspected,
            "task.inspected",
            reason_code="inspection.complete",
            expected_version=1,
        ) == 2

        planned = _advance(inspected, CodingTaskStatus.PLANNED)
        with pytest.raises(ConcurrentTransitionError):
            store.transition(planned, "task.planned", expected_version=1)
        assert store.load(created.request.task_id) == inspected

        with sqlite3.connect(store.database_path) as connection:
            rows = connection.execute(
                "SELECT state_version,event_type,from_status,to_status,reason_code,state_sha256 "
                "FROM coding_task_events WHERE task_id=? ORDER BY state_version",
                (created.request.task_id,),
            ).fetchall()
            assert [row[0] for row in rows] == [1, 2]
            assert rows[0][1:4] == ("task.created", None, "created")
            assert rows[1][1:5] == (
                "task.inspected",
                "created",
                "inspected",
                "inspection.complete",
            )
            current_hash = connection.execute(
                "SELECT state_sha256 FROM coding_tasks WHERE task_id=?",
                (created.request.task_id,),
            ).fetchone()[0]
            assert rows[-1][5] == current_hash
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    "UPDATE coding_task_events SET event_type='tampered' WHERE task_id=?",
                    (created.request.task_id,),
                )

        status = store.status()
        assert status["event_chain_consistent"] is True
        assert status["counts"] == {"tasks": 1, "events": 2, "worktrees": 0}


def test_store_rejects_invalid_transition_and_request_mutation(tmp_path: Path):
    with coding_fixture(run_id="store-invariants") as fixture:
        store = CodingTaskStore(
            tmp_path / "coding.sqlite3",
            harden_permissions=False,
        )
        created = _created_state(fixture.repository, task_id="store-invariant-task")
        store.create(created)

        planned = _advance(created, CodingTaskStatus.PLANNED)
        with pytest.raises(InvalidTransitionError, match="not allowed"):
            store.transition(planned, "task.planned", expected_version=1)

        changed_request = created.request.model_copy(update={"goal": "Changed immutable goal."})
        changed = _advance(created, CodingTaskStatus.INSPECTED).model_copy(
            update={"request": changed_request}
        )
        with pytest.raises(InvalidTransitionError, match="request is immutable"):
            store.transition(changed, "task.inspected", expected_version=1)

        assert store.load(created.request.task_id) == created
        assert store.status()["counts"]["events"] == 1


def test_worktree_registry_enforces_identity_live_path_and_state_linkage(tmp_path: Path):
    with coding_fixture(run_id="store-registry") as fixture:
        store = CodingTaskStore(
            tmp_path / "coding.sqlite3",
            harden_permissions=False,
        )
        first = _created_state(fixture.repository, task_id="registry-one")
        second = _created_state(fixture.repository, task_id="registry-two")
        store.create(first)
        store.create(second)

        external_worktree = fixture.add_worktree("registry-store")
        record = _record(first, external_worktree.path, external_worktree.branch)
        store.register_worktree(record)
        assert store.worktree(first.request.task_id) == record

        colliding = _record(second, external_worktree.path, external_worktree.branch)
        with pytest.raises(WorktreeRegistryError, match="path, or branch"):
            store.register_worktree(colliding)

        inspected = _advance(first, CodingTaskStatus.INSPECTED)
        assert store.transition(inspected, "task.inspected", expected_version=1) == 2
        planned = _advance(inspected, CodingTaskStatus.PLANNED)
        assert store.transition(planned, "task.planned", expected_version=2) == 3
        isolated = _advance(planned, CodingTaskStatus.ISOLATED, worktree=record)
        assert store.transition(isolated, "task.isolated", expected_version=3) == 4

        tampered_record = record.model_copy(update={"branch": f"{record.branch}-tampered"})
        tampered_state = _advance(
            isolated,
            CodingTaskStatus.ISOLATED,
            worktree=tampered_record,
        )
        with pytest.raises(WorktreeRegistryError, match="identity"):
            store.transition(tampered_state, "task.heartbeat", expected_version=4)

        heartbeat = record.model_copy(
            update={"heartbeat_at": record.heartbeat_at + timedelta(seconds=1)}
        )
        store.update_worktree(heartbeat)
        completed_at = heartbeat.heartbeat_at + timedelta(seconds=1)
        complete = heartbeat.model_copy(
            update={
                "status": "complete",
                "heartbeat_at": completed_at,
                "completed_at": completed_at,
            }
        )
        store.update_worktree(complete)
        removed = complete.model_copy(
            update={
                "status": "removed",
                "heartbeat_at": complete.heartbeat_at + timedelta(seconds=1),
            }
        )
        store.update_worktree(removed)
        assert store.worktree(first.request.task_id) == removed

        status = store.status()
        assert status["counts"] == {"tasks": 2, "events": 5, "worktrees": 1}
        assert status["worktree_statuses"] == {"removed": 1}
