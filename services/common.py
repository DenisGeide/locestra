from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from services.contracts import (
    CONTRACT_VERSION,
    AttemptOutcome,
    ExecutionAttemptV1,
    ExecutorName,
    PlanV1,
    RouteDecisionV1,
    RouteName,
    TaskStateV1,
    TaskStatus,
)
from services.orchestration.handoff import redact_bounded
from services.memory.migrations import (
    migrate_database,
    open_database,
    restrict_database_storage,
)
from services.memory.privacy import (
    sanitize_reference,
    sanitize_task_metadata,
    sanitize_task_text,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
INBOX_DIR = ROOT / "inbox"
LOG_DIR = ROOT / "logs"
RUN_DIR = ROOT / "run"
OUTPUT_DIR = ROOT / "outputs"

for directory in (DATA_DIR, INBOX_DIR, LOG_DIR, RUN_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_INITIALIZED_DATABASES: set[Path] = set()
_DATABASE_INIT_LOCK = threading.Lock()
_REQUIRED_TASK_COLUMNS = {
    "id", "created_at", "updated_at", "route", "status", "project_path",
    "prompt", "result", "metadata", "schema_version", "state_json",
    "privacy_version", "legacy_payload",
}


def _ensure_database(database_path: Path) -> None:
    """Run checked migrations once per process, never on ordinary reads."""

    resolved = database_path.resolve()
    with _DATABASE_INIT_LOCK:
        if resolved in _INITIALIZED_DATABASES:
            return
        restrict_database_storage(resolved)
        if resolved.is_file() and resolved.stat().st_size:
            try:
                with open_database(resolved, readonly=True) as existing:
                    version = int(existing.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row[1]) for row in existing.execute("PRAGMA table_info(tasks)")
                    }
                if version == 3 and _REQUIRED_TASK_COLUMNS.issubset(columns):
                    # Memory is optional.  A damaged/missing memory table must
                    # not make an otherwise valid task journal unavailable.
                    _INITIALIZED_DATABASES.add(resolved)
                    return
            except sqlite3.DatabaseError:
                pass
        migrate_database(resolved)
        _INITIALIZED_DATABASES.add(resolved)


def db() -> sqlite3.Connection:
    database_path = DATA_DIR / "memory.sqlite3"
    _ensure_database(database_path)
    return open_database(database_path)


_EXECUTOR_BY_ROUTE = {
    "auxiliary": ExecutorName.FAST_OLLAMA,
    "fast_chat": ExecutorName.FAST_OLLAMA,
    "strong_chat": ExecutorName.STRONG_OLLAMA,
    "local_code": ExecutorName.QWEN_CODE,
    "docs": ExecutorName.QWEN_CODE,
    "codex": ExecutorName.CODEX_CLI,
    "codex_bundle": ExecutorName.CODEX_BUNDLE,
    "browser": ExecutorName.PLAYWRIGHT,
    "image": ExecutorName.COMFYUI,
    "voice": ExecutorName.WHISPER,
    "vision": ExecutorName.DEGRADED_RESPONSE,
}


def _sanitize_plan_for_persistence(plan: PlanV1 | None) -> PlanV1 | None:
    if plan is None:
        return None
    return plan.model_copy(
        update={
            "goal": sanitize_task_text(plan.goal, "task-goal"),
            "subtasks": [sanitize_task_text(item, "subtask") for item in plan.subtasks],
            "constraints": [sanitize_task_text(item, "constraint") for item in plan.constraints],
            "acceptance_criteria": [
                sanitize_task_text(item, "acceptance") for item in plan.acceptance_criteria
            ],
            "approvals": [sanitize_task_text(item, "approval") for item in plan.approvals],
            "verification_plan": [
                sanitize_task_text(item, "verification") for item in plan.verification_plan
            ],
            # Long-term values remain in their controlled table.  The task
            # snapshot keeps only record refs and never duplicates retrieved
            # content or source fragments.
            "memory_context": [],
        }
    )


def _sanitize_task_state_for_persistence(state: TaskStateV1) -> TaskStateV1:
    artifacts = [
        artifact.model_copy(
            update={
                "path": sanitize_reference(artifact.path),
                "provenance": [
                    sanitize_reference(item) for item in artifact.provenance
                ],
            }
        )
        for artifact in state.artifacts
    ]
    attempts = [
        attempt.model_copy(
            update={
                "command_summaries": [
                    sanitize_task_text(item, "command-summary")
                    for item in attempt.command_summaries
                ],
                "error_summary": (
                    sanitize_task_text(attempt.error_summary, "error-summary")
                    if attempt.error_summary
                    else None
                ),
                "modified_files": [
                    sanitize_reference(item) for item in attempt.modified_files
                ],
                "artifact_refs": [
                    sanitize_reference(item) for item in attempt.artifact_refs
                ],
            }
        )
        for attempt in state.attempt_history
    ]
    return state.model_copy(
        update={
            "plan": _sanitize_plan_for_persistence(state.plan),
            "artifacts": artifacts,
            "artifact_refs": [sanitize_reference(item) for item in state.artifact_refs],
            "modified_files": [sanitize_reference(item) for item in state.modified_files],
            "unresolved_errors": [
                sanitize_task_text(item, "unresolved-error")
                for item in state.unresolved_errors
            ],
            "next_action": (
                sanitize_task_text(state.next_action, "next-action")
                if state.next_action
                else None
            ),
            "attempt_history": attempts,
        }
    )


def _build_task_state(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    route: str,
    status: str,
    project_path: str | None,
    now: float,
    route_decision: RouteDecisionV1 | None = None,
    plan: PlanV1 | None = None,
    actual_executor: ExecutorName | None = None,
    actual_model: str | None = None,
    actual_profile: str | None = None,
    fallback_used: bool = False,
    error_summary: str | None = None,
    reason_codes: list[str] | None = None,
    command_summaries: list[str] | None = None,
    modified_files: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> TaskStateV1:
    try:
        contract_status = TaskStatus(status)
    except ValueError as exc:
        raise ValueError(f"unsupported task status: {status}") from exc

    existing_row = connection.execute(
        "SELECT created_at, state_json FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    existing_state: TaskStateV1 | None = None
    if existing_row is not None and existing_row["state_json"]:
        existing_state = TaskStateV1.model_validate_json(existing_row["state_json"])

    created_timestamp = existing_row["created_at"] if existing_row is not None else now
    created_at = datetime.fromtimestamp(created_timestamp, timezone.utc)
    updated_at = datetime.fromtimestamp(now, timezone.utc)
    previous_attempts = existing_state.attempts if existing_state is not None else 0
    history = list(existing_state.attempt_history) if existing_state is not None else []
    executor = actual_executor or _EXECUTOR_BY_ROUTE.get(route) or (
        existing_state.executor if existing_state is not None else None
    )
    if executor is None:
        raise ValueError(f"route {route!r} has no executor mapping")
    bounded_error = (
        sanitize_task_text(redact_bounded(error_summary, 2_048), "error-summary")
        if error_summary
        else None
    )
    commands = [
        sanitize_task_text(redact_bounded(item, 2_048), "command-summary")
        for item in (command_summaries or [])
    ]
    attempt_files = list(dict.fromkeys(sanitize_reference(item) for item in (modified_files or [])))
    attempt_artifacts = list(dict.fromkeys(sanitize_reference(item) for item in (artifact_refs or [])))
    attempt_reasons = list(dict.fromkeys(reason_codes or []))

    terminal_outcome = {
        TaskStatus.COMPLETE: AttemptOutcome.COMPLETE,
        TaskStatus.FAILED: AttemptOutcome.FAILED,
        TaskStatus.CANCELLED: AttemptOutcome.CANCELLED,
    }.get(contract_status)
    if contract_status is TaskStatus.RUNNING:
        attempts = previous_attempts + 1
        history.append(
            ExecutionAttemptV1(
                index=attempts,
                executor=executor,
                model=actual_model or (route_decision.model if route_decision else None),
                outcome=AttemptOutcome.RUNNING,
                reason_codes=attempt_reasons,
                command_summaries=commands,
                modified_files=attempt_files,
                artifact_refs=attempt_artifacts,
                started_at=updated_at,
            )
        )
    elif terminal_outcome is not None:
        if history and history[-1].outcome is AttemptOutcome.RUNNING:
            current = history[-1]
            history[-1] = current.model_copy(
                update={
                    "outcome": terminal_outcome,
                    "finished_at": updated_at,
                    "error_summary": (
                        bounded_error or "Executor failed; inspect bounded evidence."
                        if terminal_outcome is AttemptOutcome.FAILED
                        else None
                    ),
                    "reason_codes": list(dict.fromkeys([*current.reason_codes, *attempt_reasons])),
                    "command_summaries": list(dict.fromkeys([*current.command_summaries, *commands])),
                    "modified_files": list(dict.fromkeys([*current.modified_files, *attempt_files])),
                    "artifact_refs": list(dict.fromkeys([*current.artifact_refs, *attempt_artifacts])),
                }
            )
            attempts = previous_attempts
        else:
            attempts = previous_attempts + 1
            history.append(
                ExecutionAttemptV1(
                    index=attempts,
                    executor=executor,
                    model=actual_model or (route_decision.model if route_decision else None),
                    outcome=terminal_outcome,
                    reason_codes=attempt_reasons,
                    command_summaries=commands,
                    error_summary=(
                        bounded_error or "Executor failed; inspect bounded evidence."
                        if terminal_outcome is AttemptOutcome.FAILED
                        else None
                    ),
                    modified_files=attempt_files,
                    artifact_refs=attempt_artifacts,
                    started_at=updated_at,
                    finished_at=updated_at,
                )
            )
    else:
        attempts = previous_attempts

    unresolved_errors = list(existing_state.unresolved_errors) if existing_state else []
    if contract_status is TaskStatus.FAILED:
        failure = bounded_error or "Executor failed; inspect bounded task evidence."
        if failure not in unresolved_errors:
            unresolved_errors.append(failure)
    elif contract_status is TaskStatus.COMPLETE:
        unresolved_errors = []
    next_actions = {
        TaskStatus.PENDING: "Resolve routing and execution prerequisites.",
        TaskStatus.READY: "Use the prepared handoff or resume when the executor is available.",
        TaskStatus.RUNNING: "Wait for the current bounded execution attempt.",
        TaskStatus.BLOCKED: "Supply the missing scope or approval.",
        TaskStatus.COMPLETE: None,
        TaskStatus.FAILED: "Inspect evidence and select a new bounded strategy.",
        TaskStatus.CANCELLED: None,
    }
    return _sanitize_task_state_for_persistence(TaskStateV1(
        task_id=task_id,
        request_id=existing_state.request_id if existing_state else task_id,
        status=contract_status,
        attempts=attempts,
        executor=executor,
        project=project_path,
        worktree=(
            project_path
            if route in {"local_code", "codex", "codex_bundle", "docs"}
            else (existing_state.worktree if existing_state else None)
        ),
        artifacts=existing_state.artifacts if existing_state else [],
        artifact_refs=list(
            dict.fromkeys([*(existing_state.artifact_refs if existing_state else []), *attempt_artifacts])
        ),
        modified_files=list(dict.fromkeys([*(existing_state.modified_files if existing_state else []), *attempt_files])),
        unresolved_errors=unresolved_errors,
        next_action=next_actions[contract_status],
        created_at=created_at,
        updated_at=updated_at,
        route=RouteName(route),
        route_decision=route_decision or (existing_state.route_decision if existing_state else None),
        plan=plan or (existing_state.plan if existing_state else None),
        model=actual_model or (route_decision.model if route_decision else None) or (existing_state.model if existing_state else None),
        profile=actual_profile or (route_decision.profile if route_decision else None) or (existing_state.profile if existing_state else None),
        fallback_used=fallback_used or (existing_state.fallback_used if existing_state else False),
        attempt_history=history,
    ))


def save_task(
    task_id: str,
    route: str,
    status: str,
    prompt: str,
    project_path: str | None = None,
    result: str | None = None,
    metadata: dict | None = None,
    *,
    route_decision: RouteDecisionV1 | None = None,
    plan: PlanV1 | None = None,
    actual_executor: ExecutorName | None = None,
    actual_model: str | None = None,
    actual_profile: str | None = None,
    fallback_used: bool = False,
    error_summary: str | None = None,
    reason_codes: list[str] | None = None,
    command_summaries: list[str] | None = None,
    modified_files: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> None:
    now = time.time()
    with db() as connection:
        # Serialize the read/modify/write lifecycle so concurrent transitions
        # of one task cannot silently lose attempt history.
        connection.execute("BEGIN IMMEDIATE")
        state = _build_task_state(
            connection,
            task_id=task_id,
            route=route,
            status=status,
            project_path=project_path,
            now=now,
            route_decision=route_decision,
            plan=plan,
            actual_executor=actual_executor,
            actual_model=actual_model,
            actual_profile=actual_profile,
            fallback_used=fallback_used,
            error_summary=error_summary,
            reason_codes=reason_codes,
            command_summaries=command_summaries,
            modified_files=modified_files,
            artifact_refs=artifact_refs,
        )
        existing_metadata_row = connection.execute(
            "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        merged_metadata: dict = {}
        if existing_metadata_row is not None:
            try:
                decoded = json.loads(existing_metadata_row["metadata"] or "{}")
                if isinstance(decoded, dict):
                    merged_metadata.update(decoded)
            except (TypeError, ValueError):
                pass
        merged_metadata.update(metadata or {})
        persisted_metadata = sanitize_task_metadata(merged_metadata)
        persisted_prompt = sanitize_task_text(prompt, "task-prompt")
        persisted_result = sanitize_task_text(result, "task-result") if result is not None else None
        connection.execute(
            """
            INSERT INTO tasks
            (id, created_at, updated_at, route, status, project_path, prompt, result,
             metadata, schema_version, state_json, privacy_version, legacy_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                route = excluded.route,
                status = excluded.status,
                project_path = excluded.project_path,
                prompt = excluded.prompt,
                result = excluded.result,
                metadata = excluded.metadata,
                schema_version = excluded.schema_version,
                state_json = excluded.state_json,
                privacy_version = excluded.privacy_version,
                legacy_payload = 0
            """,
            (
                task_id,
                now,
                now,
                route,
                status,
                project_path,
                persisted_prompt,
                persisted_result,
                json.dumps(persisted_metadata, ensure_ascii=False),
                CONTRACT_VERSION,
                state.model_dump_json(),
                "stage003-v1",
            ),
        )
        connection.commit()


def load_task_state(task_id: str) -> TaskStateV1 | None:
    """Read a v1 state snapshot; legacy rows remain explicit and return None."""

    with db() as connection:
        row = connection.execute(
            "SELECT state_json,legacy_payload FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if row is None or row["legacy_payload"] or not row["state_json"]:
        return None
    return TaskStateV1.model_validate_json(row["state_json"])


def task_store_ready() -> tuple[bool, str]:
    try:
        with db() as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if not _REQUIRED_TASK_COLUMNS.issubset(columns):
                return False, "SQLite task journal schema is incomplete."
            connection.execute(
                "SELECT id,updated_at,status,state_json FROM tasks LIMIT 1"
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        return True, "SQLite task journal is writable and queryable."
    except Exception as exc:
        return False, f"SQLite task journal unavailable: {type(exc).__name__}"
