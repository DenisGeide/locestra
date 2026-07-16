from __future__ import annotations

import json
import subprocess
from pathlib import Path

from services.contracts import (
    DecisionStatus,
    ExecutorName,
    MemoryContextItemV1,
    NormalizedRequestV1,
    RouteDecisionV1,
    RouteName,
)
from services.memory.store import MemoryStore
from services.orchestration.planner import PlanningResult

_CONTENT_ROUTES = {RouteName.LOCAL_CODE}
_REFERENCE_ONLY_ROUTES = {RouteName.CODEX, RouteName.CODEX_BUNDLE}
_RETRIEVAL_ROUTES = _CONTENT_ROUTES | _REFERENCE_ONLY_ROUTES


def current_commit_sha(project_path: str | None) -> str | None:
    """Return the current Git revision without making repository changes."""

    if not project_path:
        return None
    project = Path(project_path)
    if not project.is_dir():
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=project,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.strip()
    if completed.returncode != 0 or not revision:
        return None
    return revision[:64]


def _route_name(route: RouteDecisionV1 | RouteName | str) -> RouteName:
    if isinstance(route, RouteDecisionV1):
        return route.route
    return route if isinstance(route, RouteName) else RouteName(route)


def _render_value(value: object) -> str:
    if isinstance(value, str):
        return value.replace("\x00", "")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def attach_memory_to_planning(
    request: NormalizedRequestV1,
    planning: PlanningResult,
    route: RouteDecisionV1 | RouteName | str,
) -> PlanningResult:
    """Best-effort scoped retrieval which never changes routing or permissions.

    Fast/auxiliary/docs requests perform no memory I/O.  Local code may receive
    bounded confirmed content; Codex receives opaque record references only so
    retrieval cannot silently broaden an external data boundary.
    """

    plan = planning.plan
    if plan is None:
        return planning
    if isinstance(route, RouteDecisionV1) and (
        route.decision_status is not DecisionStatus.READY
        or route.executor is ExecutorName.DEGRADED_RESPONSE
    ):
        return planning
    try:
        route_name = _route_name(route)
    except ValueError:
        return planning
    if route_name not in _RETRIEVAL_ROUTES:
        return planning
    project_path = route.project if isinstance(route, RouteDecisionV1) else request.project_hint

    max_chars = max(1, min(1_500, int(plan.context_budget.max_input_tokens * 0.20)))
    try:
        result = MemoryStore().retrieve_safe(
            owner_id="local-user",
            project_path=project_path,
            task_id=request.request_id,
            query=plan.goal,
            max_records=6,
            max_chars=max_chars,
            current_commit_sha=current_commit_sha(project_path),
        )
        if result.degraded:
            return planning

        refs: list[str] = []
        context: list[MemoryContextItemV1] = []
        remaining = max_chars
        for item in result.items[:6]:
            if item.record_id in refs:
                continue
            if route_name in _REFERENCE_ONLY_ROUTES:
                refs.append(item.record_id)
                context.append(
                    MemoryContextItemV1(
                        record_id=item.record_id,
                        record_type=item.record_type.value,
                        subject=item.subject,
                        content=None,
                        source_refs=item.source_refs,
                        score=item.score,
                        why=item.why,
                        disclosure="reference_only",
                    )
                )
                continue
            content = _render_value(item.value)
            if not content or len(content) > remaining:
                continue
            refs.append(item.record_id)
            if route_name in _CONTENT_ROUTES:
                context.append(
                    MemoryContextItemV1(
                        record_id=item.record_id,
                        record_type=item.record_type.value,
                        subject=item.subject,
                        content=content,
                        source_refs=item.source_refs,
                        score=item.score,
                        why=item.why,
                    )
                )
                remaining -= len(content)
        if not refs:
            return planning
        enriched = type(plan).model_validate(
            {
                **plan.model_dump(mode="python"),
                "memory_context": context,
                "memory_record_refs": refs,
            }
        )
        return PlanningResult(
            signals=planning.signals,
            plan=enriched,
            planning_mode=planning.planning_mode,
        )
    except Exception:
        # Memory is optional.  Storage/schema/policy failures must not block the
        # established chat or coding path and must never expose stored content.
        return planning
