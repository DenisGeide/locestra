from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.coding import worktrees as worktree_module
from services.coding.cli import main
from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskStateV1,
    CodingTaskStatus,
    ExecutorKind,
    ReviewResultV1,
    ReviewVerdict,
    WorktreeRecordV1,
)
from services.coding.git import resolve_repository
from services.coding.store import CodingTaskStore
from services.coding.worktrees import WorktreeManager
from tests.coding_fixtures import coding_fixture, file_snapshot


def _manager(fixture) -> WorktreeManager:
    return WorktreeManager(
        registry_root=fixture.root / "cli-registry",
        owned_worktree_root=fixture.root / "cli-worktrees",
    )


def _created_state(repository: Path, task_id: str) -> CodingTaskStateV1:
    now = datetime.now(timezone.utc)
    request = CodingTaskRequestV1(
        task_id=task_id,
        request_id=f"request-{task_id}",
        goal="Exercise the bounded Coding Engine operator lifecycle.",
        repository_path=str(repository),
        mode=CodingMode.WRITE,
        risk=CodingRisk.LOW,
        constraints=["Never push or delete an unowned path."],
        acceptance_criteria=["The owned worktree lifecycle remains auditable."],
        verification_plan=["Use deterministic fixture evidence."],
        permissions=CodingPermissionsV1(modify_files=True),
    )
    return CodingTaskStateV1(
        request=request,
        status=CodingTaskStatus.CREATED,
        source_repository=str(repository),
        created_at=now,
        updated_at=now,
    )


def _transition(
    store: CodingTaskStore,
    state: CodingTaskStateV1,
    status: CodingTaskStatus,
    *,
    worktree: WorktreeRecordV1 | None = None,
    review: ReviewResultV1 | None = None,
) -> CodingTaskStateV1:
    updates: dict[str, object] = {
        "status": status,
        "updated_at": state.updated_at + timedelta(seconds=1),
    }
    if worktree is not None:
        updates["worktree"] = worktree
    if review is not None:
        updates["review"] = review
    candidate = CodingTaskStateV1.model_validate(
        state.model_copy(update=updates).model_dump(mode="python")
    )
    store.transition(
        candidate,
        f"task.{status.value}",
        expected_version=store.version(state.request.task_id),
    )
    return candidate


def _isolated_task(
    fixture,
    *,
    task_id: str,
    stale: bool = False,
) -> tuple[CodingTaskStore, WorktreeManager, CodingTaskStateV1, WorktreeRecordV1]:
    store = CodingTaskStore(
        fixture.root / f"{task_id}.sqlite3",
        harden_permissions=False,
    )
    manager = _manager(fixture)
    identity = resolve_repository(str(fixture.repository))
    state = _created_state(identity.canonical_root, task_id)
    store.create(state)
    state = _transition(store, state, CodingTaskStatus.INSPECTED)
    state = _transition(store, state, CodingTaskStatus.PLANNED)
    record = manager.create(task_id=task_id, repository=identity)
    if stale:
        heartbeat = datetime.now(timezone.utc) - timedelta(
            seconds=manager.policy.lease_stale_seconds + 5
        )
        record = record.model_copy(
            update={
                "created_at": heartbeat - timedelta(seconds=1),
                "heartbeat_at": heartbeat,
            }
        )
        manager._write_record(record)
    store.register_worktree(record)
    state = _transition(store, state, CodingTaskStatus.ISOLATED, worktree=record)
    return store, manager, state, record


def _completed_task(
    fixture,
    *,
    task_id: str,
) -> tuple[CodingTaskStore, WorktreeManager, CodingTaskStateV1, WorktreeRecordV1]:
    store, manager, state, record = _isolated_task(fixture, task_id=task_id)
    state = _transition(store, state, CodingTaskStatus.EXECUTING)
    state = _transition(store, state, CodingTaskStatus.VERIFYING)
    state = _transition(store, state, CodingTaskStatus.REVIEWING)
    completed_record = manager.complete(task_id)
    store.update_worktree(completed_record)
    review = ReviewResultV1(
        reviewer_id=f"review-{task_id}",
        reviewer=ExecutorKind.DETERMINISTIC,
        verdict=ReviewVerdict.APPROVED,
        findings=[],
        checked_requirements=True,
        checked_tests=True,
        checked_diff_scope=True,
        checked_secrets=True,
        checked_constitution=True,
        summary="The disposable fixture result is safe for operator lifecycle testing.",
        reviewed_at=datetime.now(timezone.utc),
    )
    state = _transition(
        store,
        state,
        CodingTaskStatus.COMPLETED,
        worktree=completed_record,
        review=review,
    )
    return store, manager, state, completed_record


def test_status_reports_bounded_store_and_registry_health_without_paths_or_owner_tokens(capsys):
    with coding_fixture(run_id="cli-status") as fixture:
        store = CodingTaskStore(
            fixture.root / "coding.sqlite3",
            harden_permissions=False,
        )
        manager = _manager(fixture)

        assert main(["status"], store=store, worktree_manager=manager) == 0
        output = capsys.readouterr().out
        payload = json.loads(output)

        assert payload["healthy"] is True
        assert payload["database"]["application_id"] == 0x4C414943
        assert payload["database"]["integrity_check"] == "ok"
        assert payload["ownership_registry"]["record_count"] == 0
        assert str(fixture.root) not in output
        assert "owner_token_hash" not in output


def test_status_fails_closed_on_malformed_registry_metadata_without_echoing_it(capsys):
    with coding_fixture(run_id="cli-invalid-registry") as fixture:
        store = CodingTaskStore(
            fixture.root / "coding.sqlite3",
            harden_permissions=False,
        )
        manager = _manager(fixture)
        synthetic_secret = "sk-" + "fixture-only-not-a-real-credential-1234567890"
        (manager.records_dir / "malformed.json").write_text(
            synthetic_secret,
            encoding="utf-8",
        )

        assert main(["status"], store=store, worktree_manager=manager) == 3
        output = capsys.readouterr().out
        payload = json.loads(output)

        assert payload["healthy"] is False
        assert payload["ownership_registry"]["invalid_records"] == 1
        assert synthetic_secret not in output
        assert str(manager.records_dir) not in output


def test_status_and_recover_reconcile_stale_create_intent_without_unowned_git_state(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="cli-create-intent-recovery") as fixture:
        store = CodingTaskStore(
            fixture.root / "coding.sqlite3",
            harden_permissions=False,
        )
        manager = _manager(fixture)
        identity = resolve_repository(str(fixture.repository))
        task_id = "cli-create-crash"

        def crash_before_record(_record, **_kwargs) -> None:
            raise SystemExit("simulated create crash")

        monkeypatch.setattr(manager, "_write_record", crash_before_record)
        with pytest.raises(SystemExit, match="simulated create crash"):
            manager.create(task_id=task_id, repository=identity)
        intent = manager._load_create_intent(task_id)
        assert intent is not None
        worktree = Path(intent.worktree_path)
        branch = intent.branch
        assert worktree.is_dir()

        recovery_manager = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
        )
        monkeypatch.setattr(worktree_module.psutil, "pid_exists", lambda _pid: False)

        assert main(
            ["status"], store=store, worktree_manager=recovery_manager
        ) == 3
        degraded = json.loads(capsys.readouterr().out)
        assert degraded["healthy"] is False
        assert degraded["ownership_registry"]["create_intents"] == 1
        assert degraded["ownership_registry"]["stale_create_intents"] == 1

        assert main(
            ["recover"], store=store, worktree_manager=recovery_manager
        ) == 0
        recovered = json.loads(capsys.readouterr().out)
        assert recovered["compensated_create_intents"] == 1
        assert recovered["unresolved_create_intents"] == 0
        assert recovered["paths_deleted"] == 1
        assert not worktree.exists()
        assert (
            fixture.git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 1
        )

        assert main(
            ["status"], store=store, worktree_manager=recovery_manager
        ) == 0
        healthy = json.loads(capsys.readouterr().out)
        assert healthy["healthy"] is True
        assert healthy["ownership_registry"]["create_intents"] == 0
        fixture.assert_remote_unchanged()


def test_status_detects_operational_and_durable_worktree_status_drift(capsys):
    with coding_fixture(run_id="cli-registry-drift") as fixture:
        store, manager, _, record = _isolated_task(
            fixture,
            task_id="cli-drift-task",
        )
        manager._write_record(
            record.model_copy(
                update={
                    "status": "orphaned",
                    "heartbeat_at": datetime.now(timezone.utc),
                }
            )
        )

        assert main(["status"], store=store, worktree_manager=manager) == 3
        output = capsys.readouterr().out
        payload = json.loads(output)

        assert payload["healthy"] is False
        assert payload["ownership_registry"]["mirror_status_mismatch"] == 1
        assert Path(record.worktree_path).is_dir()
        assert str(record.worktree_path) not in output


def test_recover_marks_stale_owned_task_orphaned_and_never_deletes_its_path(capsys):
    with coding_fixture(run_id="cli-recover") as fixture:
        store, manager, _, record = _isolated_task(
            fixture,
            task_id="cli-orphan-task",
            stale=True,
        )
        worktree = Path(record.worktree_path)
        source_before = file_snapshot(fixture.repository)

        assert main(["recover"], store=store, worktree_manager=manager) == 0
        output = capsys.readouterr().out
        payload = json.loads(output)

        assert payload["recovered_records"] == 1
        assert payload["task_state_updates"] == 1
        assert payload["paths_deleted"] == 0
        assert worktree.is_dir()
        assert manager.load(record.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.worktree(record.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.load(record.task_id).status is CodingTaskStatus.ORPHANED  # type: ignore[union-attr]
        assert file_snapshot(fixture.repository) == source_before
        assert str(worktree) not in output
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_recover_reconciles_crash_after_registry_completion_without_deleting_worktree(capsys):
    with coding_fixture(run_id="cli-recover-completion-crash") as fixture:
        store, manager, state, record = _isolated_task(
            fixture,
            task_id="cli-completion-crash-task",
        )
        state = _transition(store, state, CodingTaskStatus.EXECUTING)
        state = _transition(store, state, CodingTaskStatus.VERIFYING)
        state = _transition(store, state, CodingTaskStatus.REVIEWING)
        worktree = Path(record.worktree_path)
        source_before = file_snapshot(fixture.repository)

        # Simulate a process crash after the filesystem registry was finalized,
        # but before the matching SQLite worktree and task-state transitions.
        completed_record = manager.complete(record.task_id)
        assert completed_record.status == "complete"
        assert store.worktree(record.task_id).status == "active"  # type: ignore[union-attr]
        assert store.load(record.task_id).status is CodingTaskStatus.REVIEWING  # type: ignore[union-attr]

        assert main(["status"], store=store, worktree_manager=manager) == 3
        drift = json.loads(capsys.readouterr().out)
        assert drift["healthy"] is False
        assert drift["ownership_registry"]["mirror_status_mismatch"] == 1

        assert main(["recover"], store=store, worktree_manager=manager) == 0
        recovered = json.loads(capsys.readouterr().out)

        assert recovered["recovered_records"] == 1
        assert recovered["durable_updates"] == 1
        assert recovered["task_state_updates"] == 1
        assert recovered["post_recovery_healthy"] is True
        assert recovered["paths_deleted"] == 0
        assert worktree.is_dir()
        assert manager.load(record.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.worktree(record.task_id).status == "orphaned"  # type: ignore[union-attr]
        recovered_state = store.load(record.task_id)
        assert recovered_state is not None
        assert recovered_state.status is CodingTaskStatus.ORPHANED
        assert recovered_state.worktree is not None
        assert recovered_state.worktree.status == "orphaned"

        assert main(["status"], store=store, worktree_manager=manager) == 0
        healthy = json.loads(capsys.readouterr().out)
        assert healthy["healthy"] is True
        assert healthy["ownership_registry"]["mirror_status_mismatch"] == 0
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_cleanup_requires_exact_confirmation_then_removes_only_clean_completed_owned_worktree(capsys):
    with coding_fixture(run_id="cli-cleanup") as fixture:
        store, manager, _, record = _completed_task(
            fixture,
            task_id="cli-clean-task",
        )
        worktree = Path(record.worktree_path)
        source_before = file_snapshot(fixture.repository)

        assert main(
            ["cleanup", "--task-id", record.task_id, "--confirm", "wrong-task"],
            store=store,
            worktree_manager=manager,
        ) == 2
        denied_output = capsys.readouterr().out
        assert json.loads(denied_output)["reason_code"] == "coding.cleanup.confirmation_mismatch"
        assert worktree.is_dir()
        assert str(worktree) not in denied_output
        assert record.owner_token_hash not in denied_output

        assert main(
            ["cleanup", "--task-id", record.task_id],
            store=store,
            worktree_manager=manager,
        ) == 0
        preview = json.loads(capsys.readouterr().out)
        assert preview["status"] == "preview"
        assert preview["applied"] is False
        assert worktree.is_dir()

        assert main(
            ["cleanup", "--task-id", record.task_id, "--confirm", record.task_id],
            store=store,
            worktree_manager=manager,
        ) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "removed"
        assert result["paths_deleted"] == 1
        assert not worktree.exists()
        assert manager.load(record.task_id).status == "removed"  # type: ignore[union-attr]
        assert store.worktree(record.task_id).status == "removed"  # type: ignore[union-attr]
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_cleanup_preserves_dirty_completed_worktree_as_blocked_evidence(capsys):
    with coding_fixture(run_id="cli-dirty-cleanup") as fixture:
        store, manager, _, record = _completed_task(
            fixture,
            task_id="cli-dirty-task",
        )
        worktree = Path(record.worktree_path)
        evidence = worktree / "operator-evidence.txt"
        evidence.write_text("preserve this exact dirty evidence", encoding="utf-8")

        assert main(
            ["cleanup", "--task-id", record.task_id, "--confirm", record.task_id],
            store=store,
            worktree_manager=manager,
        ) == 3
        result = json.loads(capsys.readouterr().out)

        assert result["status"] == "cleanup_blocked"
        assert result["removed"] is False
        assert result["paths_deleted"] == 0
        assert evidence.read_text(encoding="utf-8") == "preserve this exact dirty evidence"
        assert manager.load(record.task_id).status == "cleanup_blocked"  # type: ignore[union-attr]
        assert store.worktree(record.task_id).status == "cleanup_blocked"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()
