from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONFIG_PATH = ROOT / "config" / "platform.json"
RUNTIME_ENV_PATH = ROOT / ".env"

# These addresses are still duplicated in the Stage 001 PowerShell/Compose
# compatibility control plane.  Accepting an override in Python alone would
# split the platform, so fail fast until a coordinated port/endpoint migration
# changes every consumer in one revision.
LIFECYCLE_COMPATIBILITY_BASELINE = {
    "ollama_base_url": "http://127.0.0.1:11434",
    "fast_ollama_base_url": "http://127.0.0.1:11435",
    "gateway_port": 8787,
    "voice_port": 8788,
    "open_webui_port": 3737,
    "n8n_port": 5678,
    "comfyui_url": "http://127.0.0.1:8388",
}


class PlatformSettings(BaseModel):
    """Resolved public settings plus env-only secret handles.

    Values are resolved once per process. Secret values use ``SecretStr`` so an
    accidental model dump or exception does not reveal them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    local_fast_model: str = "local-fast"
    local_strong_model: str = "local-strong"
    local_agent_model: str = "local-strong"
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "high"
    ollama_base_url: str = "http://127.0.0.1:11434"
    fast_ollama_base_url: str = "http://127.0.0.1:11435"
    gateway_port: int = Field(default=8787, ge=1, le=65535)
    voice_port: int = Field(default=8788, ge=1, le=65535)
    open_webui_port: int = Field(default=3737, ge=1, le=65535)
    n8n_port: int = Field(default=5678, ge=1, le=65535)
    default_project: str = str(ROOT)
    enable_local_code_exec: bool = True
    enable_codex_exec: bool = False
    codex_sandbox: str = "workspace-write"
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    telegram_credential: SecretStr = SecretStr("")
    gateway_credential: SecretStr = SecretStr("")
    telegram_gateway_url: str = "http://127.0.0.1:8787/v1/chat/completions"
    telegram_voice_url: str = "http://127.0.0.1:8788/v1/audio/transcriptions"
    comfyui_url: str = "http://127.0.0.1:8388"
    max_automatic_chat_tools: int = Field(default=8, ge=0, le=256)

    @field_validator(
        "ollama_base_url",
        "fast_ollama_base_url",
        "telegram_gateway_url",
        "telegram_voice_url",
        "comfyui_url",
    )
    @classmethod
    def require_http_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("endpoint must use http:// or https://")
        return normalized.rstrip("/")


ENV_TO_FIELD = {
    "LOCAL_FAST_MODEL": "local_fast_model",
    "LOCAL_STRONG_MODEL": "local_strong_model",
    "LOCAL_AGENT_MODEL": "local_agent_model",
    "CODEX_MODEL": "codex_model",
    "CODEX_REASONING_EFFORT": "codex_reasoning_effort",
    "OLLAMA_BASE_URL": "ollama_base_url",
    "FAST_OLLAMA_BASE_URL": "fast_ollama_base_url",
    "GATEWAY_PORT": "gateway_port",
    "VOICE_PORT": "voice_port",
    "OPEN_WEBUI_PORT": "open_webui_port",
    "N8N_PORT": "n8n_port",
    "DEFAULT_PROJECT": "default_project",
    "ENABLE_LOCAL_CODE_EXEC": "enable_local_code_exec",
    "ENABLE_CODEX_EXEC": "enable_codex_exec",
    "CODEX_SANDBOX": "codex_sandbox",
    "WHISPER_MODEL": "whisper_model",
    "WHISPER_DEVICE": "whisper_device",
    "WHISPER_COMPUTE_TYPE": "whisper_compute_type",
    **dict([("TELEGRAM_BOT_TOKEN", "telegram_credential")]),
    "GATEWAY_API_KEY": "gateway_credential",
    "TELEGRAM_GATEWAY_URL": "telegram_gateway_url",
    "TELEGRAM_VOICE_URL": "telegram_voice_url",
    "COMFYUI_URL": "comfyui_url",
    "MAX_AUTOMATIC_CHAT_TOOLS": "max_automatic_chat_tools",
}
ENV_ONLY_KEYS = {"TELEGRAM_BOT_TOKEN", "GATEWAY_API_KEY"}


def _committed_values(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("committed config must be a JSON object")
    unknown_top_level = sorted(set(payload) - {"schema_version", "settings"})
    if unknown_top_level:
        raise ValueError(
            f"unknown committed config top-level keys: {', '.join(unknown_top_level)}"
        )
    if payload.get("schema_version") != "1.0":
        raise ValueError(f"unsupported committed config schema: {payload.get('schema_version')!r}")
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("committed config must contain an object named settings")
    unknown = sorted(set(settings) - set(ENV_TO_FIELD))
    if unknown:
        raise ValueError(f"unknown committed config keys: {', '.join(unknown)}")
    forbidden = sorted(set(settings) & ENV_ONLY_KEYS)
    if forbidden:
        raise ValueError(f"env-only secret keys are forbidden in committed config: {', '.join(forbidden)}")
    return settings


def _apply_layer(target: dict[str, Any], layer: Mapping[str, Any]) -> None:
    for environment_name, field_name in ENV_TO_FIELD.items():
        value = layer.get(environment_name)
        if value is not None and value != "":
            target[field_name] = value


def load_platform_settings(
    *,
    config_path: Path = COMMITTED_CONFIG_PATH,
    env_file_path: Path | None = RUNTIME_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> PlatformSettings:
    """Resolve defaults < committed config < .env < process environment."""

    values = PlatformSettings().model_dump()
    committed = _committed_values(config_path)
    _apply_layer(values, committed)
    if env_file_path is not None and env_file_path.exists():
        _apply_layer(values, dotenv_values(env_file_path))
    _apply_layer(values, os.environ if environ is None else environ)
    values["schema_version"] = "1.0"
    settings = PlatformSettings.model_validate(values)
    incompatible = [
        field_name
        for field_name, supported_value in LIFECYCLE_COMPATIBILITY_BASELINE.items()
        if getattr(settings, field_name) != supported_value
    ]
    if incompatible:
        raise ValueError(
            "protected lifecycle override requires a coordinated migration: "
            + ", ".join(sorted(incompatible))
        )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> PlatformSettings:
    return load_platform_settings()
