from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
ROUTING_CONFIG_PATH = ROOT / "config" / "routing.json"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoutingThresholds(_StrictFrozenModel):
    strong_chat_chars: int = Field(ge=128, le=100_000)
    high_complexity_markers: int = Field(ge=1, le=16)
    critical_complexity_markers: int = Field(ge=1, le=16)
    local_code_max_attempts: int = Field(ge=1, le=4)
    fast_input_tokens: int = Field(ge=256, le=2_000_000)
    strong_input_tokens: int = Field(ge=256, le=2_000_000)
    agent_input_tokens: int = Field(ge=256, le=2_000_000)
    max_attachment_bytes: int = Field(ge=0, le=4_294_967_296)
    max_tool_output_chars: int = Field(ge=0, le=10_000_000)


class LlmSignalPolicy(_StrictFrozenModel):
    enabled: bool
    timeout_ms: int = Field(ge=50, le=10_000)
    minimum_confidence: float = Field(ge=0.5, le=1.0)


class RoutingRules(_StrictFrozenModel):
    auxiliary: list[str]
    educational: list[str]
    repository_context: list[str]
    code_targets: list[str]
    read_actions: list[str]
    mutation_actions: list[str]
    review_actions: list[str]
    read_only: list[str]
    docs: list[str]
    browser: list[str]
    image: list[str]
    strong: list[str]
    architecture: list[str]
    high_complexity: list[str]
    critical: list[str]

    @field_validator("*")
    @classmethod
    def validate_rule_list(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("routing markers must contain 1..128 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("routing marker lists must not contain duplicates")
        if len(normalized) > 256:
            raise ValueError("routing marker list exceeds bounded policy size")
        return normalized


class RoutingPolicy(_StrictFrozenModel):
    schema_version: str
    policy_version: str = Field(min_length=1, max_length=64)
    thresholds: RoutingThresholds
    planner_routes: list[str] = Field(min_length=1, max_length=32)
    rules: RoutingRules
    llm_signal: LlmSignalPolicy

    @field_validator("schema_version")
    @classmethod
    def require_schema_v1(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError(f"unsupported routing policy schema: {value!r}")
        return value

    @field_validator("planner_routes")
    @classmethod
    def unique_names(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("routing policy lists must not contain duplicates")
        required = {"strong_chat", "local_code", "codex", "docs", "browser", "image", "voice", "vision"}
        if set(values) != required:
            raise ValueError("planner_routes must contain every executor route exactly once")
        return values


def load_routing_policy(path: Path = ROUTING_CONFIG_PATH) -> RoutingPolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RoutingPolicy.model_validate(payload)


@lru_cache(maxsize=1)
def get_routing_policy() -> RoutingPolicy:
    return load_routing_policy()
