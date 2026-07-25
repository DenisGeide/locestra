from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from services.gateway import app as gateway
from services.telegram import bot as telegram_bot
from services.voice import app as voice


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_CREDENTIAL = "synthetic-local-service-credential"


def _request(path: str, authorization: str | None = None) -> Request:
    request_path, separator, query = path.partition("?")
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": request_path,
            "raw_path": request_path.encode("ascii"),
            "query_string": query.encode("ascii") if separator else b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8788),
        }
    )


def test_voice_openai_boundary_requires_shared_generated_bearer(monkeypatch):
    monkeypatch.setattr(voice, "VOICE_BEARER_CREDENTIAL", SYNTHETIC_CREDENTIAL)

    async def invoke(path: str, authorization: str | None):
        observed = []

        async def next_handler(_request):
            observed.append(True)
            return JSONResponse({"ok": True})

        response = await voice.authenticate_openai_boundary(
            _request(path, authorization), next_handler
        )
        return response, observed

    missing, missing_observed = asyncio.run(invoke("/v1/audio/transcriptions", None))
    wrong, wrong_observed = asyncio.run(
        invoke("/v1/audio/transcriptions", "Bearer wrong")
    )
    accepted, accepted_observed = asyncio.run(
        invoke("/v1/audio/transcriptions", f"Bearer {SYNTHETIC_CREDENTIAL}")
    )
    health, health_observed = asyncio.run(invoke("/health", None))
    load_missing, load_missing_observed = asyncio.run(
        invoke("/health?load_model=true", None)
    )
    load_false_missing, load_false_missing_observed = asyncio.run(
        invoke("/health?load_model=false", None)
    )
    load_accepted, load_accepted_observed = asyncio.run(
        invoke(
            "/health?load_model=true",
            f"Bearer {SYNTHETIC_CREDENTIAL}",
        )
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert not missing_observed and not wrong_observed
    assert accepted.status_code == 200
    assert accepted_observed == [True]
    assert health.status_code == 200
    assert health_observed == [True]
    assert load_missing.status_code == 401
    assert not load_missing_observed
    assert load_false_missing.status_code == 401
    assert not load_false_missing_observed
    assert load_accepted.status_code == 200
    assert load_accepted_observed == [True]


def test_voice_boundary_fails_closed_when_runtime_credential_is_missing(monkeypatch):
    monkeypatch.setattr(voice, "VOICE_BEARER_CREDENTIAL", "")

    async def next_handler(_request):
        raise AssertionError("an empty credential must never reach transcription")

    response = asyncio.run(
        voice.authenticate_openai_boundary(
            _request("/v1/audio/transcriptions", "Bearer anything"), next_handler
        )
    )

    assert response.status_code == 401


def test_gateway_voice_adapter_forwards_shared_bearer(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"text": "authenticated transcript", "language": "en", "duration": 1}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(gateway, "GATEWAY_BEARER_CREDENTIAL", SYNTHETIC_CREDENTIAL)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", FakeClient)

    transcript = asyncio.run(
        gateway.transcribe_chat_audio(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "YXVkaW8=", "format": "wav"},
                        },
                    ],
                }
            ]
        )
    )

    assert transcript == "authenticated transcript"
    assert captured["headers"] == {
        "Authorization": f"Bearer {SYNTHETIC_CREDENTIAL}"
    }
    assert captured["url"].endswith("/v1/audio/transcriptions")


def test_telegram_uses_same_runtime_bearer_for_gateway_and_voice(monkeypatch):
    monkeypatch.setattr(telegram_bot, "GATEWAY_AUTH", SYNTHETIC_CREDENTIAL)
    assert telegram_bot.gateway_headers() == {
        "Authorization": f"Bearer {SYNTHETIC_CREDENTIAL}"
    }


def test_open_webui_receives_runtime_bearer_and_ui_ports_remain_loopback():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'OPENAI_API_KEYS: "${GATEWAY_API_KEY}"' in compose
    assert 'AUDIO_STT_OPENAI_API_KEY: "${GATEWAY_API_KEY}"' in compose
    assert '"127.0.0.1:${OPEN_WEBUI_PORT:-3000}:8080"' in compose
    assert '"127.0.0.1:${N8N_PORT:-5678}:5678"' in compose


def test_startup_verifies_authenticated_host_listeners_and_warns_about_firewall():
    start = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert start.count("'--host','0.0.0.0'") == 2
    assert "Wait-GatewayAuthBoundary 30" in start
    assert "Wait-VoiceAuthBoundary 30" in start
    assert "inbound firewall rules and port forwarding closed" in start
