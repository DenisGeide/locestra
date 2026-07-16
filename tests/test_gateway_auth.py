import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from services.gateway import app as gateway


def _request(path: str, authorization: str | None = None) -> Request:
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
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
    )


def test_openai_boundary_requires_generated_bearer_and_scopes_trust(monkeypatch):
    monkeypatch.setattr(
        gateway, "GATEWAY_BEARER_CREDENTIAL", "synthetic-local-gateway-key"
    )

    async def invoke(authorization: str | None):
        observed = []

        async def next_handler(_request):
            observed.append(gateway.TRUSTED_GATEWAY_REQUEST.get())
            return JSONResponse({"ok": True})

        response = await gateway.authenticate_openai_boundary(
            _request("/v1/models", authorization), next_handler
        )
        return response, observed

    missing, missing_observed = asyncio.run(invoke(None))
    wrong, wrong_observed = asyncio.run(invoke("Bearer wrong"))
    accepted, accepted_observed = asyncio.run(
        invoke("Bearer synthetic-local-gateway-key")
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert not missing_observed and not wrong_observed
    assert accepted.status_code == 200
    assert accepted_observed == [True]
    assert gateway.TRUSTED_GATEWAY_REQUEST.get() is False


def test_non_openai_health_boundary_remains_local_and_untrusted(monkeypatch):
    monkeypatch.setattr(
        gateway, "GATEWAY_BEARER_CREDENTIAL", "synthetic-local-gateway-key"
    )

    async def invoke():
        observed = []

        async def next_handler(_request):
            observed.append(gateway.TRUSTED_GATEWAY_REQUEST.get())
            return JSONResponse({"ok": True})

        response = await gateway.authenticate_openai_boundary(
            _request("/health/live"), next_handler
        )
        return response, observed

    response, observed = asyncio.run(invoke())
    assert response.status_code == 200
    assert observed == [False]
