from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.common import ROOT


class CodingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str
    policy_version: str
    branch_prefix: str
    max_local_attempts: int = Field(ge=1, le=2)
    lease_acquire_timeout_seconds: int = Field(ge=1, le=3_600)
    lease_heartbeat_seconds: int = Field(ge=1, le=60)
    lease_stale_seconds: int = Field(ge=10, le=86_400)
    process_poll_seconds: float = Field(gt=0.0, le=5.0)
    qwen_timeout_seconds: int = Field(ge=10, le=7_200)
    codex_timeout_seconds: int = Field(ge=10, le=14_400)
    verification_timeout_seconds: int = Field(ge=1, le=7_200)
    review_timeout_seconds: int = Field(ge=10, le=7_200)
    context_token_budget: int = Field(ge=512, le=32_768)
    max_artifact_bytes: int = Field(ge=1_024, le=64 * 1024 * 1024)
    max_output_chars: int = Field(ge=1_000, le=1_000_000)
    max_diff_bytes: int = Field(ge=1_024, le=64 * 1024 * 1024)
    qwen_agent_memory_bytes: int = Field(default=8 * 1024**3, ge=64 * 1024**2, le=64 * 1024**3)
    qwen_agent_memory_swap_bytes: int = Field(default=8 * 1024**3, ge=64 * 1024**2, le=64 * 1024**3)
    qwen_agent_cpus: float = Field(default=8.0, ge=0.1, le=64.0)
    qwen_proxy_memory_bytes: int = Field(default=512 * 1024**2, ge=64 * 1024**2, le=8 * 1024**3)
    qwen_proxy_memory_swap_bytes: int = Field(default=512 * 1024**2, ge=64 * 1024**2, le=8 * 1024**3)
    qwen_proxy_cpus: float = Field(default=1.0, ge=0.1, le=8.0)
    qwen_probe_memory_bytes: int = Field(default=256 * 1024**2, ge=64 * 1024**2, le=4 * 1024**3)
    qwen_probe_memory_swap_bytes: int = Field(default=256 * 1024**2, ge=64 * 1024**2, le=4 * 1024**3)
    qwen_probe_cpus: float = Field(default=0.5, ge=0.1, le=4.0)
    verifier_memory_bytes: int = Field(default=8 * 1024**3, ge=64 * 1024**2, le=64 * 1024**3)
    verifier_memory_swap_bytes: int = Field(default=8 * 1024**3, ge=64 * 1024**2, le=64 * 1024**3)
    verifier_cpus: float = Field(default=8.0, ge=0.1, le=64.0)
    qwen_max_writable_bytes: int = Field(default=4 * 1024**3, ge=1024**2, le=128 * 1024**3)
    verifier_max_writable_bytes: int = Field(default=4 * 1024**3, ge=1024**2, le=128 * 1024**3)
    host_free_space_reserve_bytes: int = Field(default=32 * 1024**3, ge=1024**2, le=1024**4)
    free_space_watchdog_poll_seconds: float = Field(default=0.1, ge=0.05, le=5.0)
    writable_watchdog_poll_seconds: float = Field(default=1.0, ge=0.05, le=10.0)
    writable_watchdog_scan_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    writable_watchdog_max_entries: int = Field(default=300_000, ge=100, le=2_000_000)
    local_semantic_model: str = "local-strong"
    local_semantic_expected_model_digest: str = (
        "005d4fcb23bcdfccb3e919c6844cb550dc91972f207cb6f5d52184115ef44573"
    )
    local_semantic_expected_executable_path: str = "auto"
    local_semantic_expected_executable_sha256: str = "auto"
    allowed_verification_programs: tuple[str, ...] = Field(min_length=1, max_length=64)
    denied_verification_tokens: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("unsupported coding policy schema")
        return value

    @field_validator("policy_version")
    @classmethod
    def validate_policy_version(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", value):
            raise ValueError("invalid coding policy version")
        return value

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{1,63}", value) or not value.endswith("-"):
            raise ValueError("branch prefix must be a safe lowercase Git prefix ending in '-'")
        if ".." in value or "//" in value or value.endswith("."):
            raise ValueError("unsafe branch prefix")
        return value

    @field_validator("allowed_verification_programs", "denied_verification_tokens")
    @classmethod
    def validate_unique_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.casefold() for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("coding policy tokens must be unique")
        if any(not item or any(char.isspace() for char in item) for item in value):
            raise ValueError("coding policy tokens must be non-empty single tokens")
        return value

    @field_validator("local_semantic_expected_model_digest")
    @classmethod
    def validate_local_semantic_sha256(cls, value: str) -> str:
        lowered = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", lowered):
            raise ValueError("local semantic identity digest must be SHA-256")
        return lowered

    @field_validator("local_semantic_expected_executable_sha256")
    @classmethod
    def validate_local_semantic_executable_sha256(cls, value: str) -> str:
        lowered = value.casefold()
        if lowered == "auto" or re.fullmatch(r"[0-9a-f]{64}", lowered):
            return lowered
        raise ValueError(
            "local semantic executable digest must be 'auto' or SHA-256"
        )

    @field_validator("local_semantic_model")
    @classmethod
    def validate_local_semantic_model(cls, value: str) -> str:
        if value != "local-strong":
            raise ValueError("local semantic reviewer requires the local-strong alias")
        return value

    @field_validator("local_semantic_expected_executable_path")
    @classmethod
    def validate_local_semantic_executable_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized == "auto":
            return normalized
        windows_path = re.fullmatch(
            r"[A-Za-z]:/[^\x00-\x1f]+/ollama\.exe",
            normalized,
            re.I,
        )
        posix_path = re.fullmatch(r"/[^\x00-\x1f]+/ollama", normalized)
        if not windows_path and not posix_path:
            raise ValueError(
                "local semantic Ollama executable must be 'auto' or an absolute path"
            )
        return normalized

    @model_validator(mode="after")
    def validate_container_resource_relations(self) -> "CodingPolicy":
        for memory, swap in (
            (self.qwen_agent_memory_bytes, self.qwen_agent_memory_swap_bytes),
            (self.qwen_proxy_memory_bytes, self.qwen_proxy_memory_swap_bytes),
            (self.qwen_probe_memory_bytes, self.qwen_probe_memory_swap_bytes),
            (self.verifier_memory_bytes, self.verifier_memory_swap_bytes),
        ):
            if swap < memory:
                raise ValueError("container memory-swap must be at least memory")
        return self


def load_coding_policy(path: Path | None = None) -> CodingPolicy:
    source = path or ROOT / "config" / "coding.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    for key in ("allowed_verification_programs", "denied_verification_tokens"):
        if isinstance(payload.get(key), list):
            payload[key] = tuple(payload[key])
    return CodingPolicy.model_validate(payload)


@lru_cache(maxsize=1)
def get_coding_policy() -> CodingPolicy:
    return load_coding_policy()
