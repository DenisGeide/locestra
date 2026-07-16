import json

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from services.config import COMMITTED_CONFIG_PATH, RUNTIME_ENV_PATH, load_platform_settings


def write_config(path, settings):
    path.write_text(
        json.dumps({"schema_version": "1.0", "settings": settings}),
        encoding="utf-8",
    )


def test_configuration_precedence_defaults_committed_env_file_and_process(tmp_path):
    config_path = tmp_path / "platform.json"
    env_path = tmp_path / ".env"
    write_config(
        config_path,
        {"LOCAL_FAST_MODEL": "committed-fast", "MAX_AUTOMATIC_CHAT_TOOLS": 9},
    )
    env_path.write_text(
        "LOCAL_FAST_MODEL=env-fast\nMAX_AUTOMATIC_CHAT_TOOLS=10\n",
        encoding="utf-8",
    )

    settings = load_platform_settings(
        config_path=config_path,
        env_file_path=env_path,
        environ={"MAX_AUTOMATIC_CHAT_TOOLS": "11"},
    )

    assert settings.local_fast_model == "env-fast"
    assert settings.max_automatic_chat_tools == 11
    assert settings.gateway_port == 8787
    assert settings.local_strong_model == "local-strong"


def test_committed_config_rejects_unknown_and_secret_keys(tmp_path):
    config_path = tmp_path / "platform.json"
    write_config(config_path, {"NOT_A_SETTING": "value"})
    with pytest.raises(ValueError, match="unknown committed config"):
        load_platform_settings(config_path=config_path, env_file_path=None, environ={})

    write_config(config_path, {"TELEGRAM_BOT_TOKEN": "test-must-not-be-committed"})
    with pytest.raises(ValueError, match="env-only secret"):
        load_platform_settings(config_path=config_path, env_file_path=None, environ={})

    write_config(config_path, {"GATEWAY_API_KEY": "test-must-not-be-committed"})
    with pytest.raises(ValueError, match="env-only secret"):
        load_platform_settings(config_path=config_path, env_file_path=None, environ={})


def test_committed_config_rejects_unknown_top_level_keys(tmp_path):
    config_path = tmp_path / "platform.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "settings": {},
                "TELEGRAM_BOT_TOKEN": "test-not-a-valid-top-level-field",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown committed config top-level"):
        load_platform_settings(config_path=config_path, env_file_path=None, environ={})


def test_secret_is_env_only_and_redacted_in_representation(tmp_path):
    config_path = tmp_path / "platform.json"
    write_config(config_path, {})
    settings = load_platform_settings(
        config_path=config_path,
        env_file_path=None,
        environ={
            "TELEGRAM_BOT_TOKEN": "test-runtime-only-value",
            "GATEWAY_API_KEY": "test-runtime-gateway-value",
        },
    )

    assert settings.telegram_credential.get_secret_value() == "test-runtime-only-value"
    assert settings.gateway_credential.get_secret_value() == "test-runtime-gateway-value"
    assert "test-runtime-only-value" not in repr(settings)
    assert "test-runtime-gateway-value" not in repr(settings)


def test_invalid_endpoint_and_port_are_rejected(tmp_path):
    config_path = tmp_path / "platform.json"
    write_config(config_path, {"OLLAMA_BASE_URL": "file:///tmp/model", "VOICE_PORT": 70000})
    with pytest.raises(ValidationError):
        load_platform_settings(config_path=config_path, env_file_path=None, environ={})


def test_protected_lifecycle_override_fails_before_split_runtime(tmp_path):
    config_path = tmp_path / "platform.json"
    write_config(config_path, {})

    with pytest.raises(ValueError, match="coordinated migration"):
        load_platform_settings(
            config_path=config_path,
            env_file_path=None,
            environ={"GATEWAY_PORT": "9003"},
        )


def test_committed_baseline_matches_public_environment_template():
    settings = load_platform_settings(
        config_path=COMMITTED_CONFIG_PATH,
        env_file_path=None,
        environ={},
    )
    public = dotenv_values(RUNTIME_ENV_PATH.with_name(".env.example"))

    assert settings.local_fast_model == public["LOCAL_FAST_MODEL"]
    assert settings.local_strong_model == public["LOCAL_STRONG_MODEL"]
    assert settings.local_agent_model == public["LOCAL_AGENT_MODEL"]
    assert settings.codex_model == public["CODEX_MODEL"]
    assert settings.ollama_base_url == public["OLLAMA_BASE_URL"]
    assert settings.fast_ollama_base_url == public["FAST_OLLAMA_BASE_URL"]
    assert settings.gateway_port == int(public["GATEWAY_PORT"])
    assert settings.voice_port == int(public["VOICE_PORT"])
    assert settings.open_webui_port == int(public["OPEN_WEBUI_PORT"])
    assert settings.n8n_port == int(public["N8N_PORT"])
    assert settings.comfyui_url == public["COMFYUI_URL"]
    assert settings.whisper_model == public["WHISPER_MODEL"]
    assert public["GATEWAY_API_KEY"] == ""
