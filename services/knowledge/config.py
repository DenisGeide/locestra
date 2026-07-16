from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "knowledge.json"


class KnowledgePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    policy_version: str
    tracked_files_only: bool = True
    allowed_root_files: tuple[str, ...]
    allowed_directories: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    blocked_directory_names: tuple[str, ...]
    blocked_file_names: tuple[str, ...]
    blocked_file_suffixes: tuple[str, ...]
    max_file_bytes: int = Field(ge=1_024, le=16_777_216)
    max_total_bytes: int = Field(ge=1_024, le=268_435_456)
    max_fragment_chars: int = Field(ge=256, le=16_384)
    max_fragments_per_source: int = Field(ge=1, le=100_000)
    max_git_history_entries: int = Field(ge=1, le=10_000)
    max_tracked_files: int = Field(ge=1, le=1_000_000)
    max_git_output_bytes: int = Field(ge=1_024, le=268_435_456)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("unsupported knowledge policy schema")
        return value

    @field_validator("allowed_extensions")
    @classmethod
    def normalize_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(not value.startswith(".") for value in values):
            raise ValueError("allowed extensions must be non-empty dotted suffixes")
        normalized = tuple(value.casefold() for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate allowed extension")
        return normalized


@lru_cache(maxsize=4)
def load_knowledge_policy(path: str | Path = DEFAULT_CONFIG_PATH) -> KnowledgePolicy:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return KnowledgePolicy.model_validate(payload)
