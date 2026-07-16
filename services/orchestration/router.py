from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from services.contracts import (
    ActionKind,
    AvailabilityStatus,
    ComplexityLevel,
    DecisionStatus,
    ExecutorName,
    ExecutionMode,
    NormalizedRequestV1,
    OverrideDisposition,
    PermissionDisposition,
    ProjectResolutionStatus,
    RouteDecisionV1,
    RouteFallbackV1,
    RouteName,
    RouteOverride,
    RiskLevel,
)
from services.orchestration.config import RoutingPolicy, get_routing_policy
from services.orchestration.planner import PlanningResult, plan_exceeds_agent_input_budget


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    statuses: dict[str, AvailabilityStatus]
    checked_at: datetime
    stale: bool = False

    def status(self, name: str) -> AvailabilityStatus:
        if self.stale:
            return AvailabilityStatus.UNAVAILABLE
        return self.statuses.get(name, AvailabilityStatus.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    codex_cloud_approved: bool = False
    browser_public_network_allowed: bool = True


@dataclass(frozen=True, slots=True)
class FailureHistory:
    local_code_failures: int = 0


def assumed_capabilities(*, vision: AvailabilityStatus = AvailabilityStatus.UNAVAILABLE) -> CapabilitySnapshot:
    now = datetime.now(timezone.utc)
    return CapabilitySnapshot(
        statuses={
            "fast_model": AvailabilityStatus.AVAILABLE,
            "strong_model": AvailabilityStatus.AVAILABLE,
            "qwen_code": AvailabilityStatus.AVAILABLE,
            "codex": AvailabilityStatus.AVAILABLE,
            "context7": AvailabilityStatus.AVAILABLE,
            "browser": AvailabilityStatus.AVAILABLE,
            "voice": AvailabilityStatus.AVAILABLE,
            "vision": vision,
            "image": AvailabilityStatus.ON_DEMAND,
        },
        checked_at=now,
    )


def _worktree_lock_reference(project: str) -> str:
    canonical = os.path.normcase(os.path.realpath(project)).encode("utf-8", errors="surrogatepass")
    return f"worktree:{hashlib.sha256(canonical).hexdigest()[:16]}"


def public_url_policy_reason(url: str | None) -> str | None:
    if not url:
        return "browser.url_missing"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "browser.url_invalid"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return "browser.url_invalid"
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "permission.network_target_denied"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "permission.network_target_denied"
    return None


def _natural_route(planning: PlanningResult) -> RouteName:
    signals = planning.signals
    if signals.auxiliary:
        return RouteName.AUXILIARY
    if signals.repository_action:
        if signals.review or signals.complexity in {ComplexityLevel.HIGH, ComplexityLevel.CRITICAL}:
            return RouteName.CODEX
        return RouteName.LOCAL_CODE
    return {
        ActionKind.VOICE: RouteName.VOICE,
        ActionKind.VISION: RouteName.VISION,
        ActionKind.IMAGE: RouteName.IMAGE,
        ActionKind.DOCUMENTATION: RouteName.DOCS,
        ActionKind.BROWSER: RouteName.BROWSER,
        ActionKind.ANALYSIS: RouteName.STRONG_CHAT,
    }.get(signals.action, RouteName.FAST_CHAT)


def _apply_override(
    natural: RouteName,
    request: NormalizedRequestV1,
    planning: PlanningResult,
) -> tuple[RouteName, OverrideDisposition, list[str]]:
    override = request.routing_override
    if override is None:
        return natural, OverrideDisposition.NONE, []
    if request.override_conflict:
        return natural, OverrideDisposition.REJECTED, ["override.conflict"]
    signals = planning.signals
    if override is RouteOverride.LOCAL:
        if signals.repository_action:
            return RouteName.LOCAL_CODE, OverrideDisposition.APPLIED, ["override.local.applied"]
        if natural in {RouteName.IMAGE, RouteName.VOICE, RouteName.VISION}:
            return natural, OverrideDisposition.APPLIED, ["override.local.applied"]
        target = RouteName.STRONG_CHAT if signals.complexity is not ComplexityLevel.LOW else RouteName.FAST_CHAT
        return target, OverrideDisposition.APPLIED, ["override.local.applied"]
    if override is RouteOverride.CODEX:
        if not (signals.repository_action or signals.architecture or signals.programming):
            return natural, OverrideDisposition.REJECTED, ["override.codex.rejected_not_applicable"]
        return RouteName.CODEX, OverrideDisposition.APPLIED, ["override.codex.applied"]
    target = {
        RouteOverride.VOICE: RouteName.VOICE,
        RouteOverride.VISION: RouteName.VISION,
        RouteOverride.IMAGE: RouteName.IMAGE,
        RouteOverride.BROWSER: RouteName.BROWSER,
    }[override]
    return target, OverrideDisposition.APPLIED, [f"override.{override.value}.applied"]


_CAPABILITY_BY_ROUTE = {
    RouteName.AUXILIARY: "fast_model",
    RouteName.FAST_CHAT: "fast_model",
    RouteName.STRONG_CHAT: "strong_model",
    RouteName.LOCAL_CODE: "qwen_code",
    RouteName.CODEX: "codex",
    RouteName.CODEX_BUNDLE: "codex",
    RouteName.DOCS: "context7",
    RouteName.BROWSER: "browser",
    RouteName.IMAGE: "image",
    RouteName.VOICE: "voice",
    RouteName.VISION: "vision",
}

_EXECUTOR_BY_ROUTE = {
    RouteName.AUXILIARY: ExecutorName.FAST_OLLAMA,
    RouteName.FAST_CHAT: ExecutorName.FAST_OLLAMA,
    RouteName.STRONG_CHAT: ExecutorName.STRONG_OLLAMA,
    RouteName.LOCAL_CODE: ExecutorName.QWEN_CODE,
    RouteName.CODEX: ExecutorName.CODEX_CLI,
    RouteName.CODEX_BUNDLE: ExecutorName.CODEX_BUNDLE,
    RouteName.DOCS: ExecutorName.QWEN_CODE,
    RouteName.BROWSER: ExecutorName.PLAYWRIGHT,
    RouteName.IMAGE: ExecutorName.COMFYUI,
    RouteName.VOICE: ExecutorName.WHISPER,
    RouteName.VISION: ExecutorName.DEGRADED_RESPONSE,
}


def route_request(
    request: NormalizedRequestV1,
    planning: PlanningResult,
    *,
    capabilities: CapabilitySnapshot | None = None,
    permissions: PermissionSnapshot | None = None,
    failures: FailureHistory | None = None,
    policy: RoutingPolicy | None = None,
    fast_model: str = "local-fast",
    strong_model: str = "local-strong",
    agent_model: str = "local-strong",
    codex_model: str = "codex",
) -> RouteDecisionV1:
    policy = policy or get_routing_policy()
    capabilities = capabilities or assumed_capabilities()
    permissions = permissions or PermissionSnapshot()
    failures = failures or FailureHistory()
    signals = planning.signals
    natural = _natural_route(planning)
    route, override_disposition, override_reasons = _apply_override(natural, request, planning)
    reasons = list(signals.reason_codes) + override_reasons
    blocking: list[str] = []
    decision_status = DecisionStatus.READY
    permission = PermissionDisposition.ALLOWED

    needs_project = route in {RouteName.LOCAL_CODE, RouteName.CODEX} and signals.repository_action
    project_is_resolved = bool(
        request.project_hint
        and (
            request.project_resolution is None
            or request.project_resolution.status is ProjectResolutionStatus.RESOLVED
        )
    )
    if needs_project and not project_is_resolved:
        blocking.append(
            "project.explicit_invalid"
            if request.project_resolution and request.project_resolution.status is ProjectResolutionStatus.INVALID
            else "project.missing"
        )
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.DENIED
    if signals.permission_conflict:
        blocking.append("permission.read_only_conflict")
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.DENIED
    if request.override_conflict:
        blocking.append("override.conflict")
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.DENIED
    if (
        request.routing_override is RouteOverride.LOCAL
        and signals.complexity is ComplexityLevel.CRITICAL
        and signals.repository_action
    ):
        blocking.append("permission.critical_action_denied")
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.DENIED
    elif (
        request.routing_override is RouteOverride.LOCAL
        and signals.complexity is ComplexityLevel.HIGH
        and signals.repository_action
        and signals.execution_mode is ExecutionMode.WRITE
    ):
        blocking.append("permission.high_risk_local_override_denied")
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.DENIED

    if route is RouteName.BROWSER:
        url_reason = public_url_policy_reason(signals.public_url)
        if url_reason:
            blocking.append(url_reason)
            decision_status = DecisionStatus.BLOCKED
            permission = PermissionDisposition.DENIED
        elif not permissions.browser_public_network_allowed:
            blocking.append("permission.network_access_denied")
            decision_status = DecisionStatus.BLOCKED
            permission = PermissionDisposition.DENIED
    if route is RouteName.VOICE and not signals.audio_attachment:
        blocking.append("voice.attachment_missing")
        decision_status = DecisionStatus.BLOCKED
    if route is RouteName.VISION and not signals.image_attachment:
        blocking.append("vision.attachment_missing")
        decision_status = DecisionStatus.BLOCKED

    if (
        route is RouteName.LOCAL_CODE
        and decision_status is DecisionStatus.READY
        and planning.plan is not None
        and plan_exceeds_agent_input_budget(planning.plan, request.user_message)
    ):
        blocking.append("context.agent_input_exceeds_budget")
        reasons.append("context.fail_closed")
        decision_status = DecisionStatus.BLOCKED

    capability = _CAPABILITY_BY_ROUTE[route]
    capability_status = capabilities.status(capability)
    if capability_status in {AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.DISABLED}:
        reasons.append(f"capability.{capability}.unavailable")
        if decision_status is DecisionStatus.READY:
            decision_status = DecisionStatus.DEGRADED
    elif capability_status is AvailabilityStatus.DEGRADED:
        reasons.append(f"capability.{capability}.degraded")
        if decision_status is DecisionStatus.READY:
            decision_status = DecisionStatus.DEGRADED

    executor = _EXECUTOR_BY_ROUTE[route]
    if decision_status is not DecisionStatus.READY:
        executor = ExecutorName.DEGRADED_RESPONSE

    if route is RouteName.CODEX and permission is not PermissionDisposition.DENIED:
        # A route preference or capability flag is not scoped permission to export a workspace.
        if not permissions.codex_cloud_approved:
            permission = PermissionDisposition.APPROVAL_REQUIRED
            if "permission.cloud_approval_required" not in blocking:
                blocking.append("permission.cloud_approval_required")
            reasons.extend(["fallback.codex_bundle", "permission.cloud_approval_required"])
            executor = ExecutorName.CODEX_BUNDLE
            decision_status = DecisionStatus.BLOCKED
        elif capability_status not in {AvailabilityStatus.AVAILABLE, AvailabilityStatus.ON_DEMAND}:
            executor = ExecutorName.CODEX_BUNDLE
            reasons.append("fallback.codex_bundle")
            decision_status = DecisionStatus.DEGRADED
    if (
        signals.repository_action
        and route is RouteName.LOCAL_CODE
        and decision_status is DecisionStatus.READY
        and failures.local_code_failures >= policy.thresholds.local_code_max_attempts
    ):
        route = RouteName.CODEX_BUNDLE
        executor = ExecutorName.CODEX_BUNDLE
        capability = "codex"
        capability_status = capabilities.status(capability)
        reasons.extend(["failure.local_attempt_limit", "fallback.codex_bundle"])
        blocking.extend(["failure.local_attempt_limit", "permission.cloud_approval_required"])
        decision_status = DecisionStatus.BLOCKED
        permission = PermissionDisposition.APPROVAL_REQUIRED

    if executor is ExecutorName.DEGRADED_RESPONSE and not blocking:
        blocking.append(f"capability.{capability}.unavailable")

    model: str | None = None
    profile: str | None = None
    if executor is ExecutorName.FAST_OLLAMA:
        model, profile = fast_model, "local-fast"
    elif executor is ExecutorName.STRONG_OLLAMA:
        model, profile = strong_model, "local-strong"
    elif executor is ExecutorName.QWEN_CODE:
        model, profile = agent_model, "local-strong"
    elif executor is ExecutorName.CODEX_CLI:
        model, profile = codex_model, "codex-high"

    project = (
        request.project_hint
        if signals.repository_action and route in {RouteName.LOCAL_CODE, RouteName.CODEX, RouteName.CODEX_BUNDLE}
        else None
    )
    locks: list[str] = []
    if project and executor in {ExecutorName.QWEN_CODE, ExecutorName.CODEX_CLI}:
        locks.append(_worktree_lock_reference(project))
    if executor is ExecutorName.FAST_OLLAMA:
        locks.append("fast_model")
    elif executor is ExecutorName.STRONG_OLLAMA:
        locks.append("gpu_heavy")
    elif executor is ExecutorName.QWEN_CODE:
        locks.extend(["qwen_agent", "gpu_heavy"])
    elif executor is ExecutorName.CODEX_CLI:
        locks.append("codex_agent")
    elif executor is ExecutorName.COMFYUI:
        locks.extend(["image", "gpu_heavy"])

    fallback: RouteFallbackV1 | None = None
    if route is RouteName.LOCAL_CODE and executor is ExecutorName.QWEN_CODE:
        fallback = RouteFallbackV1(
            route=RouteName.CODEX_BUNDLE,
            executor=ExecutorName.CODEX_BUNDLE,
            reason_codes=["failure.local_attempt_limit"],
        )
    elif route is RouteName.CODEX and executor is ExecutorName.CODEX_CLI:
        fallback = RouteFallbackV1(
            route=RouteName.CODEX_BUNDLE,
            executor=ExecutorName.CODEX_BUNDLE,
            reason_codes=["executor.codex_failure"],
        )

    effective_action = signals.action
    effective_mode = signals.execution_mode
    effective_risk = signals.risk
    if override_disposition is OverrideDisposition.APPLIED and not signals.repository_action:
        effective_action, effective_mode = {
            RouteName.VOICE: (ActionKind.VOICE, ExecutionMode.READ_ONLY),
            RouteName.VISION: (ActionKind.VISION, ExecutionMode.READ_ONLY),
            RouteName.IMAGE: (ActionKind.IMAGE, ExecutionMode.WRITE),
            RouteName.BROWSER: (ActionKind.BROWSER, ExecutionMode.READ_ONLY),
        }.get(route, (effective_action, effective_mode))
        if route is RouteName.BROWSER:
            effective_risk = RiskLevel.HIGH
    if planning.plan is None:
        reasons.append(f"planner.{planning.planning_mode}")
    return RouteDecisionV1(
        request_id=request.request_id,
        route=route,
        executor=executor,
        model=model,
        profile=profile,
        reason_codes=list(dict.fromkeys(reasons)),
        risk=effective_risk,
        fallback=fallback,
        project=project,
        required_locks=locks,
        policy_version=policy.policy_version,
        action=effective_action,
        complexity=signals.complexity,
        execution_mode=effective_mode,
        requested_route=request.routing_override,
        override_disposition=override_disposition,
        decision_status=decision_status,
        permission_disposition=permission,
        capability=capability,
        capability_status=capability_status,
        capability_checked_at=capabilities.checked_at,
        blocking_reason_codes=list(dict.fromkeys(blocking)),
        max_attempts=(policy.thresholds.local_code_max_attempts if route is RouteName.LOCAL_CODE else 1),
    )
