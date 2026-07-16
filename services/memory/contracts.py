from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MEMORY_RECORD_SCHEMA_VERSION = "1.0"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
class MemoryRecordType(StrEnum):
    USER_PROFILE = "user_profile"
    PROJECT_KNOWLEDGE = "project_knowledge"
    TASK_HISTORY = "task_history"
    OPERATIONAL_STATE = "operational_state"
    ARCHIVE_REFERENCE = "archive_reference"


class MemoryScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    TASK = "task"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"
    STALE = "stale"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemorySensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class MemoryRetention(StrEnum):
    SESSION = "session"
    TASK = "task"
    TTL = "ttl"
    MANUAL = "manual"
    PERMANENT = "permanent"


class StrictMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MemorySourceV1(StrictMemoryModel):
    source_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    uri: str | None = Field(default=None, max_length=2_048)
    fragment: str | None = Field(default=None, max_length=1_024)
    source_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    source_mtime_ns: int | None = Field(default=None, ge=0)
    producer: str = Field(default="user", min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    author: str = Field(default="local-user", min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class TaskHistoryValueV1(StrictMemoryModel):
    """Bounded task evidence; never a raw prompt, chat or tool transcript."""

    goal_summary: str = Field(min_length=1, max_length=2_048)
    executor: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    route: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    attempts: int = Field(ge=0, le=100)
    modified_files: list[str] = Field(default_factory=list, max_length=10_000)
    tests: list[str] = Field(default_factory=list, max_length=128)
    artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    failures: list[str] = Field(default_factory=list, max_length=64)


class OperationalStateValueV1(StrictMemoryModel):
    """Short-lived resumable state with explicit lease and heartbeat metadata."""

    active_goal: str = Field(min_length=1, max_length=2_048)
    stage: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    unresolved_errors: list[str] = Field(default_factory=list, max_length=64)
    next_action: str | None = Field(default=None, max_length=2_048)
    heartbeat_at: datetime
    lease_owner: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)
    lease_expires_at: datetime | None = None

    @field_validator("heartbeat_at", "lease_expires_at")
    @classmethod
    def aware_operational_timestamp(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("operational timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_lease(self) -> "OperationalStateValueV1":
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be provided together")
        return self


class ArchiveReferenceMetadataV1(StrictMemoryModel):
    """Small typed facts about an archive, never renamed free-form content."""

    created_at: datetime | None = None
    updated_at: datetime | None = None
    captured_at: datetime | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    item_count: int | None = Field(default=None, ge=0)
    message_count: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    language: str | None = Field(default=None, max_length=32, pattern=_IDENTIFIER_PATTERN)
    external_id: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)
    source_version: str | None = Field(default=None, max_length=128, pattern=_IDENTIFIER_PATTERN)

    @field_validator("created_at", "updated_at", "captured_at")
    @classmethod
    def aware_archive_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("archive timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class ArchiveReferenceValueV1(StrictMemoryModel):
    """Metadata-only pointer to a source that is not active memory content."""

    archive_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    kind: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    uri: str | None = Field(default=None, max_length=2_048)
    source_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    media_type: str | None = Field(default=None, max_length=128)
    metadata: ArchiveReferenceMetadataV1 = Field(
        default_factory=ArchiveReferenceMetadataV1
    )

    @field_validator("uri")
    @classmethod
    def validate_reference_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "://" not in value:
            raise ValueError("archive URI must use an explicit allowed scheme")
        scheme = urlsplit(value).scheme.casefold()
        if scheme not in {"archive", "http", "https", "local-file", "project"}:
            raise ValueError("archive URI scheme is not allowed")
        return value

class MemoryUpsertV1(StrictMemoryModel):
    record_type: MemoryRecordType
    scope: MemoryScope
    subject: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    value: JsonValue
    source: MemorySourceV1
    owner_id: str = Field(default="local-user", min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    project_path: str | None = Field(default=None, max_length=4_096)
    task_id: str | None = Field(default=None, max_length=256)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    project_commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    supersedes_record_id: str | None = Field(default=None, max_length=128)
    sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL
    retention: MemoryRetention = MemoryRetention.MANUAL
    expires_at: datetime | None = None
    actor: str = Field(default="local-user", min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)

    @field_validator("valid_from", "valid_to", "expires_at")
    @classmethod
    def aware_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_scope_and_retention(self) -> "MemoryUpsertV1":
        if self.scope is MemoryScope.PROJECT and not self.project_path:
            raise ValueError("project scope requires project_path")
        if self.scope is MemoryScope.TASK and not self.task_id:
            raise ValueError("task scope requires task_id")
        if self.scope is MemoryScope.USER and (self.project_path or self.task_id):
            raise ValueError("user scope cannot carry project_path or task_id")
        if self.scope is MemoryScope.PROJECT and self.task_id:
            raise ValueError("project scope cannot carry task_id")
        required_scope = {
            MemoryRecordType.USER_PROFILE: MemoryScope.USER,
            MemoryRecordType.PROJECT_KNOWLEDGE: MemoryScope.PROJECT,
            MemoryRecordType.TASK_HISTORY: MemoryScope.TASK,
            MemoryRecordType.OPERATIONAL_STATE: MemoryScope.TASK,
        }.get(self.record_type)
        if required_scope is not None and self.scope is not required_scope:
            raise ValueError(f"{self.record_type.value} requires {required_scope.value} scope")
        if self.retention is MemoryRetention.TTL and self.expires_at is None:
            raise ValueError("ttl retention requires expires_at")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.status not in {MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED}:
            raise ValueError("new records may only be candidate or confirmed")
        if self.record_type is MemoryRecordType.ARCHIVE_REFERENCE and not isinstance(self.value, dict):
            raise ValueError("archive references must contain metadata, not archive content")
        return self


class MemoryRecordV1(StrictMemoryModel):
    schema_version: str = MEMORY_RECORD_SCHEMA_VERSION
    record_id: str
    record_type: MemoryRecordType
    owner_id: str
    scope: MemoryScope
    scope_key: str
    project_path: str | None
    task_id: str | None
    subject: str
    value: JsonValue
    sources: list[MemorySourceV1]
    created_at: datetime
    observed_at: datetime
    updated_at: datetime
    confidence: float
    status: MemoryStatus
    valid_from: datetime | None
    valid_to: datetime | None
    project_commit_sha: str | None
    producer: str
    author: str
    supersedes_record_id: str | None
    sensitivity: MemorySensitivity
    retention: MemoryRetention
    expires_at: datetime | None
    deleted_at: datetime | None
    revision: int


class RetrievalItemV1(StrictMemoryModel):
    record_id: str
    record_type: MemoryRecordType
    subject: str
    value: JsonValue
    score: float = Field(ge=0.0, le=1.0)
    why: str = Field(min_length=1, max_length=512)
    source_refs: list[str] = Field(max_length=32)
    project_commit_sha: str | None = None


class RetrievalResultV1(StrictMemoryModel):
    items: list[RetrievalItemV1]
    used_chars: int = Field(ge=0)
    max_chars: int = Field(ge=1)
    degraded: bool = False
    diagnostic: str | None = Field(default=None, max_length=256)


def json_compatible(value: Any) -> JsonValue:
    """Typing helper used by the CLI after decoding JSON."""

    return value
