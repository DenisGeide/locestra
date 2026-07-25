"""Version 1.0 internal data contracts.

These models deliberately describe bounded metadata, not request bodies,
binary attachments, or unbounded tool output.  Their generated Pydantic JSON
Schema is the sole machine-readable schema source for this contract version.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "1.0"
SchemaVersion = Literal["1.0"]

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ShortName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_048),
]
LongText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=262_144),
]
Reference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_096),
]
ReasonCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]
ResourceLock = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[a-z][a-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Fa-f0-9]{64}$"),
]


class StrictContractModel(BaseModel):
    """Base configuration for data after an entry boundary."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class RequestSource(StrEnum):
    OPEN_WEBUI = "open_webui"
    API = "api"
    TELEGRAM = "telegram"
    N8N = "n8n"
    INTERNAL = "internal"


class RouteName(StrEnum):
    AUXILIARY = "auxiliary"
    FAST_CHAT = "fast_chat"
    STRONG_CHAT = "strong_chat"
    LOCAL_CODE = "local_code"
    CODEX = "codex"
    CODEX_BUNDLE = "codex_bundle"
    DOCS = "docs"
    BROWSER = "browser"
    IMAGE = "image"
    VOICE = "voice"
    VISION = "vision"


class ExecutorName(StrEnum):
    FAST_OLLAMA = "fast_ollama"
    STRONG_OLLAMA = "strong_ollama"
    QWEN_CODE = "qwen_code"
    CODEX_CLI = "codex_cli"
    CODEX_BUNDLE = "codex_bundle"
    PLAYWRIGHT = "playwright"
    COMFYUI = "comfyui"
    WHISPER = "whisper"
    DEGRADED_RESPONSE = "degraded_response"


_ALLOWED_EXECUTORS_BY_ROUTE: dict[RouteName, set[ExecutorName]] = {
    RouteName.AUXILIARY: {ExecutorName.FAST_OLLAMA, ExecutorName.DEGRADED_RESPONSE},
    RouteName.FAST_CHAT: {ExecutorName.FAST_OLLAMA, ExecutorName.DEGRADED_RESPONSE},
    RouteName.STRONG_CHAT: {ExecutorName.STRONG_OLLAMA, ExecutorName.DEGRADED_RESPONSE},
    RouteName.LOCAL_CODE: {
        ExecutorName.QWEN_CODE,
        ExecutorName.CODEX_CLI,
        ExecutorName.DEGRADED_RESPONSE,
    },
    RouteName.CODEX: {ExecutorName.CODEX_CLI, ExecutorName.CODEX_BUNDLE, ExecutorName.DEGRADED_RESPONSE},
    RouteName.CODEX_BUNDLE: {ExecutorName.CODEX_BUNDLE},
    RouteName.DOCS: {ExecutorName.QWEN_CODE, ExecutorName.DEGRADED_RESPONSE},
    RouteName.BROWSER: {ExecutorName.PLAYWRIGHT, ExecutorName.DEGRADED_RESPONSE},
    RouteName.IMAGE: {ExecutorName.COMFYUI, ExecutorName.DEGRADED_RESPONSE},
    RouteName.VOICE: {ExecutorName.WHISPER, ExecutorName.DEGRADED_RESPONSE},
    RouteName.VISION: {ExecutorName.DEGRADED_RESPONSE},
}


def _require_compatible_route_executor(route: RouteName, executor: ExecutorName) -> None:
    if executor not in _ALLOWED_EXECUTORS_BY_ROUTE[route]:
        raise ValueError(f"executor {executor} is incompatible with route {route}")


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionKind(StrEnum):
    CHAT = "chat"
    ANALYSIS = "analysis"
    REPOSITORY_READ = "repository_read"
    REPOSITORY_MUTATION = "repository_mutation"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    BROWSER = "browser"
    VOICE = "voice"
    VISION = "vision"
    IMAGE = "image"
    AUXILIARY = "auxiliary"


class ComplexityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionMode(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    WRITE = "write"


class RouteOverride(StrEnum):
    LOCAL = "local"
    CODEX = "codex"
    VOICE = "voice"
    VISION = "vision"
    IMAGE = "image"
    BROWSER = "browser"


class OverrideDisposition(StrEnum):
    NONE = "none"
    APPLIED = "applied"
    REJECTED = "rejected"


class DecisionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class PermissionDisposition(StrEnum):
    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


class ProjectResolutionSource(StrEnum):
    EXPLICIT = "explicit"
    DEFAULT = "default"
    NONE = "none"


class ProjectResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    INVALID = "invalid"
    MISSING = "missing"


class AttemptOutcome(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttachmentKind(StrEnum):
    FILE = "file"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    OTHER = "other"


class RetentionPolicy(StrEnum):
    SESSION = "session"
    TASK = "task"
    TTL = "ttl"
    MANUAL = "manual"
    PERMANENT = "permanent"


class HealthProbeKind(StrEnum):
    HTTP = "http"
    COMMAND = "command"
    IN_PROCESS = "in_process"
    NONE = "none"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    ON_DEMAND = "on_demand"


class Locality(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"
    HYBRID = "hybrid"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def _reference_without_inline_data(value: str, field_name: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain a NUL byte")
    if value.lstrip().casefold().startswith("data:"):
        raise ValueError(f"{field_name} must reference data, not embed a data URL")
    return value


def _unique(values: list[str], field_name: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def _validate_json_schema_payload(value: dict[str, Any], field_name: str) -> dict[str, Any]:
    def inspect(item: Any) -> None:
        if isinstance(item, (bytes, bytearray, memoryview)):
            raise ValueError(f"{field_name} must not contain inline binary data")
        if isinstance(item, str) and item.lstrip().casefold().startswith("data:"):
            raise ValueError(f"{field_name} must not contain an inline data URL")
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} keys must be strings")
                inspect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                inspect(nested)

    inspect(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 131_072:
        raise ValueError(f"{field_name} exceeds the 131072-byte contract limit")
    return value


class AttachmentRefV1(StrictContractModel):
    attachment_id: Identifier
    kind: AttachmentKind
    reference: Reference
    media_type: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] | None = None
    size_bytes: int | None = Field(default=None, ge=0, le=4_294_967_296)
    sha256: Sha256 | None = None
    provenance: list[ShortText] = Field(default_factory=list, max_length=32)

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        return _reference_without_inline_data(value, "reference")

    @field_validator("sha256")
    @classmethod
    def normalize_hash(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: list[str]) -> list[str]:
        checked = [_reference_without_inline_data(item, "provenance") for item in value]
        return _unique(checked, "provenance")


class ProjectResolutionV1(StrictContractModel):
    source: ProjectResolutionSource
    status: ProjectResolutionStatus

    @model_validator(mode="after")
    def validate_resolution(self) -> "ProjectResolutionV1":
        if self.source is ProjectResolutionSource.NONE and self.status is ProjectResolutionStatus.RESOLVED:
            raise ValueError("a resolved project requires an explicit or default source")
        if self.source is ProjectResolutionSource.EXPLICIT and self.status is ProjectResolutionStatus.MISSING:
            raise ValueError("an explicit project is resolved or invalid, never missing")
        return self


class NormalizedRequestV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    request_id: Identifier
    user_message: LongText
    attachments: list[AttachmentRefV1] = Field(max_length=32)
    source: RequestSource
    project_hint: Reference | None
    explicit_route: RouteName | None
    created_at: datetime
    correlation_id: Identifier
    routing_override: RouteOverride | None = None
    override_conflict: bool = False
    project_resolution: ProjectResolutionV1 | None = None

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @field_validator("project_hint")
    @classmethod
    def validate_project_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reference_without_inline_data(value, "project_hint")


class ContextBudgetV1(StrictContractModel):
    max_input_tokens: int = Field(ge=1, le=2_000_000)
    reserved_output_tokens: int = Field(ge=0, le=1_000_000)
    max_attachment_bytes: int = Field(ge=0, le=4_294_967_296)
    max_tool_output_chars: int = Field(default=20_000, ge=0, le=10_000_000)
    compression_policy: ShortName = "provenance_preserving"


class MemoryContextItemV1(StrictContractModel):
    """Bounded confirmed memory evidence metadata attached to a plan.

    ``reference_only`` items support local explainability while withholding the
    stored value from an external executor.  Content remains untrusted data.
    """

    record_id: Identifier
    record_type: ShortName
    subject: ShortText
    content: LongText | None
    source_refs: list[Reference] = Field(default_factory=list, max_length=32)
    score: float = Field(ge=0.0, le=1.0)
    why: ShortText
    status: Literal["confirmed"] = "confirmed"
    disclosure: Literal["content", "reference_only"] = "content"

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, value: list[str]) -> list[str]:
        checked = [_reference_without_inline_data(item, "source_refs") for item in value]
        return _unique(checked, "source_refs")

    @model_validator(mode="after")
    def validate_disclosure(self) -> "MemoryContextItemV1":
        if self.disclosure == "content" and self.content is None:
            raise ValueError("content disclosure requires a bounded value")
        if self.disclosure == "reference_only" and self.content is not None:
            raise ValueError("reference-only memory must not include content")
        return self


class PlanV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    goal: LongText
    subtasks: list[ShortText] = Field(min_length=1, max_length=128)
    tools: list[ShortName] = Field(max_length=64)
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=128)
    risk: RiskLevel
    approvals: list[ShortText] = Field(max_length=64)
    verification_plan: list[ShortText] = Field(min_length=1, max_length=128)
    context_budget: ContextBudgetV1
    request_id: Identifier | None = None
    action: ActionKind | None = None
    complexity: ComplexityLevel | None = None
    constraints: list[ShortText] = Field(default_factory=list, max_length=64)
    memory_context: list[MemoryContextItemV1] = Field(default_factory=list, max_length=6)
    memory_record_refs: list[Identifier] = Field(default_factory=list, max_length=32)

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        return _unique(value, "tools")

    @field_validator("constraints")
    @classmethod
    def unique_constraints(cls, value: list[str]) -> list[str]:
        return _unique(value, "constraints")

    @field_validator("memory_record_refs")
    @classmethod
    def unique_memory_record_refs(cls, value: list[str]) -> list[str]:
        return _unique(value, "memory_record_refs")

    @model_validator(mode="after")
    def validate_memory_references(self) -> "PlanV1":
        referenced = set(self.memory_record_refs)
        if any(item.record_id not in referenced for item in self.memory_context):
            raise ValueError("memory_context records must be listed in memory_record_refs")
        return self


class RouteFallbackV1(StrictContractModel):
    route: RouteName
    executor: ExecutorName
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=32)

    @field_validator("reason_codes")
    @classmethod
    def unique_reason_codes(cls, value: list[str]) -> list[str]:
        return _unique(value, "reason_codes")

    @model_validator(mode="after")
    def validate_executor_for_route(self) -> "RouteFallbackV1":
        _require_compatible_route_executor(self.route, self.executor)
        return self


class RouteDecisionV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    request_id: Identifier
    route: RouteName
    executor: ExecutorName
    model: ShortName | None
    profile: ShortName | None
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=32)
    risk: RiskLevel
    fallback: RouteFallbackV1 | None
    project: Reference | None
    required_locks: list[ResourceLock] = Field(max_length=32)
    policy_version: ShortName | None = None
    action: ActionKind | None = None
    complexity: ComplexityLevel | None = None
    execution_mode: ExecutionMode = ExecutionMode.NONE
    requested_route: RouteOverride | None = None
    override_disposition: OverrideDisposition = OverrideDisposition.NONE
    decision_status: DecisionStatus = DecisionStatus.READY
    permission_disposition: PermissionDisposition = PermissionDisposition.ALLOWED
    capability: ShortName | None = None
    capability_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE
    capability_checked_at: datetime | None = None
    blocking_reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=32)
    max_attempts: int = Field(default=1, ge=0, le=10)

    @field_validator("reason_codes", "required_locks")
    @classmethod
    def unique_contract_lists(cls, value: list[str], info) -> list[str]:
        return _unique(value, info.field_name)

    @field_validator("blocking_reason_codes")
    @classmethod
    def unique_blocking_reasons(cls, value: list[str]) -> list[str]:
        return _unique(value, "blocking_reason_codes")

    @field_validator("capability_checked_at")
    @classmethod
    def validate_capability_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, "capability_checked_at")

    @field_validator("project")
    @classmethod
    def validate_project(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reference_without_inline_data(value, "project")

    @model_validator(mode="after")
    def validate_executor_for_route(self) -> "RouteDecisionV1":
        _require_compatible_route_executor(self.route, self.executor)
        return self


class ToolHealthSpecV1(StrictContractModel):
    kind: HealthProbeKind
    target: Reference | None
    timeout_seconds: float = Field(gt=0, le=300)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reference_without_inline_data(value, "health target")


class RetryPolicyV1(StrictContractModel):
    max_attempts: int = Field(ge=1, le=10)
    backoff_seconds: float = Field(ge=0, le=3_600)
    retryable_errors: list[ReasonCode] = Field(max_length=32)

    @field_validator("retryable_errors")
    @classmethod
    def unique_retryable_errors(cls, value: list[str]) -> list[str]:
        return _unique(value, "retryable_errors")


class ToolSpecV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    name: ShortName
    version: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    input_schema: dict[str, Any] = Field(max_length=256)
    output_schema: dict[str, Any] = Field(max_length=256)
    health: ToolHealthSpecV1
    permissions: list[ShortName] = Field(max_length=64)
    risk: RiskLevel
    timeout_seconds: float = Field(gt=0, le=86_400)
    retry: RetryPolicyV1
    availability: AvailabilityStatus
    locality: Locality

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_json_schemas(cls, value: dict[str, Any], info) -> dict[str, Any]:
        return _validate_json_schema_payload(value, info.field_name)

    @field_validator("permissions")
    @classmethod
    def unique_permissions(cls, value: list[str]) -> list[str]:
        return _unique(value, "permissions")


class RetentionPolicyV1(StrictContractModel):
    policy: RetentionPolicy
    expires_at: datetime | None

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, "expires_at")

    @model_validator(mode="after")
    def validate_ttl_expiry(self) -> "RetentionPolicyV1":
        if self.policy is RetentionPolicy.TTL and self.expires_at is None:
            raise ValueError("ttl retention requires expires_at")
        if self.policy is not RetentionPolicy.TTL and self.expires_at is not None:
            raise ValueError("expires_at is only valid for ttl retention")
        return self


class ArtifactMetadataV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    artifact_id: Identifier
    type: ShortName
    path: Reference
    hash: Sha256
    producer: ShortName
    source_request: Identifier
    created_at: datetime
    provenance: list[ShortText] = Field(min_length=1, max_length=32)
    retention: RetentionPolicyV1

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _reference_without_inline_data(value, "path")

    @field_validator("hash")
    @classmethod
    def normalize_hash(cls, value: str) -> str:
        return value.lower()

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: list[str]) -> list[str]:
        checked = [_reference_without_inline_data(item, "provenance") for item in value]
        return _unique(checked, "provenance")


class ExecutionAttemptV1(StrictContractModel):
    index: int = Field(ge=1, le=100)
    executor: ExecutorName
    model: ShortName | None = None
    outcome: AttemptOutcome
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=32)
    command_summaries: list[ShortText] = Field(default_factory=list, max_length=64)
    error_summary: ShortText | None = None
    modified_files: list[Reference] = Field(default_factory=list, max_length=10_000)
    artifact_refs: list[Reference] = Field(default_factory=list, max_length=128)
    started_at: datetime
    finished_at: datetime | None = None

    @field_validator("reason_codes", "command_summaries", "modified_files", "artifact_refs")
    @classmethod
    def validate_unique_attempt_lists(cls, value: list[str], info) -> list[str]:
        checked = (
            [_reference_without_inline_data(item, info.field_name) for item in value]
            if info.field_name in {"modified_files", "artifact_refs"}
            else value
        )
        return _unique(checked, info.field_name)

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_attempt_timestamps(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_attempt_completion(self) -> "ExecutionAttemptV1":
        if self.outcome is AttemptOutcome.RUNNING and self.finished_at is not None:
            raise ValueError("a running attempt cannot have finished_at")
        if self.outcome is not AttemptOutcome.RUNNING and self.finished_at is None:
            raise ValueError("a terminal attempt requires finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.outcome is AttemptOutcome.FAILED and not self.error_summary:
            raise ValueError("a failed attempt requires error_summary")
        return self


class TaskStateV1(StrictContractModel):
    schema_version: SchemaVersion = CONTRACT_VERSION
    task_id: Identifier
    request_id: Identifier
    status: TaskStatus
    attempts: int = Field(ge=0, le=100)
    executor: ExecutorName | None
    project: Reference | None
    worktree: Reference | None
    artifacts: list[ArtifactMetadataV1] = Field(max_length=128)
    artifact_refs: list[Reference] = Field(default_factory=list, max_length=128)
    modified_files: list[Reference] = Field(max_length=10_000)
    unresolved_errors: list[ShortText] = Field(max_length=128)
    next_action: ShortText | None
    created_at: datetime
    updated_at: datetime
    route: RouteName | None = None
    route_decision: RouteDecisionV1 | None = None
    plan: PlanV1 | None = None
    model: ShortName | None = None
    profile: ShortName | None = None
    fallback_used: bool = False
    attempt_history: list[ExecutionAttemptV1] = Field(default_factory=list, max_length=100)

    @field_validator("project", "worktree")
    @classmethod
    def validate_optional_references(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _reference_without_inline_data(value, info.field_name)

    @field_validator("artifact_refs", "modified_files")
    @classmethod
    def validate_task_references(cls, value: list[str], info) -> list[str]:
        checked = [_reference_without_inline_data(item, info.field_name) for item in value]
        return _unique(checked, info.field_name)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_state_invariants(self) -> "TaskStateV1":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status in {TaskStatus.RUNNING, TaskStatus.COMPLETE, TaskStatus.FAILED} and self.attempts < 1:
            raise ValueError(f"{self.status} state requires at least one attempt")
        if self.status is TaskStatus.FAILED and not self.unresolved_errors:
            raise ValueError("failed state requires unresolved_errors")
        if self.status is TaskStatus.COMPLETE and self.unresolved_errors:
            raise ValueError("complete state must not contain unresolved_errors")
        if self.attempt_history and self.attempts < len(self.attempt_history):
            raise ValueError("attempts cannot be smaller than the structured attempt history")
        if self.attempt_history and self.attempt_history[-1].index != self.attempts:
            raise ValueError("the latest structured attempt index must equal attempts")
        return self
