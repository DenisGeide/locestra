from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import psutil

from services.coding.config import get_coding_policy
from services.coding.contracts import CodingTaskStateV1, CodingTaskStatus, WorktreeRecordV1
from services.coding.migrations import CodingMigrationError
from services.coding.store import CodingStoreError, CodingTaskStore
from services.coding.worktrees import WorktreeError, WorktreeManager


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WORKTREE_IDENTITY_FIELDS = (
    "task_id",
    "source_repository",
    "worktree_path",
    "branch",
    "base_commit",
    "owner_token_hash",
)
_ORPHAN_TRANSITIONABLE = {
    CodingTaskStatus.ISOLATED,
    CodingTaskStatus.EXECUTING,
    CodingTaskStatus.VERIFYING,
    CodingTaskStatus.REVIEWING,
    CodingTaskStatus.HANDOFF_READY,
    CodingTaskStatus.ORPHANED,
}


class CodingCliError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent-coding",
        description="Safe local Coding Engine operator boundary",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="verify the durable store and ownership registry")
    commands.add_parser(
        "recover",
        help="mark stale proven-owned records orphaned without deleting their paths",
    )
    cleanup = commands.add_parser(
        "cleanup",
        help="preview or remove one clean, completed, proven-owned Git worktree",
    )
    cleanup.add_argument("--task-id", required=True)
    cleanup.add_argument(
        "--confirm",
        help="apply cleanup only when this value exactly equals --task-id",
    )
    return parser


def _same_worktree_identity(
    left: WorktreeRecordV1,
    right: WorktreeRecordV1,
) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in _WORKTREE_IDENTITY_FIELDS
    )


def _registry_status(
    manager: WorktreeManager,
    store: CodingTaskStore,
    *,
    durable_count: int,
) -> dict[str, object]:
    statuses: Counter[str] = Counter()
    invalid_records = 0
    stale_active = 0
    mirror_missing = 0
    mirror_identity_mismatch = 0
    mirror_status_mismatch = 0
    now = datetime.now(timezone.utc)
    record_count = 0

    for entry in sorted(manager.records_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not entry.name.endswith(".json"):
            invalid_records += 1
            continue
        task_id = entry.name[:-5]
        if not _SAFE_TASK_ID.fullmatch(task_id):
            invalid_records += 1
            continue
        try:
            record = manager.load(task_id)
        except (WorktreeError, ValueError, OSError):
            invalid_records += 1
            continue
        if record is None or record.task_id != task_id:
            invalid_records += 1
            continue

        record_count += 1
        statuses[record.status] += 1
        if record.status == "active":
            age = (now - record.heartbeat_at).total_seconds()
            if age > manager.policy.lease_stale_seconds or not psutil.pid_exists(record.owner_pid):
                stale_active += 1
        try:
            durable = store.worktree(task_id)
        except CodingStoreError:
            mirror_identity_mismatch += 1
            continue
        if durable is None:
            mirror_missing += 1
        elif not _same_worktree_identity(record, durable):
            mirror_identity_mismatch += 1
        elif record.status != durable.status:
            mirror_status_mismatch += 1

    lease_entries = sum(1 for _ in manager.leases_dir.iterdir())
    create_intents = manager.creation_intent_status()
    return {
        "record_count": record_count,
        "record_statuses": dict(sorted(statuses.items())),
        "invalid_records": invalid_records,
        "stale_active_records": stale_active,
        "requires_recovery": stale_active > 0,
        "lease_entries": lease_entries,
        "durable_record_count": durable_count,
        "durable_count_mismatch": durable_count != record_count,
        "mirror_missing": mirror_missing,
        "mirror_identity_mismatch": mirror_identity_mismatch,
        "mirror_status_mismatch": mirror_status_mismatch,
        "create_intents": create_intents["pending"],
        "prepared_create_intents": create_intents["prepared"],
        "added_create_intents": create_intents["added"],
        "live_create_intents": create_intents["live"],
        "stale_create_intents": create_intents["stale"],
        "invalid_create_intents": create_intents["invalid"],
        "owned_root_marker_valid": True,
    }


def _status_payload(
    store: CodingTaskStore,
    manager: WorktreeManager,
) -> tuple[dict[str, object], bool]:
    database = store.status()
    counts = database.get("counts")
    durable_count = int(counts.get("worktrees", 0)) if isinstance(counts, dict) else -1
    registry = _registry_status(manager, store, durable_count=durable_count)
    database_healthy = (
        database.get("integrity_check") == "ok"
        and database.get("foreign_key_violations") == 0
        and database.get("event_chain_consistent") is True
    )
    registry_healthy = (
        registry["invalid_records"] == 0
        and registry["stale_active_records"] == 0
        and registry["durable_count_mismatch"] is False
        and registry["mirror_missing"] == 0
        and registry["mirror_identity_mismatch"] == 0
        and registry["mirror_status_mismatch"] == 0
        and registry["create_intents"] == 0
        and registry["invalid_create_intents"] == 0
    )
    healthy = bool(database_healthy and registry_healthy)
    return (
        {
            "schema_version": "1.0",
            "command": "status",
            "status": "ok" if healthy else "degraded",
            "healthy": healthy,
            "policy_version": manager.policy.policy_version,
            "database": database,
            "ownership_registry": registry,
        },
        healthy,
    )


def _recover(
    store: CodingTaskStore,
    manager: WorktreeManager,
) -> tuple[dict[str, object], bool]:
    creation_recovery = manager.recover_creation_intents()
    recovered: list[WorktreeRecordV1] = list(
        creation_recovery.orphaned_records
    )
    # Reconcile a crash between the filesystem registry's "complete" write and
    # the durable task transition.  A non-completed task must never leave a
    # cleanable completed mirror; preserve it as orphaned instead.
    for entry in sorted(manager.records_dir.glob("*.json")):
        task_id = entry.stem
        if not _SAFE_TASK_ID.fullmatch(task_id):
            continue
        try:
            record = manager.load(task_id)
            state = store.load(task_id)
            durable = store.worktree(task_id)
            if (
                record is None
                or durable is None
                or state is None
                or record.status != "complete"
                or state.status is CodingTaskStatus.COMPLETED
                or not _same_worktree_identity(record, durable)
                or durable.status not in {"active", "complete"}
            ):
                continue
            recovered.append(manager.mark_orphaned(task_id))
        except (CodingStoreError, WorktreeError, ValueError, OSError):
            continue
    seen = {item.task_id for item in recovered}
    recovered.extend(
        item for item in manager.recover_orphans() if item.task_id not in seen
    )
    durable_updates = 0
    task_state_updates = 0
    registry_only = 0
    unchanged_task_states = 0
    synchronization_errors = 0

    for record in recovered:
        try:
            durable = store.worktree(record.task_id)
            if durable is None:
                registry_only += 1
                continue
            if not _same_worktree_identity(record, durable):
                synchronization_errors += 1
                continue
            store.update_worktree(record)
            durable_updates += 1

            state = store.load(record.task_id)
            if state is None or state.status not in _ORPHAN_TRANSITIONABLE:
                unchanged_task_states += 1
                continue
            candidate = CodingTaskStateV1.model_validate(
                state.model_copy(
                    update={
                        "status": CodingTaskStatus.ORPHANED,
                        "worktree": record,
                        "updated_at": max(
                            datetime.now(timezone.utc),
                            state.updated_at + timedelta(microseconds=1),
                        ),
                    }
                ).model_dump(mode="python")
            )
            store.transition(
                candidate,
                "worktree.orphaned",
                reason_code="recovery.stale_owner",
                expected_version=store.version(record.task_id),
            )
            task_state_updates += 1
        except (CodingStoreError, CodingMigrationError, ValueError, OSError):
            # Preserve the orphaned operational record and path.  A partial
            # mirror failure is reported only as bounded metadata for repair.
            synchronization_errors += 1

    post_status, post_recovery_healthy = _status_payload(store, manager)
    post_registry = post_status["ownership_registry"]
    assert isinstance(post_registry, dict)
    complete = (
        registry_only == 0
        and synchronization_errors == 0
        and creation_recovery.unresolved == 0
        and creation_recovery.invalid == 0
        and creation_recovery.live == 0
        and post_recovery_healthy
    )
    return (
        {
            "schema_version": "1.0",
            "command": "recover",
            "status": "ok" if complete else "partial",
            "recovered_records": len(recovered),
            "recovered_task_ids": sorted(item.task_id for item in recovered),
            "compensated_create_intents": creation_recovery.compensated,
            "finalized_create_intents": creation_recovery.finalized,
            "orphaned_create_intents": len(creation_recovery.orphaned_records),
            "unresolved_create_intents": creation_recovery.unresolved,
            "invalid_create_intents": creation_recovery.invalid,
            "live_create_intents": creation_recovery.live,
            "durable_updates": durable_updates,
            "task_state_updates": task_state_updates,
            "unchanged_task_states": unchanged_task_states,
            "registry_only_records": registry_only,
            "synchronization_errors": synchronization_errors,
            "post_recovery_healthy": post_recovery_healthy,
            "remaining_invalid_records": post_registry["invalid_records"],
            "remaining_stale_active_records": post_registry["stale_active_records"],
            "remaining_mirror_errors": (
                int(post_registry["mirror_missing"])
                + int(post_registry["mirror_identity_mismatch"])
                + int(post_registry["mirror_status_mismatch"])
            ),
            "paths_deleted": creation_recovery.paths_deleted,
        },
        complete,
    )


def _cleanup(
    args: argparse.Namespace,
    store: CodingTaskStore,
    manager: WorktreeManager,
) -> tuple[dict[str, object], bool]:
    task_id = str(args.task_id)
    if not _SAFE_TASK_ID.fullmatch(task_id):
        raise CodingCliError("coding.cleanup.invalid_task_id")
    record = manager.load(task_id)
    if record is None:
        raise CodingCliError("coding.cleanup.record_missing")
    if record.status != "complete":
        raise CodingCliError("coding.cleanup.not_complete")

    durable = store.worktree(task_id)
    state = store.load(task_id)
    if (
        durable is None
        or not _same_worktree_identity(record, durable)
        or durable.status != record.status
    ):
        raise CodingCliError("coding.cleanup.registry_mismatch")
    if (
        state is None
        or state.status is not CodingTaskStatus.COMPLETED
        or state.worktree is None
        or not _same_worktree_identity(record, state.worktree)
    ):
        raise CodingCliError("coding.cleanup.task_not_completed")

    if args.confirm is None:
        return (
            {
                "schema_version": "1.0",
                "command": "cleanup",
                "status": "preview",
                "task_id": task_id,
                "eligible": True,
                "applied": False,
                "paths_deleted": 0,
            },
            True,
        )
    if args.confirm != task_id:
        raise CodingCliError("coding.cleanup.confirmation_mismatch")

    # Re-read both registries immediately before the exact owned operation.
    current = manager.load(task_id)
    current_durable = store.worktree(task_id)
    if (
        current is None
        or current_durable is None
        or current.status != "complete"
        or current_durable.status != "complete"
        or not _same_worktree_identity(current, current_durable)
    ):
        raise CodingCliError("coding.cleanup.registry_changed")
    result = manager.cleanup(task_id)
    store.update_worktree(result)
    removed = result.status == "removed"
    return (
        {
            "schema_version": "1.0",
            "command": "cleanup",
            "status": result.status,
            "task_id": task_id,
            "eligible": removed,
            "applied": True,
            "removed": removed,
            "paths_deleted": 1 if removed else 0,
        },
        removed,
    )


def execute(
    args: argparse.Namespace,
    *,
    store: CodingTaskStore | None = None,
    worktree_manager: WorktreeManager | None = None,
) -> int:
    effective_store = store or CodingTaskStore()
    effective_manager = worktree_manager or WorktreeManager(policy=get_coding_policy())
    if args.command == "status":
        payload, healthy = _status_payload(effective_store, effective_manager)
    elif args.command == "recover":
        payload, healthy = _recover(effective_store, effective_manager)
    elif args.command == "cleanup":
        payload, healthy = _cleanup(args, effective_store, effective_manager)
    else:
        raise CodingCliError("coding.command.unsupported")
    _print(payload)
    return 0 if healthy else 3


def main(
    argv: Sequence[str] | None = None,
    *,
    store: CodingTaskStore | None = None,
    worktree_manager: WorktreeManager | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return execute(args, store=store, worktree_manager=worktree_manager)
    except Exception as exc:
        # The operator boundary deliberately omits exception messages: they
        # may contain private paths, command output, or task material.
        _print(
            {
                "schema_version": "1.0",
                "command": getattr(args, "command", "unknown"),
                "status": "error",
                "error_type": type(exc).__name__,
                "reason_code": getattr(exc, "reason_code", "coding.operation_failed"),
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CodingCliError", "build_parser", "execute", "main"]
