from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


KNOWLEDGE_SCHEMA_VERSION = "1.0"


class StrictKnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceKind(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    CONVERSATION_JSON = "conversation_json"
    CONVERSATION_HTML = "conversation_html"
    PROJECT_CONFIG = "project_config"
    REPOSITORY_FILE = "repository_file"
    GIT_HISTORY = "git_history"
    REPOSITORY_MAP = "repository_map"


class SourceStatus(StrEnum):
    DISCOVERED = "discovered"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    IMPORTED = "imported"
    UNSUPPORTED = "unsupported"
    STALE = "stale"
    DELETED = "deleted"


class FragmentStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DELETED = "deleted"


class FactKind(StrEnum):
    FACT = "fact"
    DECISION = "decision"


class FactStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFLICTED = "conflicted"
    STALE = "stale"


class FreshnessRequirement(StrEnum):
    ACTIVE_ONLY = "active_only"
    INCLUDE_STALE = "include_stale"


class SourceRegistrationV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    owner_id: str = Field(default="local-user", min_length=1, max_length=128)
    project_path: str = Field(min_length=1, max_length=4_096)
    consent: bool = False
    sensitivity_ceiling: str = Field(default="internal", pattern=r"^(public|internal|sensitive)$")
    adapter_version: str = Field(default="knowledge-1.0", min_length=1, max_length=64)


class ImportRequestV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    registration: SourceRegistrationV1
    source_path: str = Field(min_length=1, max_length=4_096)
    source_kind: SourceKind | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def require_consent(self) -> "ImportRequestV1":
        if not self.registration.consent:
            raise ValueError("source registration requires explicit consent")
        return self


class ProvenanceV1(StrictKnowledgeModel):
    generation_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    source_uri: str = Field(min_length=1, max_length=4_096)
    source_origin: Literal["manual", "repository"]
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_size_bytes: int = Field(ge=0)
    source_mtime_ns: int = Field(ge=0)
    fragment_locator: str = Field(min_length=1, max_length=1_024)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    parser: str = Field(min_length=1, max_length=128)
    parser_version: str = Field(min_length=1, max_length=64)
    derivation_version: str = Field(min_length=1, max_length=512)
    policy_version: str = Field(min_length=1, max_length=128)
    extraction_method: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    project_commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    worktree_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    sensitivity: str = Field(pattern=r"^(public|internal|sensitive)$")
    status: Literal["active", "stale"]

    @field_validator("observed_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ImportResultV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    project_path: str
    source_id: str | None = None
    source_uri: str
    source_kind: SourceKind
    status: SourceStatus
    dry_run: bool
    source_hash: str | None = None
    fragments_parsed: int = Field(default=0, ge=0)
    fragments_published: int = Field(default=0, ge=0)
    facts_published: int = Field(default=0, ge=0)
    unchanged: bool = False
    renamed_from: str | None = None
    reason_code: str | None = None


class RetrievalRequestV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    owner_id: str = Field(default="local-user", min_length=1, max_length=128)
    project_path: str = Field(min_length=1, max_length=4_096)
    query: str = Field(min_length=1, max_length=2_048)
    allowed_source_types: list[SourceKind] | None = Field(default=None, max_length=16)
    token_budget: int = Field(default=2_000, ge=128, le=32_768)
    max_fragments: int = Field(default=8, ge=1, le=32)
    freshness: FreshnessRequirement = FreshnessRequirement.ACTIVE_ONLY


class RetrievedFragmentV1(StrictKnowledgeModel):
    fragment_id: str
    source_kind: SourceKind
    content: str = Field(min_length=1, max_length=16_384)
    title: str | None = None
    provenance: ProvenanceV1
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=512)
    estimated_tokens: int = Field(ge=1)
    stale: bool = False
    conflict: bool = False
    untrusted: Literal[True] = True
    local_only: Literal[True] = True


class RetrievalResultV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    project_path: str
    query: str
    token_budget: int
    estimated_tokens: int = Field(ge=0)
    fragments: list[RetrievedFragmentV1]
    degraded: bool = False
    reason_code: str | None = None
    next_offset: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def enforce_budget(self) -> "RetrievalResultV1":
        if self.estimated_tokens > self.token_budget:
            raise ValueError("retrieval estimate exceeds token budget")
        if self.estimated_tokens != sum(item.estimated_tokens for item in self.fragments):
            raise ValueError("retrieval estimate must equal fragment estimates")
        return self


class RepositoryFileV1(StrictKnowledgeModel):
    path: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    language: str | None = None
    category: str
    symbols: list[str] = Field(default_factory=list, max_length=2_000)
    indexed: bool = False
    exclusion_reason: str | None = None
    git_commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    dirty: bool = False
    git_object_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")
    git_index_object_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")
    git_worktree_object_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40,64}$")


class BlockedRepositorySourceV1(StrictKnowledgeModel):
    path: str | None = Field(default=None, max_length=4_096)
    path_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason_code: str = Field(min_length=1, max_length=128)


class RepositoryMapV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    map_version: str = "1.0"
    owner_id: str = "local-user"
    project_path: str
    git_commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,64}$")
    git_remote: str | None = None
    worktree_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version: str = Field(min_length=1, max_length=128)
    tracked_files_count: int = Field(ge=0)
    blocked_files_count: int = Field(ge=0)
    generated_at: datetime
    languages: dict[str, int]
    manifests: list[str]
    entry_points: list[str]
    modules: list[str]
    tests: list[str]
    commands: list[str]
    documentation: list[str]
    agents_hierarchy: list[str]
    files: list[RepositoryFileV1]
    blocked_sources: list[BlockedRepositorySourceV1] = Field(default_factory=list)
    untrusted: Literal[True] = True
    local_only: Literal[True] = True
    stale: bool = False

    @field_validator("generated_at")
    @classmethod
    def generated_timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ContextEnvelopeV1(StrictKnowledgeModel):
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION
    project_path: str
    goal: str = Field(min_length=1, max_length=2_048)
    constraints: list[Annotated[str, Field(max_length=2_048)]] = Field(default_factory=list, max_length=64)
    modified_files: list[Annotated[str, Field(max_length=1_024)]] = Field(default_factory=list, max_length=256)
    unresolved_errors: list[Annotated[str, Field(max_length=2_048)]] = Field(default_factory=list, max_length=64)
    verification_plan: list[Annotated[str, Field(max_length=2_048)]] = Field(default_factory=list, max_length=64)
    fresh_tool_results: list[Annotated[str, Field(max_length=2_048)]] = Field(default_factory=list, max_length=32)
    repository_summary: dict[str, Any]
    evidence: RetrievalResultV1
    token_budget: int = Field(ge=128, le=32_768)
    estimated_tokens: int = Field(ge=0)
    untrusted_evidence: Literal[True] = True
    repository_summary_untrusted: Literal[True] = True
    untrusted_tool_results: Literal[True] = True
    local_only: Literal[True] = True
    degraded: bool = False
    reason_code: str | None = None

    @model_validator(mode="after")
    def enforce_budget(self) -> "ContextEnvelopeV1":
        if self.estimated_tokens > self.token_budget:
            raise ValueError("context estimate exceeds token budget")
        if self.evidence.estimated_tokens > self.evidence.token_budget:
            raise ValueError("evidence estimate exceeds evidence budget")
        return self
