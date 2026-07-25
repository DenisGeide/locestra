from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CODING_SCHEMA_VERSION = "1.0"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _bounded_text(value: str, *, name: str, maximum: int) -> str:
    if "\x00" in value:
        raise ValueError(f"{name} contains a NUL byte")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return cleaned


def _reference(value: str, *, name: str, maximum: int = 4_096) -> str:
    checked = _bounded_text(value, name=name, maximum=maximum)
    if any(ord(char) < 32 and char not in "\t\r\n" for char in checked):
        raise ValueError(f"{name} contains a control character")
    return checked


def _aware(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _unique(values: list[str], *, name: str) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


class StrictCodingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class CodingMode(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"


class CodingRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class CodingTaskStatus(StrEnum):
    CREATED = "created"
    INSPECTED = "inspected"
    PLANNED = "planned"
    ISOLATED = "isolated"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    HANDOFF_READY = "handoff_ready"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class ExecutorKind(StrEnum):
    LOCAL_QWEN = "local_qwen"
    LOCAL_SEMANTIC_REVIEW = "local_semantic_review"
    CODEX_EXEC = "codex_exec"
    CODEX_REVIEW = "codex_review"
    DETERMINISTIC = "deterministic"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CommandStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    NOT_RUN = "not_run"


class ReviewSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class ArtifactKind(StrEnum):
    STATE = "state"
    DIFF = "diff"
    COMMAND_OUTPUT = "command_output"
    REVIEW = "review"
    HANDOFF = "handoff"
    SCREENSHOT = "screenshot"
    UI_EVIDENCE = "ui_evidence"
    CONTEXT = "context"


class CodingPermissionsV1(StrictCodingModel):
    modify_files: bool = False
    local_commit: bool = False
    cloud_execution: bool = False
    data_classification: DataClassification = DataClassification.INTERNAL
    push: Literal[False] = False
    deploy: Literal[False] = False

    @model_validator(mode="after")
    def validate_permissions(self) -> "CodingPermissionsV1":
        if self.local_commit and not self.modify_files:
            raise ValueError("local commit requires file modification permission")
        if self.cloud_execution and self.data_classification is not DataClassification.PUBLIC:
            raise ValueError("cloud execution requires explicit public fixture classification in this contract")
        return self


class VerificationCommandV1(StrictCodingModel):
    argv: list[str] = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(default=300, ge=1, le=7_200)
    required: bool = True

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        for item in value:
            _reference(item, name="argv", maximum=4_096)
            if "\r" in item or "\n" in item:
                raise ValueError("command arguments cannot contain newlines")
        return value


class CodingTaskRequestV1(StrictCodingModel):
    schema_version: Literal["1.0"] = CODING_SCHEMA_VERSION
    task_id: str
    request_id: str
    goal: str = Field(min_length=1, max_length=262_144)
    repository_path: str = Field(min_length=1, max_length=4_096)
    mode: CodingMode
    risk: CodingRisk
    constraints: list[str] = Field(default_factory=list, max_length=128)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=128)
    verification_plan: list[str] = Field(min_length=1, max_length=128)
    verification_commands: list[VerificationCommandV1] = Field(default_factory=list, max_length=32)
    permissions: CodingPermissionsV1
    route_reasons: list[str] = Field(default_factory=list, max_length=64)
    rule_scope_paths: list[str] = Field(default_factory=list, max_length=10_000)
    expected_diff_paths: list[str] = Field(default_factory=list, max_length=10_000)
    forbidden_diff_paths: list[str] = Field(default_factory=list, max_length=10_000)
    commit_message: str | None = Field(default=None, max_length=256)
    ui_url: str | None = Field(default=None, max_length=2_048)
    ui_selector: str | None = Field(default=None, max_length=512)
    ui_expected_text: str | None = Field(default=None, max_length=2_048)

    @field_validator("task_id", "request_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("task/request ID must use 1-64 safe ASCII characters")
        return value

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        return _reference(value, name="repository_path")

    @field_validator(
        "constraints", "acceptance_criteria", "verification_plan", "route_reasons",
        "rule_scope_paths", "expected_diff_paths", "forbidden_diff_paths"
    )
    @classmethod
    def validate_lists(cls, value: list[str], info) -> list[str]:
        checked = [_reference(item, name=info.field_name) for item in value]
        return _unique(checked, name=info.field_name)

    @field_validator("rule_scope_paths", "expected_diff_paths", "forbidden_diff_paths")
    @classmethod
    def validate_repository_relative_scopes(cls, value: list[str]) -> list[str]:
        for raw in value:
            normalized = raw.replace("\\", "/")
            candidate = PurePosixPath(normalized)
            if (
                normalized in {"", "."}
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or candidate.is_absolute()
                or ".." in candidate.parts
                or any(ord(char) < 32 for char in normalized)
            ):
                raise ValueError("coding scope path must stay inside the repository")
        return value

    @model_validator(mode="after")
    def validate_request_permissions(self) -> "CodingTaskRequestV1":
        if self.mode is CodingMode.READ_ONLY and self.permissions.modify_files:
            raise ValueError("read-only task cannot permit modifications")
        if self.mode is CodingMode.WRITE and not self.permissions.modify_files:
            raise ValueError("write task requires modification permission")
        if self.commit_message and not self.permissions.local_commit:
            raise ValueError("commit_message requires local_commit permission")
        if self.commit_message and ("\r" in self.commit_message or "\n" in self.commit_message):
            raise ValueError("commit_message must be a single line")
        for allowed in self.expected_diff_paths:
            allowed_key = allowed.casefold() if os.name == "nt" else allowed
            for denied in self.forbidden_diff_paths:
                denied_key = denied.casefold() if os.name == "nt" else denied
                if allowed_key == denied_key or allowed_key.startswith(
                    f"{denied_key}/"
                ):
                    raise ValueError(
                        "expected diff path conflicts with a forbidden diff path"
                    )
        ui_fields = (self.ui_url, self.ui_selector, self.ui_expected_text)
        if any(ui_fields) and not all(ui_fields):
            raise ValueError("UI verification requires URL, selector, and expected text")
        return self


class RuleReferenceV1(StrictCodingModel):
    path: str
    sha256: str
    scope: str

    @field_validator("path", "scope")
    @classmethod
    def validate_refs(cls, value: str, info) -> str:
        return _reference(value, name=info.field_name)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        lowered = value.casefold()
        if not _SHA256.fullmatch(lowered):
            raise ValueError("sha256 must be lowercase hex")
        return lowered


class WorktreeRecordV1(StrictCodingModel):
    schema_version: Literal["1.0"] = CODING_SCHEMA_VERSION
    task_id: str
    source_repository: str
    worktree_path: str
    branch: str | None
    git_dir: str | None = None
    git_common_dir: str | None = None
    git_marker_sha256: str | None = None
    base_commit: str
    owner_token_hash: str
    status: Literal["active", "complete", "orphaned", "cleanup_blocked", "removed"]
    owner_pid: int = Field(ge=1)
    created_at: datetime
    heartbeat_at: datetime
    completed_at: datetime | None = None

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid task id")
        return value

    @field_validator(
        "source_repository",
        "worktree_path",
        "branch",
        "git_dir",
        "git_common_dir",
    )
    @classmethod
    def validate_references(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _reference(value, name=info.field_name)

    @field_validator("base_commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        lowered = value.casefold()
        if not _GIT_SHA.fullmatch(lowered):
            raise ValueError("base_commit must be a full Git object id")
        return lowered

    @field_validator("owner_token_hash")
    @classmethod
    def validate_owner_hash(cls, value: str) -> str:
        lowered = value.casefold()
        if not _SHA256.fullmatch(lowered):
            raise ValueError("owner token hash must be sha256")
        return lowered

    @field_validator("git_marker_sha256")
    @classmethod
    def validate_git_marker_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.casefold()
        if not _SHA256.fullmatch(lowered):
            raise ValueError("Git marker hash must be sha256")
        return lowered

    @field_validator("created_at", "heartbeat_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return _aware(value, name=info.field_name) if value is not None else None


class ArtifactReferenceV1(StrictCodingModel):
    artifact_id: str
    kind: ArtifactKind
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)
    producer: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid artifact id")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _reference(value, name="path")

    @field_validator("sha256")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        lowered = value.casefold()
        if not _SHA256.fullmatch(lowered):
            raise ValueError("invalid sha256")
        return lowered

    @field_validator("created_at")
    @classmethod
    def validate_created(cls, value: datetime) -> datetime:
        return _aware(value, name="created_at")


class CommandResultV1(StrictCodingModel):
    command_id: str
    argv: list[str] = Field(min_length=1, max_length=64)
    cwd: str
    purpose: str = Field(min_length=1, max_length=256)
    status: CommandStatus
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    output_artifact_id: str | None = None
    summary: str = Field(min_length=1, max_length=2_048)

    @field_validator("command_id", "output_artifact_id")
    @classmethod
    def validate_command_ids(cls, value: str | None) -> str | None:
        if value is not None and not _TASK_ID.fullmatch(value):
            raise ValueError("invalid command/artifact id")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return _reference(value, name="cwd")

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_command_times(cls, value: datetime, info) -> datetime:
        return _aware(value, name=info.field_name)

    @model_validator(mode="after")
    def validate_result(self) -> "CommandResultV1":
        if self.finished_at < self.started_at:
            raise ValueError("command finished before it started")
        if self.status is CommandStatus.PASSED and self.exit_code != 0:
            raise ValueError("passed command requires exit code 0")
        if self.status in {CommandStatus.TIMED_OUT, CommandStatus.CANCELLED, CommandStatus.NOT_RUN}:
            if self.exit_code is not None:
                raise ValueError("non-executed/terminated command must not report an exit code")
        return self


class ExecutionAttemptV1(StrictCodingModel):
    index: int = Field(ge=1, le=10)
    executor: ExecutorKind
    status: AttemptStatus
    strategy: str = Field(min_length=1, max_length=512)
    started_at: datetime
    finished_at: datetime | None = None
    error_summary: str | None = Field(default=None, max_length=2_048)
    command_ids: list[str] = Field(default_factory=list, max_length=128)
    modified_files: list[str] = Field(default_factory=list, max_length=10_000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_attempt_times(cls, value: datetime | None, info) -> datetime | None:
        return _aware(value, name=info.field_name) if value is not None else None

    @field_validator("command_ids", "modified_files", "artifact_ids")
    @classmethod
    def validate_attempt_lists(cls, value: list[str], info) -> list[str]:
        checked = [_reference(item, name=info.field_name) for item in value]
        return _unique(checked, name=info.field_name)

    @model_validator(mode="after")
    def validate_attempt(self) -> "ExecutionAttemptV1":
        if self.status is AttemptStatus.RUNNING and self.finished_at is not None:
            raise ValueError("running attempt cannot have finished_at")
        if self.status is not AttemptStatus.RUNNING and self.finished_at is None:
            raise ValueError("terminal attempt requires finished_at")
        if self.status is AttemptStatus.FAILED and not self.error_summary:
            raise ValueError("failed attempt requires error summary")
        return self


class ReviewFindingV1(StrictCodingModel):
    severity: ReviewSeverity
    code: str = Field(min_length=1, max_length=128)
    file: str | None = Field(default=None, max_length=4_096)
    line: int | None = Field(default=None, ge=1)
    failure_scenario: str = Field(min_length=1, max_length=4_096)
    remediation: str = Field(min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def validate_location(self) -> "ReviewFindingV1":
        if self.line is not None and self.file is None:
            raise ValueError("line requires a file")
        return self


class ReviewResultV1(StrictCodingModel):
    reviewer_id: str
    reviewer: ExecutorKind
    independent: Literal[True] = True
    verdict: ReviewVerdict
    findings: list[ReviewFindingV1] = Field(default_factory=list, max_length=256)
    checked_requirements: bool
    checked_tests: bool
    checked_diff_scope: bool
    checked_secrets: bool
    checked_constitution: bool
    subject_sha256: str | None = None
    evidence_artifact_id: str | None = None
    evidence_artifact_sha256: str | None = None
    summary: str = Field(min_length=1, max_length=4_096)
    reviewed_at: datetime

    @field_validator("reviewer_id")
    @classmethod
    def validate_reviewer_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid reviewer id")
        return value

    @field_validator("subject_sha256", "evidence_artifact_sha256")
    @classmethod
    def validate_review_binding_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("review evidence binding must be sha256")
        return value

    @field_validator("evidence_artifact_id")
    @classmethod
    def validate_review_evidence_artifact_id(cls, value: str | None) -> str | None:
        if value is not None and not _TASK_ID.fullmatch(value):
            raise ValueError("invalid review evidence artifact id")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at(cls, value: datetime) -> datetime:
        return _aware(value, name="reviewed_at")

    @model_validator(mode="after")
    def validate_verdict(self) -> "ReviewResultV1":
        blocking = any(item.severity in {ReviewSeverity.HIGH, ReviewSeverity.CRITICAL} for item in self.findings)
        if self.verdict is ReviewVerdict.APPROVED and blocking:
            raise ValueError("approved review cannot contain high/critical findings")
        bindings = (
            self.subject_sha256,
            self.evidence_artifact_id,
            self.evidence_artifact_sha256,
        )
        if any(item is not None for item in bindings) and not all(
            item is not None for item in bindings
        ):
            raise ValueError("review evidence binding fields are all-or-none")
        if (
            self.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
            and self.verdict is not ReviewVerdict.BLOCKED
            and not all(item is not None for item in bindings)
        ):
            raise ValueError("completed local semantic review requires evidence bindings")
        return self


_UNDELIVERABLE_REVIEW_CODES = {
    "codex.review_unstructured",
    "codex.review_mutated_worktree",
}


def is_successful_review_delivery(
    request: CodingTaskRequestV1,
    review: ReviewResultV1,
) -> bool:
    """Return whether a review result is a valid completed task deliverable.

    An approved independent review is the normal completion gate.  A high-risk
    read-only Codex review is different: concrete findings are the requested
    deliverable, so a rejected verdict can still mean the *review task* itself
    completed successfully.  Keep that exception deliberately narrow and
    fail closed on missing gates, malformed output, or worktree mutation.
    """

    all_gates = all(
        (
            review.checked_requirements,
            review.checked_tests,
            review.checked_diff_scope,
            review.checked_secrets,
            review.checked_constitution,
        )
    )
    codex_delivery_required = (
        request.mode is CodingMode.READ_ONLY
        and request.risk in {CodingRisk.HIGH, CodingRisk.CRITICAL}
        and request.permissions.cloud_execution
        and request.permissions.data_classification is DataClassification.PUBLIC
    )
    if review.verdict is ReviewVerdict.APPROVED:
        return all_gates and (
            not codex_delivery_required
            or review.reviewer is ExecutorKind.CODEX_REVIEW
        )
    return (
        codex_delivery_required
        and review.reviewer is ExecutorKind.CODEX_REVIEW
        and review.verdict is ReviewVerdict.REJECTED
        and bool(review.findings)
        and all_gates
        and not any(item.code in _UNDELIVERABLE_REVIEW_CODES for item in review.findings)
    )


class CodingTaskStateV1(StrictCodingModel):
    schema_version: Literal["1.0"] = CODING_SCHEMA_VERSION
    request: CodingTaskRequestV1
    status: CodingTaskStatus
    source_repository: str
    worktree: WorktreeRecordV1 | None = None
    applicable_rules: list[RuleReferenceV1] = Field(default_factory=list, max_length=64)
    inspected_files: list[str] = Field(default_factory=list, max_length=10_000)
    context_artifact_id: str | None = None
    attempts: list[ExecutionAttemptV1] = Field(default_factory=list, max_length=10)
    command_results: list[CommandResultV1] = Field(default_factory=list, max_length=256)
    artifacts: list[ArtifactReferenceV1] = Field(default_factory=list, max_length=256)
    modified_files: list[str] = Field(default_factory=list, max_length=10_000)
    review: ReviewResultV1 | None = None
    handoff_artifact_id: str | None = None
    commit_sha: str | None = None
    unresolved_errors: list[str] = Field(default_factory=list, max_length=128)
    created_at: datetime
    updated_at: datetime

    @field_validator("source_repository")
    @classmethod
    def validate_source_repository(cls, value: str) -> str:
        return _reference(value, name="source_repository")

    @field_validator("inspected_files", "modified_files", "unresolved_errors")
    @classmethod
    def validate_state_lists(cls, value: list[str], info) -> list[str]:
        checked = [_reference(item, name=info.field_name) for item in value]
        return _unique(checked, name=info.field_name)

    @field_validator("context_artifact_id", "handoff_artifact_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        if value is not None and not _TASK_ID.fullmatch(value):
            raise ValueError("invalid artifact reference")
        return value

    @field_validator("commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.casefold()
        if not _GIT_SHA.fullmatch(lowered):
            raise ValueError("invalid commit SHA")
        return lowered

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_state_times(cls, value: datetime, info) -> datetime:
        return _aware(value, name=info.field_name)

    @model_validator(mode="after")
    def validate_state(self) -> "CodingTaskStateV1":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.request.mode is CodingMode.WRITE and self.status in {
            CodingTaskStatus.ISOLATED,
            CodingTaskStatus.EXECUTING,
            CodingTaskStatus.VERIFYING,
            CodingTaskStatus.REVIEWING,
            CodingTaskStatus.COMPLETED,
            CodingTaskStatus.HANDOFF_READY,
        } and self.worktree is None:
            raise ValueError("write workflow state requires an isolated worktree")
        if self.status is CodingTaskStatus.COMPLETED:
            if self.review is None or not is_successful_review_delivery(self.request, self.review):
                raise ValueError(
                    "completed coding task requires an approved independent review "
                    "or a validated high-risk read-only Codex review delivery"
                )
            if self.review.verdict is ReviewVerdict.REJECTED:
                delivered_attempt = self.attempts[-1] if self.attempts else None
                if (
                    delivered_attempt is None
                    or delivered_attempt.executor is not ExecutorKind.CODEX_REVIEW
                    or delivered_attempt.status is not AttemptStatus.PASSED
                    or delivered_attempt.command_ids
                    or delivered_attempt.modified_files
                    or self.command_results
                    or self.modified_files
                    or self.commit_sha is not None
                    or self.worktree is None
                    or self.worktree.status != "complete"
                ):
                    raise ValueError(
                        "completed rejected review delivery must be a clean, command-free, "
                        "passed Codex review attempt in a completed owned worktree"
                    )
            if self.unresolved_errors:
                raise ValueError("completed coding task cannot retain unresolved errors")
        if self.commit_sha and not self.request.permissions.local_commit:
            raise ValueError("commit SHA cannot exist without commit permission")
        return self


class CodingTaskResultV1(StrictCodingModel):
    schema_version: Literal["1.0"] = CODING_SCHEMA_VERSION
    task_id: str
    status: CodingTaskStatus
    summary: str = Field(min_length=1, max_length=4_096)
    source_repository: str
    worktree_path: str | None
    branch: str | None
    commit_sha: str | None
    attempts: int = Field(ge=0, le=10)
    modified_files: list[str] = Field(default_factory=list, max_length=10_000)
    verification_passed: bool
    review_verdict: ReviewVerdict | None
    final_executor: ExecutorKind | None = None
    final_model: str | None = Field(default=None, max_length=256)
    review_findings_count: int = Field(default=0, ge=0, le=256)
    artifact_paths: list[str] = Field(default_factory=list, max_length=256)
    handoff_path: str | None = None

    @field_validator("task_id")
    @classmethod
    def validate_result_task_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid task id")
        return value

    @field_validator("final_model")
    @classmethod
    def validate_final_model(cls, value: str | None) -> str | None:
        return _reference(value, name="final_model", maximum=256) if value else None
