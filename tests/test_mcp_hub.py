from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from mcp.client.stdio import StdioServerParameters
from pydantic import ValidationError

import services.mcp_hub.hub as hub_module
import services.mcp_hub.launcher as launcher_module
import services.mcp_hub.runtime as runtime_module
import services.mcp_hub.cli as cli_module
import services.mcp_hub.audit as audit_module
import services.mcp_hub.config as config_module
from services.common import ROOT
from services.mcp_hub.audit import write_call_audit
from services.mcp_hub.config import (
    MCP_REGISTRY_PATH,
    McpRegistry,
    load_registry,
    qwen_server_view,
    render_qwen_settings,
    validate_installed_source,
)
from services.mcp_hub.hub import ManagedMcpHub, McpHubUnavailable
from services.mcp_hub.runtime import registry_snapshot


EXPECTED_DISCOVERY_HASHES = {
    "context7": {
        "resolve-library-id": "8e6eeef9cb886e6b08d64cba10de9b8de224406daf6a286c90ef1bd82f0ea732",
        "query-docs": "9c8375fc64a84241291a6af1906bf2a19552a6eb0b1f8c39b4f93a1c4b714c76",
    },
    "playwright": {
        "browser_navigate": "2165538e098634780eec628947d795a2619b4d2e3cef0e36d3084ac46abb94f7",
    },
    "local-diagnostics": {
        "mcp_registry_status": "e853c57581cf8be36ae5649946e94ca81a768ee9eb108d36b9a10c28f8eb7c7d",
    },
}


def _raw_registry() -> dict[str, Any]:
    return json.loads(MCP_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write_registry(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _local_registry(*, max_attempts: int = 1) -> McpRegistry:
    payload = _raw_registry()
    local = next(
        server for server in payload["servers"] if server["id"] == "local-diagnostics"
    )
    local["tools"][0]["max_attempts"] = max_attempts
    return McpRegistry.model_validate(
        {
            "schema_version": payload["schema_version"],
            "policy_version": payload["policy_version"],
            "servers": [local],
        }
    )


def _broken_server_registry() -> McpRegistry:
    payload = _raw_registry()
    local = next(
        server for server in payload["servers"] if server["id"] == "local-diagnostics"
    )
    broken = copy.deepcopy(local)
    broken["id"] = "broken-test"
    broken["display_name"] = "Broken Test MCP"
    broken["capabilities"] = ["synthetic_failure_isolation"]
    broken["consumers"] = ["mcp_hub"]
    broken["concurrency"]["resource_lock"] = "mcp:broken-test"
    return McpRegistry.model_validate(
        {
            "schema_version": payload["schema_version"],
            "policy_version": payload["policy_version"],
            "servers": [broken, local],
        }
    )


def _launcher_pids(server_id: str) -> set[int]:
    marker = f"--server-id {server_id}".casefold()
    launcher = "services\\mcp_hub\\launcher.py"
    found: set[int] = set()
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = " ".join(process.info["cmdline"] or []).casefold()
        except (psutil.Error, OSError):
            continue
        if launcher in command and marker in command:
            found.add(int(process.info["pid"]))
    return found


def _assert_no_new_launcher_processes(before: dict[str, set[int]]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = {
            server_id: _launcher_pids(server_id) - initial
            for server_id, initial in before.items()
        }
        if not any(remaining.values()):
            return
        time.sleep(0.05)
    assert not any(remaining.values()), f"orphan managed MCP launchers: {remaining}"


def test_registry_is_strict_and_rejects_unknown_fields_and_coercion(tmp_path):
    payload = _raw_registry()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_registry(_write_registry(tmp_path / "unknown.json", payload))

    payload = _raw_registry()
    payload["servers"][0]["enabled"] = 1
    with pytest.raises(ValidationError, match="bool_type"):
        load_registry(_write_registry(tmp_path / "coerced.json", payload))


def test_registry_rejects_duplicate_server_and_capability_ids(tmp_path):
    payload = _raw_registry()
    payload["servers"].append(copy.deepcopy(payload["servers"][0]))
    with pytest.raises(ValidationError, match="duplicate server ids"):
        load_registry(_write_registry(tmp_path / "duplicate-server.json", payload))

    payload = _raw_registry()
    duplicate = copy.deepcopy(payload["servers"][0])
    duplicate["id"] = "duplicate-capability"
    duplicate["concurrency"]["resource_lock"] = "mcp:duplicate-capability"
    payload["servers"].append(duplicate)
    with pytest.raises(ValidationError, match="duplicate capability ids"):
        load_registry(_write_registry(tmp_path / "duplicate-capability.json", payload))


def test_registry_rejects_mutable_versions_and_secret_shaped_fields(tmp_path):
    payload = _raw_registry()
    payload["servers"][0]["version"] = "latest"
    payload["servers"][0]["source"]["version"] = "latest"
    with pytest.raises(ValidationError, match="exact semantic version"):
        load_registry(_write_registry(tmp_path / "latest.json", payload))

    payload = _raw_registry()
    payload["servers"][0]["api_" + "token"] = "test-only-" + "secret"
    with pytest.raises(ValueError, match="secret-shaped registry field"):
        load_registry(_write_registry(tmp_path / "secret-field.json", payload))


def test_registry_rejects_secret_like_values_even_under_public_fields(tmp_path):
    payload = _raw_registry()
    payload["servers"][0]["display_name"] = (
        "Bear" + "er stage006" + "SecretValue123456789"
    )

    with pytest.raises(ValueError, match="secret-shaped registry value"):
        load_registry(_write_registry(tmp_path / "secret-value.json", payload))


@pytest.mark.parametrize(
    "secret",
    [
        "AK" + "IA" + ("A" * 16),
        "AI" + "za" + ("B" * 30),
        "x" + "oxb-" + ("C" * 20),
        "ey" + "J" + ("D" * 10) + "." + ("E" * 10) + "." + ("F" * 10),
        "post" + "gres://fixture-user:" + "Fixture-Pass-123@" + "localhost/db",
    ],
    ids=["aws", "google", "slack", "jwt", "connection-uri"],
)
def test_registry_uses_canonical_secret_detection_for_string_values(
    tmp_path, secret
):
    payload = _raw_registry()
    payload["servers"][0]["display_name"] = secret

    with pytest.raises(ValueError, match="secret-shaped registry value"):
        load_registry(_write_registry(tmp_path / "canonical-secret.json", payload))


def test_registry_secret_scanner_failure_is_closed(monkeypatch, tmp_path):
    payload = _raw_registry()
    payload["servers"][0]["display_name"] = "scanner-failure-registry-value"
    real_detect_secret = config_module.detect_secret

    def failing_scanner(value):
        if value == b"scanner-failure-registry-value":
            raise RuntimeError("synthetic scanner failure")
        return real_detect_secret(value)

    monkeypatch.setattr(config_module, "detect_secret", failing_scanner)
    with pytest.raises(ValueError, match="registry secret scanner failed closed"):
        load_registry(_write_registry(tmp_path / "scanner-failure.json", payload))


def test_registry_runtime_environment_names_remain_names_not_values(tmp_path):
    payload = _raw_registry()
    payload["servers"][0]["transport"]["runtime_environment_names"] = [
        "MCP_CONTEXT7_API_TOKEN"
    ]

    registry = load_registry(_write_registry(tmp_path / "environment-name.json", payload))
    assert registry.servers[0].transport.runtime_environment_names == [
        "MCP_CONTEXT7_API_TOKEN"
    ]


def test_registry_runtime_environment_name_cannot_hide_secret_value(tmp_path):
    payload = _raw_registry()
    payload["servers"][0]["transport"]["runtime_environment_names"] = [
        "AK" + "IA" + ("A" * 16)
    ]

    with pytest.raises(ValueError, match="secret-shaped registry value"):
        load_registry(_write_registry(tmp_path / "secret-environment-name.json", payload))


def test_external_egress_uses_canonical_secret_detection_without_false_positives():
    server = load_registry().server("context7")
    tool = next(tool for tool in server.tools if tool.name == "resolve-library-id")
    secret_values = [
        "AK" + "IA" + ("A" * 16),
        "AS" + "IA" + ("B" * 16),
        "x" + "oxb-" + ("C" * 20),
        "AI" + "za" + ("D" * 30),
        "123456:" + ("E" * 30),
        "ey" + "J" + ("F" * 10) + "." + ("G" * 10) + "." + ("H" * 10),
        "s" + "k_" + "live_" + ("I" * 16),
        "S" + "G." + ("J" * 16) + "." + ("K" * 16),
        "post" + "gres://fixture-user:" + "Fixture-Pass-123@" + "localhost/db",
        "s" + "k-" + ("L" * 20),
        "gh" + "p_" + ("M" * 30),
        "Bear" + "er " + ("N" * 20),
        "-----BEGIN " + "PRIVATE KEY-----",
        json.dumps({"pass" + "word": "S3riously-Private-Fixture-Value"}),
    ]

    for value in secret_values:
        with pytest.raises(hub_module.McpHubRejected, match="egress boundary"):
            hub_module._validate_arguments(
                server,
                tool,
                {"query": value, "libraryName": "FastAPI"},
            )

    for value in [
        "How should documentation describe a token parameter?",
        "Authorization: Bearer ${ACCESS_TOKEN}",
        "api_key=<your-api-key>",
        "Use os.environ to read credentials without embedding values.",
    ]:
        hub_module._validate_arguments(
            server,
            tool,
            {"query": value, "libraryName": "FastAPI"},
        )


@pytest.mark.parametrize(
    "module_name",
    ["services.mcp_hub.module_that_does_not_exist", "os"],
)
def test_project_source_missing_or_external_module_fails_closed(module_name):
    server = _local_registry().server("local-diagnostics")
    source = server.source.model_copy(update={"package": module_name})
    transport = server.transport.model_copy(update={"entrypoint": module_name})
    candidate = server.model_copy(update={"source": source, "transport": transport})

    with pytest.raises(ValueError):
        validate_installed_source(candidate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tool: tool.update(input_schema={"type": "not-a-json-schema-type"}),
        lambda tool: tool.update(output_schema={"type": "not-a-json-schema-type"}),
        lambda tool: tool.update(upstream_output_schema_sha256=None),
        lambda tool: tool.update(output_schema_version=None),
        lambda tool: tool.update(output_schema=None),
    ],
)
def test_registry_meta_validates_json_schemas_and_output_contract_parity(
    tmp_path, mutate
):
    payload = _raw_registry()
    tool = next(
        server for server in payload["servers"] if server["id"] == "local-diagnostics"
    )["tools"][0]
    mutate(tool)

    with pytest.raises((ValidationError, ValueError)):
        load_registry(_write_registry(tmp_path / "invalid-tool-schema.json", payload))


def test_generated_qwen_views_are_canonical_and_coding_stays_mcp_free():
    registry = load_registry()
    rendered = {
        profile: render_qwen_settings(profile, registry)
        for profile in ("qwen", "qwen-docs", "qwen-code")
    }

    assert set(rendered["qwen"]["mcpServers"]) == {"local-diagnostics"}
    assert set(rendered["qwen-docs"]["mcpServers"]) == {"context7"}
    assert rendered["qwen-code"]["mcpServers"] == {}
    assert json.loads((ROOT / "config/qwen-code/settings.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ] == {}

    consumers = {
        "qwen": "qwen_platform",
        "qwen-docs": "qwen_docs",
    }
    for profile, consumer in consumers.items():
        expected = {
            server.id: qwen_server_view(server)
            for server in registry.servers
            if server.enabled
            and server.configured_state == "on_demand"
            and consumer in server.consumers
        }
        assert rendered[profile]["mcpServers"] == expected
        assert "@latest" not in json.dumps(expected)

    for profile in ("qwen", "qwen-docs"):
        for server_id, view in rendered[profile]["mcpServers"].items():
            server = registry.server(server_id)
            assert view["timeout"] == (
                max(tool.call_timeout_seconds for tool in server.tools) * 1000
                + 2_000
            )
            assert view["discoveryTimeoutMs"] == (
                server.lifecycle.startup_timeout_seconds
                + server.lifecycle.readiness_timeout_seconds
            ) * 1000 + 2_000


def test_real_discovery_hashes_local_call_and_lifecycle_leave_no_orphan():
    registry = load_registry()
    before = {
        server_id: _launcher_pids(server_id)
        for server_id in EXPECTED_DISCOVERY_HASHES
    }
    initially_absent_artifacts = {
        path
        for server_id in EXPECTED_DISCOVERY_HASHES
        for path in (
            runtime_module.owner_path(server_id),
            runtime_module.lock_path(server_id),
        )
        if not path.exists()
    }

    async def exercise() -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
        managed = ManagedMcpHub(registry)
        hashes: dict[str, dict[str, str]] = {}
        for server_id in EXPECTED_DISCOVERY_HASHES:
            discovered = await managed.discover(
                server_id,
                request_id=f"pytest-discovery-{server_id}",
            )
            hashes[server_id] = {
                tool["name"]: tool["input_schema_sha256"] for tool in discovered
            }
        result = await managed.call(
            "local-diagnostics",
            "mcp_registry_status",
            {},
            request_id="pytest-local-call",
        )
        return hashes, result

    try:
        hashes, result = asyncio.run(exercise())
        assert hashes == EXPECTED_DISCOVERY_HASHES
        evidence = json.dumps(result, ensure_ascii=False)
        assert all(server.id in evidence for server in registry.servers)
    finally:
        _assert_no_new_launcher_processes(before)

    for path in initially_absent_artifacts:
        assert not path.exists(), f"managed MCP lifecycle left an artifact: {path}"


def test_broken_server_does_not_disable_local_diagnostics(monkeypatch, tmp_path):
    registry = _broken_server_registry()
    original_parameters = ManagedMcpHub._parameters

    def parameters(server):
        if server.id == "broken-test":
            return StdioServerParameters(
                command=sys.executable,
                args=["-c", "raise SystemExit(17)"],
            )
        return original_parameters(server)

    monkeypatch.setattr(ManagedMcpHub, "_parameters", staticmethod(parameters))
    monkeypatch.setattr(runtime_module, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hub_module, "write_call_audit", lambda **_event: None)

    async def exercise() -> list[dict[str, Any]]:
        managed = ManagedMcpHub(registry)
        with pytest.raises(McpHubUnavailable):
            await managed.discover("broken-test", request_id="pytest-broken")
        return await managed.discover(
            "local-diagnostics",
            request_id="pytest-after-broken",
        )

    discovered = asyncio.run(exercise())
    assert [tool["name"] for tool in discovered] == ["mcp_registry_status"]


def test_live_doctor_degrades_for_preexisting_unowned_managed_process_without_killing(
    monkeypatch
):
    registry = load_registry()
    candidate = {
        "pid": 4242,
        "create_time": 123.5,
        "server_id": "context7",
        "role": "launcher",
    }

    class FakeHub:
        def __init__(self, observed_registry):
            assert observed_registry is registry

        async def discover(self, server_id, **_kwargs):
            return [{"name": registry.server(server_id).tools[0].name}]

        async def call(self, server_id, _tool_name, _arguments, **_kwargs):
            if server_id == "context7":
                return {"text": "current lifespan documentation"}
            return {"servers": [server.id for server in registry.servers]}

        async def playwright_title_fixture(self, **_kwargs):
            return {"title": "Locestra MCP Fixture"}

    monkeypatch.setattr(cli_module, "load_registry", lambda: registry)
    monkeypatch.setattr(cli_module, "ManagedMcpHub", FakeHub)
    monkeypatch.setattr(cli_module, "owner_inventory", lambda _registry: [])
    monkeypatch.setattr(
        cli_module, "owned_process_identities", lambda _registry: set()
    )
    monkeypatch.setattr(
        cli_module, "_managed_process_inventory", lambda _registry: [candidate]
    )
    monkeypatch.setattr(
        cli_module,
        "stop_owned_servers",
        lambda _registry: (_ for _ in ()).throw(
            AssertionError("doctor must report, not kill, an unowned candidate")
        ),
    )

    report, healthy = asyncio.run(cli_module._live_doctor())

    assert not healthy
    assert report["status"] == "degraded"
    assert report["unowned_orphan_candidates"] == [candidate]
    assert report["unexpected_managed_processes"] == []
    assert report["lifecycle_cleanup"] == "ok"


class _FailingStdioContext:
    async def __aenter__(self):
        raise OSError("synthetic transport failure")

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _HangingStdioContext:
    async def __aenter__(self):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _GroupedFailureStdioContext:
    def __init__(self, error: BaseExceptionGroup):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _GuardHandle:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_error = close_error
        self.close_calls = 0

    def seek(self, _offset, whence=os.SEEK_SET):
        return 1 if whence == os.SEEK_END else 0

    def fileno(self):
        return 123

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise OSError("synthetic guard close failure")


def _patch_advisory_lock(monkeypatch, implementation) -> None:
    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", implementation)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", implementation)


class _FakeStdioContext:
    async def __aenter__(self):
        return None, None

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeSchemaSession:
    def __init__(self, discovered):
        self.discovered = discovered
        self.call_tool_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=self.discovered)

    async def call_tool(self, _name, _arguments):
        self.call_tool_count += 1
        raise AssertionError("call_tool must not run after schema validation fails")


class _BlockingRemoteErrorSession(_FakeSchemaSession):
    def __init__(self, discovered):
        super().__init__(discovered)
        self.entered_call = asyncio.Event()
        self.release_call = asyncio.Event()

    async def call_tool(self, _name, _arguments):
        self.call_tool_count += 1
        self.entered_call.set()
        await self.release_call.wait()
        return SimpleNamespace(isError=True)


class _ResultSession(_FakeSchemaSession):
    def __init__(self, discovered, result):
        super().__init__(discovered)
        self.result = result

    async def call_tool(self, _name, _arguments):
        self.call_tool_count += 1
        return self.result


def _isolate_hub_side_effects(monkeypatch, events: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    monkeypatch.setattr(hub_module, "circuit_open", lambda _server: False)
    monkeypatch.setattr(hub_module, "record_success", lambda _server: None)
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason_code: failures.append(reason_code),
    )
    monkeypatch.setattr(
        hub_module,
        "write_call_audit",
        lambda **event: events.append(event),
    )
    return failures


@pytest.mark.parametrize("failure_kind", ["duplicate", "schema_drift"])
def test_call_validates_discovery_in_same_session_before_call_tool(
    monkeypatch, failure_kind
):
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    tool = server.tools[0].model_copy(
        update={
            "upstream_input_schema_sha256": hub_module.canonical_schema_hash(schema),
            "upstream_output_schema_sha256": None,
        }
    )
    server = server.model_copy(update={"tools": [tool]})
    registry = registry.model_copy(update={"servers": [server]})
    valid = SimpleNamespace(
        name=tool.name,
        inputSchema=schema,
        outputSchema=None,
    )
    discovered = (
        [valid, SimpleNamespace(name=tool.name, inputSchema=schema, outputSchema=None)]
        if failure_kind == "duplicate"
        else [
            SimpleNamespace(
                name=tool.name,
                inputSchema={"type": "string"},
                outputSchema=None,
            )
        ]
    )
    session = _FakeSchemaSession(discovered)
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(hub_module, "stdio_client", lambda *_a, **_kw: _FakeStdioContext())
    monkeypatch.setattr(hub_module, "ClientSession", lambda *_a, **_kw: session)

    with pytest.raises(hub_module.McpSchemaMismatch):
        asyncio.run(
            ManagedMcpHub(registry).call(
                server.id,
                tool.name,
                {},
                request_id=f"pytest-{failure_kind}",
            )
        )

    assert session.call_tool_count == 0
    assert failures == ["schema_mismatch"]
    assert len(events) == 1
    assert events[0]["reason_code"] == "schema_mismatch"


def test_hub_validates_structured_output_before_success(monkeypatch):
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    output_schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    tool = server.tools[0].model_copy(
        update={
            "upstream_input_schema_sha256": hub_module.canonical_schema_hash(input_schema),
            "upstream_output_schema_sha256": hub_module.canonical_schema_hash(output_schema),
            "output_schema": output_schema,
        }
    )
    server = server.model_copy(update={"tools": [tool]})
    registry = registry.model_copy(update={"servers": [server]})
    session = _ResultSession(
        [
            SimpleNamespace(
                name=tool.name,
                inputSchema=input_schema,
                outputSchema=output_schema,
            )
        ],
        SimpleNamespace(isError=False, structuredContent={"wrong": True}),
    )
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(hub_module, "stdio_client", lambda *_a, **_kw: _FakeStdioContext())
    monkeypatch.setattr(hub_module, "ClientSession", lambda *_a, **_kw: session)

    with pytest.raises(hub_module.McpSchemaMismatch):
        asyncio.run(ManagedMcpHub(registry).call(server.id, tool.name, {}))

    assert session.call_tool_count == 1
    assert failures == ["schema_mismatch"]
    assert events[-1]["reason_code"] == "schema_mismatch"


def test_entry_fail_fast_rejections_are_audited_once_without_failure_budget(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        hub_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )
    registry = _local_registry()
    server = registry.server("local-diagnostics")

    disabled = server.model_copy(update={"enabled": False, "configured_state": "disabled"})
    disabled_registry = registry.model_copy(update={"servers": [disabled]})
    with pytest.raises(hub_module.McpHubDisabled):
        asyncio.run(
            ManagedMcpHub(disabled_registry).call(
                disabled.id, server.tools[0].name, {}, request_id="pytest-disabled"
            )
        )
    assert len(events) == 1
    assert events.pop()["reason_code"] == "disabled"

    monkeypatch.setattr(hub_module, "circuit_open", lambda _server: True)
    with pytest.raises(hub_module.McpHubCircuitOpen):
        asyncio.run(
            ManagedMcpHub(registry).call(
                server.id, server.tools[0].name, {}, request_id="pytest-circuit"
            )
        )
    assert len(events) == 1
    assert events.pop()["reason_code"] == "circuit_open"

    monkeypatch.setattr(hub_module, "circuit_open", lambda _server: False)
    with pytest.raises(hub_module.McpToolNotAllowlisted):
        asyncio.run(
            ManagedMcpHub(registry).call(
                server.id, "not_allowlisted", {}, request_id="pytest-not-allowlisted"
            )
        )
    assert len(events) == 1
    assert events.pop()["reason_code"] == "tool_not_allowlisted"

    with pytest.raises(hub_module.McpHubDisabled):
        asyncio.run(
            ManagedMcpHub(disabled_registry).discover(
                disabled.id, request_id="pytest-disabled-discovery"
            )
        )
    assert len(events) == 1
    discovery = events.pop()
    assert discovery["tool_name"] == "tools.list"
    assert discovery["reason_code"] == "disabled"
    assert discovery["status"] == "rejected"
    assert failures == []


def test_successful_discovery_does_not_reset_existing_failure_budget(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    tool = server.tools[0].model_copy(
        update={
            "upstream_input_schema_sha256": hub_module.canonical_schema_hash(schema),
            "upstream_output_schema_sha256": None,
        }
    )
    server = server.model_copy(update={"tools": [tool]})
    registry = registry.model_copy(update={"servers": [server]})
    session = _FakeSchemaSession(
        [SimpleNamespace(name=tool.name, inputSchema=schema, outputSchema=None)]
    )

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(hub_module, "stdio_client", lambda *_a, **_kw: _FakeStdioContext())
    monkeypatch.setattr(hub_module, "ClientSession", lambda *_a, **_kw: session)
    monkeypatch.setattr(hub_module, "write_call_audit", lambda **_event: None)
    runtime_module.record_failure(server, "transport_failure")
    before = runtime_module.read_status(server)

    discovered = asyncio.run(ManagedMcpHub(registry).discover(server.id))
    after = runtime_module.read_status(server)

    assert [item["name"] for item in discovered] == [tool.name]
    assert before["state"] == after["state"] == "degraded"
    assert before["consecutive_failures"] == after["consecutive_failures"] == 1


def test_operation_guard_serializes_callers_and_rechecks_circuit_before_stdio(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    lifecycle = server.lifecycle.model_copy(update={"circuit_failure_threshold": 2})
    tool = server.tools[0].model_copy(
        update={
            "upstream_input_schema_sha256": hub_module.canonical_schema_hash(schema),
            "upstream_output_schema_sha256": None,
        }
    )
    server = server.model_copy(update={"lifecycle": lifecycle, "tools": [tool]})
    registry = registry.model_copy(update={"servers": [server]})
    session = _BlockingRemoteErrorSession(
        [SimpleNamespace(name=tool.name, inputSchema=schema, outputSchema=None)]
    )
    events: list[dict[str, Any]] = []
    stdio_starts = 0

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    def stdio(*_args, **_kwargs):
        nonlocal stdio_starts
        stdio_starts += 1
        return _FakeStdioContext()

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(hub_module, "stdio_client", stdio)
    monkeypatch.setattr(hub_module, "ClientSession", lambda *_a, **_kw: session)
    monkeypatch.setattr(
        hub_module, "write_call_audit", lambda **event: events.append(event)
    )
    runtime_module.record_failure(server, "transport_failure")

    async def exercise():
        managed = ManagedMcpHub(registry)
        first = asyncio.create_task(
            managed.call(server.id, tool.name, {}, request_id="first")
        )
        await session.entered_call.wait()
        second = asyncio.create_task(
            managed.call(server.id, tool.name, {}, request_id="second")
        )
        await asyncio.sleep(0.1)
        assert stdio_starts == 1
        session.release_call.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    first_result, second_result = asyncio.run(exercise())

    assert isinstance(first_result, hub_module.McpRemoteToolError)
    assert isinstance(second_result, hub_module.McpHubCircuitOpen)
    assert session.call_tool_count == 1
    assert stdio_starts == 1
    assert runtime_module.read_status(server)["state"] == "circuit_open"
    assert [(event["request_id"], event["status"], event["reason_code"]) for event in events] == [
        ("first", "failed", "remote_tool_error"),
        ("second", "rejected", "circuit_open"),
    ]


def test_operation_guard_timeout_is_busy_without_failure_budget(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    held = runtime_module.try_acquire_operation_guard(server.id)
    assert held is not None
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        ManagedMcpHub,
        "_operation_gate_timeout_seconds",
        staticmethod(lambda _server, *, discovery: 0.05),
    )
    monkeypatch.setattr(
        hub_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )
    try:
        with pytest.raises(hub_module.McpHubBusy):
            asyncio.run(
                ManagedMcpHub(registry).call(
                    server.id, server.tools[0].name, {}, request_id="gate-busy"
                )
            )
    finally:
        held.release()

    assert events == [
        {
            "server_id": server.id,
            "tool_name": server.tools[0].name,
            "duration_ms": events[0]["duration_ms"],
            "status": "rejected",
            "attempt": 1,
            "reason_code": "busy",
            "request_id": "gate-busy",
            "task_id": None,
        }
    ]
    assert failures == []
    assert runtime_module.operation_guard_path(server.id).is_file()


@pytest.mark.parametrize("failure_stage", ["unlock", "close"])
def test_operation_guard_release_is_idempotent_and_never_poisons_process_lock(
    monkeypatch, failure_stage
):
    process_lock = threading.Lock()
    assert process_lock.acquire(blocking=False)
    handle = _GuardHandle(close_error=failure_stage == "close")

    def advisory_lock(*_args):
        if failure_stage == "unlock":
            raise OSError("synthetic guard unlock failure")

    _patch_advisory_lock(monkeypatch, advisory_lock)
    lease = runtime_module.OperationGuardLease(handle, process_lock)

    with pytest.raises(OSError, match=f"synthetic guard {failure_stage} failure"):
        lease.release()

    assert handle.close_calls == 1
    assert not process_lock.locked()
    lease.release()
    assert handle.close_calls == 1
    assert process_lock.acquire(blocking=False)
    process_lock.release()


def test_status_guard_close_failure_does_not_poison_later_status_lock(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    server_id = "status-close-failure"
    handles = iter([_GuardHandle(close_error=True), _GuardHandle()])
    monkeypatch.setattr(Path, "open", lambda _path, *_args, **_kwargs: next(handles))
    _patch_advisory_lock(monkeypatch, lambda *_args: None)

    with pytest.raises(OSError, match="synthetic guard close failure"):
        with runtime_module._status_guard(server_id):
            pass

    with runtime_module._status_guard(server_id):
        pass
    assert not runtime_module._status_process_lock(server_id).locked()


def test_failed_operation_acquisition_close_failure_does_not_poison_later_call(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    server_id = "operation-close-failure"
    handles = iter([_GuardHandle(close_error=True), _GuardHandle()])
    advisory_calls = 0

    def advisory_lock(*_args):
        nonlocal advisory_calls
        advisory_calls += 1
        if advisory_calls == 1:
            raise OSError("synthetic guard acquisition failure")

    monkeypatch.setattr(Path, "open", lambda _path, *_args, **_kwargs: next(handles))
    _patch_advisory_lock(monkeypatch, advisory_lock)

    with pytest.raises(OSError, match="synthetic guard close failure"):
        runtime_module.try_acquire_operation_guard(server_id)

    lease = runtime_module.try_acquire_operation_guard(server_id)
    assert lease is not None
    lease.release()
    assert not runtime_module._operation_process_lock(server_id).locked()


@pytest.mark.parametrize("operation", ["discover", "call"])
def test_hub_cancellation_survives_operation_gate_release_failure(
    monkeypatch, operation
):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    entered = asyncio.Event()
    process_lock = threading.Lock()
    assert process_lock.acquire(blocking=False)

    class FaultingLease:
        def __init__(self):
            self.released = False
            self.release_calls = 0

        def release(self):
            self.release_calls += 1
            if self.released:
                return
            self.released = True
            process_lock.release()
            raise OSError("private cleanup detail must not cross")

    lease = FaultingLease()

    async def acquire_gate(_self, _server, *, discovery):
        return lease

    async def hang_under_gate(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ManagedMcpHub, "_acquire_operation_gate", acquire_gate)
    if operation == "discover":
        monkeypatch.setattr(
            ManagedMcpHub, "_discover_under_gate", hang_under_gate
        )
    else:
        monkeypatch.setattr(ManagedMcpHub, "_call_under_gate", hang_under_gate)

    async def exercise() -> asyncio.CancelledError:
        managed = ManagedMcpHub(registry)
        if operation == "discover":
            coroutine = managed.discover(server.id)
        else:
            coroutine = managed.call(server.id, server.tools[0].name, {})
        task = asyncio.create_task(coroutine)
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as cancellation:
            return cancellation
        raise AssertionError("cancellation was swallowed")

    cancellation = asyncio.run(exercise())
    notes = getattr(cancellation, "__notes__", [])
    assert "MCP cancellation operation-gate cleanup failed (OSError)" in notes
    assert all("private cleanup detail" not in note for note in notes)
    assert lease.release_calls == 2
    assert process_lock.acquire(blocking=False)
    process_lock.release()


def test_operation_gate_acquire_cancellation_survives_release_failure(monkeypatch):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    entered = threading.Event()
    allow_return = threading.Event()
    process_lock = threading.Lock()
    assert process_lock.acquire(blocking=False)

    class FaultingLease:
        def release(self):
            process_lock.release()
            raise OSError("private acquire cleanup detail must not cross")

    def acquire_after_cancel(_server_id):
        entered.set()
        assert allow_return.wait(timeout=2)
        return FaultingLease()

    monkeypatch.setattr(
        hub_module, "try_acquire_operation_guard", acquire_after_cancel
    )

    async def exercise() -> asyncio.CancelledError:
        task = asyncio.create_task(
            ManagedMcpHub(registry)._acquire_operation_gate(
                server, discovery=False
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        allow_return.set()
        try:
            await task
        except asyncio.CancelledError as cancellation:
            return cancellation
        raise AssertionError("cancellation was swallowed")

    cancellation = asyncio.run(exercise())
    notes = getattr(cancellation, "__notes__", [])
    assert "MCP cancellation operation-gate cleanup failed (OSError)" in notes
    assert all("private acquire cleanup detail" not in note for note in notes)
    assert process_lock.acquire(blocking=False)
    process_lock.release()


def test_cancellation_while_queued_for_operation_gate_is_audited_once(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    held = runtime_module.try_acquire_operation_guard(server.id)
    assert held is not None
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        hub_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )

    async def exercise():
        task = asyncio.create_task(
            ManagedMcpHub(registry).call(
                server.id, server.tools[0].name, {}, request_id="queued-cancel"
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        held.release()

    assert len(events) == 1
    assert events[0]["status"] == "cancelled"
    assert events[0]["reason_code"] == "cancelled"
    assert failures == []
    reacquired = runtime_module.try_acquire_operation_guard(server.id)
    assert reacquired is not None
    reacquired.release()


def test_cancellation_while_waiting_for_runtime_slot_is_audited_once(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    failures: list[str] = []

    async def wait_forever(_self, _server, *, seconds=2.0):
        await asyncio.Event().wait()

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", wait_forever)
    monkeypatch.setattr(
        hub_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )

    async def exercise():
        task = asyncio.create_task(
            ManagedMcpHub(registry).call(
                server.id, server.tools[0].name, {}, request_id="slot-cancel"
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert len(events) == 1
    assert events[0]["status"] == "cancelled"
    assert events[0]["reason_code"] == "cancelled"
    assert failures == []


def test_idempotent_transport_failure_has_exactly_one_bounded_retry(monkeypatch):
    attempts = 0
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)

    def failing_stdio(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return _FailingStdioContext()

    monkeypatch.setattr(hub_module, "stdio_client", failing_stdio)

    with pytest.raises(McpHubUnavailable):
        asyncio.run(
            ManagedMcpHub(_local_registry(max_attempts=2)).call(
                "local-diagnostics",
                "mcp_registry_status",
                {},
                request_id="pytest-retry",
            )
        )

    assert attempts == 2
    assert [event["attempt"] for event in events] == [1, 2]
    assert failures == ["transport_failure"]


def test_internal_timeout_is_bounded_and_recorded(monkeypatch):
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(hub_module, "stdio_client", lambda *_a, **_kw: _HangingStdioContext())
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: real_timeout(0.05))

    started = time.monotonic()
    with pytest.raises(McpHubUnavailable):
        asyncio.run(
            ManagedMcpHub(_local_registry()).call(
                "local-diagnostics",
                "mcp_registry_status",
                {},
                request_id="pytest-timeout",
            )
        )

    assert time.monotonic() - started < 1
    assert events[-1]["status"] == "timed_out"
    assert events[-1]["reason_code"] == "timeout"
    assert failures == ["timeout"]


def test_cancel_is_propagated_and_audited_without_retry(monkeypatch):
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)
    attempts = 0

    def hanging_stdio(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return _HangingStdioContext()

    monkeypatch.setattr(hub_module, "stdio_client", hanging_stdio)

    async def cancel_call() -> None:
        task = asyncio.create_task(
            ManagedMcpHub(_local_registry(max_attempts=2)).call(
                "local-diagnostics",
                "mcp_registry_status",
                {},
                request_id="pytest-cancel",
            )
        )
        for _attempt in range(50):
            if attempts:
                break
            await asyncio.sleep(0.01)
        assert attempts == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())
    assert attempts == 1
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["reason_code"] == "cancelled"
    assert failures == []


@pytest.mark.parametrize("operation", ["discover", "call"])
def test_cancellation_base_exception_group_is_reaped_audited_and_propagated(
    monkeypatch, operation
):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)
    reaped: list[str] = []
    grouped = BaseExceptionGroup(
        "cancelled transport",
        [asyncio.CancelledError()],
    )

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(
        hub_module,
        "stdio_client",
        lambda *_args, **_kwargs: _GroupedFailureStdioContext(grouped),
    )
    monkeypatch.setattr(
        hub_module,
        "reap_stale_server",
        lambda _registry, observed: reaped.append(observed.id),
    )

    async def exercise():
        managed = ManagedMcpHub(registry)
        if operation == "discover":
            return await managed._discover_under_gate(
                server,
                started=time.monotonic(),
                request_id="group-cancel",
                task_id="group-cancel",
            )
        return await managed._call_under_gate(
            server,
            server.tools[0].name,
            {},
            validation_started=time.monotonic(),
            request_id="group-cancel",
            task_id="group-cancel",
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())

    assert reaped == [server.id]
    assert failures == []
    assert len(events) == 1
    assert events[0]["status"] == "cancelled"
    assert events[0]["reason_code"] == "cancelled"


@pytest.mark.parametrize("operation", ["discover", "call"])
def test_unrelated_base_exception_group_is_not_normalized_or_swallowed(
    monkeypatch, operation
):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)
    grouped = BaseExceptionGroup("control flow", [KeyboardInterrupt("stop")])

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(
        hub_module,
        "stdio_client",
        lambda *_args, **_kwargs: _GroupedFailureStdioContext(grouped),
    )
    monkeypatch.setattr(hub_module, "reap_stale_server", lambda *_args: None)

    async def exercise():
        managed = ManagedMcpHub(registry)
        if operation == "discover":
            return await managed._discover_under_gate(
                server,
                started=time.monotonic(),
                request_id="group-control",
                task_id="group-control",
            )
        return await managed._call_under_gate(
            server,
            server.tools[0].name,
            {},
            validation_started=time.monotonic(),
            request_id="group-control",
            task_id="group-control",
        )

    with pytest.raises(BaseExceptionGroup) as raised:
        asyncio.run(exercise())

    assert raised.value is grouped
    assert events == []
    assert failures == []


@pytest.mark.parametrize("failure_shape", ["plain", "grouped"])
def test_cancellation_semantics_survive_audit_persistence_failure(
    monkeypatch, failure_shape
):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    failures: list[str] = []
    reaped: list[str] = []
    if failure_shape == "plain":
        stdio_context = _HangingStdioContext()
    else:
        stdio_context = _GroupedFailureStdioContext(
            BaseExceptionGroup("cancelled transport", [asyncio.CancelledError()])
        )

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(
        hub_module, "stdio_client", lambda *_args, **_kwargs: stdio_context
    )
    monkeypatch.setattr(
        hub_module,
        "write_call_audit",
        lambda **_event: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )
    monkeypatch.setattr(
        hub_module,
        "reap_stale_server",
        lambda _registry, observed: reaped.append(observed.id),
    )

    async def exercise() -> asyncio.CancelledError:
        task = asyncio.create_task(
            ManagedMcpHub(registry)._call_under_gate(
                server,
                server.tools[0].name,
                {},
                validation_started=time.monotonic(),
                request_id="audit-cancel",
                task_id="audit-cancel",
            )
        )
        if failure_shape == "plain":
            await asyncio.sleep(0.02)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError as cancellation:
            return cancellation
        raise AssertionError("cancellation was swallowed")

    cancellation = asyncio.run(exercise())
    assert "MCP cancellation audit persistence failed" in getattr(
        cancellation, "__notes__", []
    )
    assert reaped == [server.id]
    assert failures == []


@pytest.mark.parametrize("operation", ["discover", "call"])
@pytest.mark.parametrize("reap_error_type", [OSError, TimeoutError])
def test_cancellation_survives_best_effort_reap_failure(
    monkeypatch, operation, reap_error_type
):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    failures = _isolate_hub_side_effects(monkeypatch, events)
    reap_attempts: list[str] = []
    entered = asyncio.Event()

    class CancellationContext:
        async def __aenter__(self):
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    def failing_reap(_registry, observed):
        reap_attempts.append(observed.id)
        raise reap_error_type("synthetic reap failure")

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(
        hub_module, "stdio_client", lambda *_args, **_kwargs: CancellationContext()
    )
    monkeypatch.setattr(hub_module, "reap_stale_server", failing_reap)

    async def exercise() -> asyncio.CancelledError:
        managed = ManagedMcpHub(registry)
        if operation == "discover":
            coroutine = managed._discover_under_gate(
                server,
                started=time.monotonic(),
                request_id="reap-cancel",
                task_id="reap-cancel",
            )
        else:
            coroutine = managed._call_under_gate(
                server,
                server.tools[0].name,
                {},
                validation_started=time.monotonic(),
                request_id="reap-cancel",
                task_id="reap-cancel",
            )
        task = asyncio.create_task(coroutine)
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as cancellation:
            return cancellation
        raise AssertionError("cancellation was swallowed")

    cancellation = asyncio.run(exercise())
    assert reap_attempts == [server.id]
    assert failures == []
    assert len(events) == 1
    assert events[0]["status"] == "cancelled"
    assert events[0]["reason_code"] == "cancelled"
    assert (
        f"MCP cancellation stale-owner cleanup failed ({reap_error_type.__name__})"
        in getattr(cancellation, "__notes__", [])
    )


def test_retry_sleep_cancellation_survives_audit_persistence_failure(monkeypatch):
    registry = _local_registry(max_attempts=2)
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    failures: list[str] = []

    async def available_slot(_self, _server, *, seconds=2.0):
        return None

    def audit(**event):
        if event["status"] == "cancelled":
            raise OSError("audit unavailable")
        events.append(event)

    monkeypatch.setattr(ManagedMcpHub, "_wait_for_slot", available_slot)
    monkeypatch.setattr(
        hub_module, "stdio_client", lambda *_args, **_kwargs: _FailingStdioContext()
    )
    monkeypatch.setattr(hub_module, "write_call_audit", audit)
    monkeypatch.setattr(hub_module, "reap_stale_server", lambda *_args: None)
    monkeypatch.setattr(
        hub_module,
        "record_failure",
        lambda _server, reason: failures.append(reason),
    )

    async def exercise() -> asyncio.CancelledError:
        task = asyncio.create_task(
            ManagedMcpHub(registry)._call_under_gate(
                server,
                server.tools[0].name,
                {},
                validation_started=time.monotonic(),
                request_id="retry-audit-cancel",
                task_id="retry-audit-cancel",
            )
        )
        while not events:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as cancellation:
            return cancellation
        raise AssertionError("retry cancellation was swallowed")

    cancellation = asyncio.run(exercise())
    assert [event["status"] for event in events] == ["failed"]
    assert "MCP cancellation audit persistence failed" in getattr(
        cancellation, "__notes__", []
    )
    assert failures == []


def test_disabled_and_degraded_states_are_visible_in_registry_snapshot(monkeypatch, tmp_path):
    payload = _raw_registry()
    local = next(
        server for server in payload["servers"] if server["id"] == "local-diagnostics"
    )
    local["enabled"] = False
    local["configured_state"] = "disabled"
    registry = McpRegistry.model_validate(payload)
    monkeypatch.setattr(runtime_module, "STATUS_DIR", tmp_path / "status")

    context7 = registry.server("context7")
    runtime_module.record_failure(context7, "synthetic_failure")
    by_id = {
        server["id"]: server for server in registry_snapshot(registry)["servers"]
    }

    assert by_id["context7"]["runtime_state"] == "degraded"
    assert by_id["context7"]["last_reason_code"] == "synthetic_failure"
    assert by_id["local-diagnostics"]["runtime_state"] == "disabled"


def test_circuit_breaker_is_per_server_and_cooldown_resets_failure_budget(monkeypatch, tmp_path):
    registry = load_registry()
    context7 = registry.server("context7")
    lifecycle = context7.lifecycle.model_copy(
        update={"circuit_failure_threshold": 2, "circuit_cooldown_seconds": 1}
    )
    context7 = context7.model_copy(update={"lifecycle": lifecycle})
    monkeypatch.setattr(runtime_module, "STATUS_DIR", tmp_path / "status")

    runtime_module.record_failure(context7, "transport_failure")
    assert runtime_module.read_status(context7)["state"] == "degraded"
    runtime_module.record_failure(context7, "transport_failure")
    opened = runtime_module.read_status(context7)
    assert opened["state"] == "circuit_open"

    opened["circuit_open_until_epoch"] = 0.0
    runtime_module.write_status(context7, opened)
    cooled = runtime_module.read_status(context7)
    assert cooled["state"] == "on_demand"
    assert cooled["consecutive_failures"] == 0
    assert registry_snapshot(registry)["servers"][1]["runtime_state"] != "circuit_open"


def test_concurrent_status_failures_do_not_lose_rmw_updates(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    server = _local_registry().server("local-diagnostics")
    lifecycle = server.lifecycle.model_copy(update={"circuit_failure_threshold": 10})
    server = server.model_copy(update={"lifecycle": lifecycle})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _index: runtime_module.record_failure(
                    server, "concurrent_transport_failure"
                ),
                range(20),
            )
        )

    status = runtime_module.read_status(server)
    assert status["consecutive_failures"] == 20
    assert status["state"] == "circuit_open"
    assert runtime_module.status_guard_path(server.id).is_file()


def test_audit_is_metadata_only_and_redacts_secret_like_identifiers(tmp_path):
    audit_path = tmp_path / "mcp-calls.jsonl"
    secret = "s" + "k-stage006" + "SecretValue123456789"
    write_call_audit(
        server_id="context7",
        tool_name="query-docs",
        duration_ms=12,
        status="rejected",
        attempt=1,
        reason_code="policy_rejected",
        request_id=secret,
        task_id=("Bear" + f"er-{secret}"),
        path=audit_path,
    )

    raw = audit_path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert secret not in raw
    assert set(event) == {
        "schema_version",
        "event",
        "timestamp",
        "server_id",
        "tool",
        "duration_ms",
        "status",
        "attempt",
        "reason_code",
        "request_id",
        "task_id",
    }
    assert not ({"arguments", "payload", "result", "command", "environment"} & set(event))


@pytest.mark.parametrize(
    "secret",
    [
        "A" + "KIA" + ("0" * 16),
        "AI" + "za" + ("A" * 30),
        "xo" + "xb-" + ("A" * 20),
    ],
    ids=["aws", "google", "slack"],
)
def test_audit_canonical_scanner_redacts_safe_format_credentials(tmp_path, secret):
    audit_path = tmp_path / "mcp-calls.jsonl"
    write_call_audit(
        server_id="context7",
        tool_name="query-docs",
        duration_ms=1,
        status="rejected",
        attempt=1,
        reason_code="policy_rejected",
        request_id=secret,
        path=audit_path,
    )

    raw = audit_path.read_bytes()
    event = json.loads(raw)
    assert secret.encode() not in raw
    assert event["request_id"] == "redacted"
    assert len(raw) <= 2_048


def test_audit_secret_scanner_failure_redacts_identifier(monkeypatch, tmp_path):
    audit_path = tmp_path / "mcp-calls.jsonl"
    real_detect_secret = audit_module.detect_secret

    def failing_scanner(payload):
        if payload == b"scanner-failure-id":
            raise RuntimeError("synthetic scanner failure")
        return real_detect_secret(payload)

    monkeypatch.setattr(audit_module, "detect_secret", failing_scanner)
    write_call_audit(
        server_id="context7",
        tool_name="query-docs",
        duration_ms=1,
        status="failed",
        attempt=1,
        reason_code="transport_failure",
        request_id="scanner-failure-id",
        path=audit_path,
    )

    raw = audit_path.read_bytes()
    event = json.loads(raw)
    assert b"scanner-failure-id" not in raw
    assert event["request_id"] == "redacted"
    assert len(raw) <= 2_048


def test_audit_append_retries_short_writes_without_truncating_json(monkeypatch, tmp_path):
    audit_path = tmp_path / "mcp-calls.jsonl"
    real_write = audit_module.os.write
    write_sizes: list[int] = []

    def short_write(descriptor, value):
        requested = len(value)
        bounded = max(1, requested // 2)
        written = real_write(descriptor, value[:bounded])
        write_sizes.append(written)
        return written

    monkeypatch.setattr(audit_module.os, "write", short_write)
    write_call_audit(
        server_id="context7",
        tool_name="query-docs",
        duration_ms=1,
        status="ok",
        attempt=1,
        reason_code="ok",
        request_id="short-write",
        path=audit_path,
    )

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["request_id"] == "short-write"
    assert len(write_sizes) > 1


def test_audit_append_zero_write_fails_closed(monkeypatch, tmp_path):
    audit_path = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setattr(audit_module.os, "write", lambda _descriptor, _value: 0)

    with pytest.raises(OSError, match="made no progress"):
        write_call_audit(
            server_id="context7",
            tool_name="query-docs",
            duration_ms=1,
            status="failed",
            attempt=1,
            reason_code="transport_failure",
            request_id="zero-write",
            path=audit_path,
        )

    assert not audit_path.read_bytes()


def test_managed_stdio_policy_filters_tools_and_rejects_non_loopback_playwright(monkeypatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        launcher_module,
        "write_call_audit",
        lambda **event: events.append(event),
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("playwright"))

    list_request = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
    forward, response = policy.client_line(list_request)
    assert forward == list_request
    assert response is None
    upstream = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "browser_navigate",
                            "inputSchema": {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "type": "object",
                                "properties": {
                                    "url": {
                                        "type": "string",
                                        "description": "The URL to navigate to",
                                    }
                                },
                                "required": ["url"],
                                "additionalProperties": False,
                            },
                        },
                        {"name": "browser_evaluate"},
                    ]
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    filtered = json.loads(policy.server_line(upstream))
    assert [tool["name"] for tool in filtered["result"]["tools"]] == [
        "browser_navigate"
    ]
    events.clear()

    unsafe_request = (
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
        b'{"name":"browser_navigate","arguments":{"url":"https://example.com"}}}\n'
    )
    forward, response = policy.client_line(unsafe_request)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32602
    assert events == [
        {
            "server_id": "playwright",
            "tool_name": "browser_navigate",
            "duration_ms": 0,
            "status": "rejected",
            "attempt": 1,
            "reason_code": "policy_rejected",
            "request_id": "mcp-session",
        }
    ]


def test_valid_direct_tools_list_audits_ready_without_resetting_failure_budget(monkeypatch):
    events: list[dict[str, Any]] = []
    successes: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module,
        "record_success",
        lambda server: successes.append(server.id),
    )
    response = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "browser_navigate",
                            "inputSchema": {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "type": "object",
                                "properties": {
                                    "url": {
                                        "type": "string",
                                        "description": "The URL to navigate to",
                                    }
                                },
                                "required": ["url"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    request = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'

    direct = launcher_module._ProtocolPolicy(load_registry().server("playwright"))
    assert direct.client_line(request) == (request, None)
    direct.server_line(response)
    assert direct.ready_event.is_set()
    assert successes == []
    assert len(events) == 1
    assert events[0]["tool_name"] == "tools.list"
    assert events[0]["status"] == "ok"

    hub_client = launcher_module._ProtocolPolicy(
        load_registry().server("playwright"), emit_lifecycle_events=False
    )
    assert hub_client.client_line(request) == (request, None)
    hub_client.server_line(response)
    assert hub_client.ready_event.is_set()
    assert successes == []
    assert len(events) == 1


@pytest.mark.parametrize("operation", ["list", "call"])
def test_direct_audit_oserror_returns_bounded_error_closes_state_and_terminates_guard(
    monkeypatch, operation
):
    server = load_registry().server("local-diagnostics")
    tool = server.tools[0]
    policy = launcher_module._ProtocolPolicy(server)
    if operation == "list":
        request = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": 71,
                "method": "tools/list",
                "params": {},
            }
        )
        upstream = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": 71,
                "result": {
                    "tools": [
                        {
                            "name": tool.name,
                            "inputSchema": tool.input_schema,
                            "outputSchema": tool.output_schema,
                        }
                    ]
                },
            }
        )
    else:
        policy.ready_event.set()
        request = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": 72,
                "method": "tools/call",
                "params": {"name": tool.name, "arguments": {}},
            }
        )
        upstream = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": 72,
                "result": {
                    "content": [],
                    "isError": False,
                    "structuredContent": {
                        "schema_version": "1.0",
                        "policy_version": "2026-07-17.1",
                        "servers": [],
                    },
                },
            }
        )
    assert policy.client_line(request) == (request, None)

    secret_marker = "private-audit-payload-must-not-cross"

    def fail_audit(**_event):
        raise OSError(secret_marker)

    class Guard:
        def __init__(self):
            self.terminated = False

        def terminate(self, *, include_parent):
            assert include_parent is True
            self.terminated = True

    guard = Guard()
    output = io.BytesIO()
    monkeypatch.setattr(launcher_module, "write_call_audit", fail_audit)
    pump = threading.Thread(
        target=launcher_module._pump_output,
        args=(io.BytesIO(upstream), output, policy, threading.Lock(), guard),
    )
    pump.start()
    pump.join(timeout=0.5)

    assert not pump.is_alive()
    response = json.loads(output.getvalue())
    assert response["error"] == {
        "code": -32603,
        "message": "Managed MCP audit persistence failed.",
    }
    assert secret_marker not in output.getvalue().decode("utf-8")
    assert policy.audit_failure_event.is_set()
    assert policy.schema_failure_event.is_set()
    assert policy.list_requests == {}
    assert policy.pending == {}
    assert policy.cancelled_pending == set()
    assert guard.terminated


def test_gateway_runtime_correlation_populates_request_and_task_audit_fields(monkeypatch):
    events: list[dict[str, Any]] = []
    server = load_registry().server("local-diagnostics")
    policy = launcher_module._ProtocolPolicy(
        server,
        correlation_id="gateway-docs-456",
        task_id="gateway-docs-456",
    )
    policy.ready_event.set()
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    request = (
        b'{"jsonrpc":"2.0","id":73,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    response = (
        b'{"jsonrpc":"2.0","id":73,"result":{"content":[],"isError":false,'
        b'"structuredContent":{"schema_version":"1.0",'
        b'"policy_version":"2026-07-17.1","servers":[]}}}\n'
    )

    assert policy.client_line(request) == (request, None)
    assert policy.server_line(response) == response
    assert events[-1]["request_id"] == "gateway-docs-456"
    assert events[-1]["task_id"] == "gateway-docs-456"
    assert "arguments" not in events[-1]
    assert "result" not in events[-1]


def test_invalid_runtime_correlation_fails_before_owner_or_child_launch(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    monkeypatch.setenv("LOCESTRA_MCP_CORRELATION_ID", "../unsafe request")
    monkeypatch.setattr(launcher_module, "load_registry", lambda: registry)
    monkeypatch.setattr(
        launcher_module,
        "validate_installed_source",
        lambda _server: (_ for _ in ()).throw(
            AssertionError("invalid correlation must fail before source or child launch")
        ),
    )

    assert launcher_module.run_managed_server(server.id, hub_client=False) == 64
    assert not runtime_module.owner_path(server.id).exists()
    assert not runtime_module.lock_path(server.id).exists()


def test_hub_client_ignores_invalid_ambient_direct_consumer_correlation(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    monkeypatch.setenv("LOCESTRA_MCP_CORRELATION_ID", "../unsafe request")
    monkeypatch.setattr(launcher_module, "circuit_open", lambda _server: True)

    assert (
        launcher_module._run_registered_server(
            registry,
            server,
            playwright_fixture_url=None,
            hub_client=True,
        )
        == 69
    )


def test_tools_list_json_rpc_error_fails_readiness_immediately(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module,
        "record_failure",
        lambda server, reason: failures.append((server.id, reason)),
    )
    server = load_registry().server("context7")
    request = b'{"jsonrpc":"2.0","id":31,"method":"tools/list","params":{}}\n'
    upstream_error = (
        b'{"jsonrpc":"2.0","id":31,"error":'
        b'{"code":-32603,"message":"upstream discovery failed"}}\n'
    )

    direct = launcher_module._ProtocolPolicy(server)
    assert direct.client_line(request) == (request, None)
    assert direct.server_line(upstream_error) == upstream_error
    assert direct.schema_failure_event.is_set()
    assert not direct.ready_event.is_set()
    assert direct.list_requests == {}
    assert failures == [("context7", "remote_tool_error")]
    assert events[-1]["tool_name"] == "tools.list"
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason_code"] == "remote_tool_error"

    hub_client = launcher_module._ProtocolPolicy(
        server, emit_lifecycle_events=False
    )
    assert hub_client.client_line(request) == (request, None)
    assert hub_client.server_line(upstream_error) == upstream_error
    assert hub_client.schema_failure_event.is_set()
    assert failures == [("context7", "remote_tool_error")]
    assert len(events) == 1


def test_malformed_tools_list_result_is_normalized_and_fails_fast(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("playwright"))
    request = b'{"jsonrpc":"2.0","id":32,"method":"tools/list","params":{}}\n'
    malformed = (
        b'{"jsonrpc":"2.0","id":32,"result":{"tools":"not-an-array"}}\n'
    )

    assert policy.client_line(request) == (request, None)
    response = json.loads(policy.server_line(malformed))
    assert response == {
        "jsonrpc": "2.0",
        "id": 32,
        "error": {
            "code": -32603,
            "message": "Managed MCP returned an invalid tools/list result.",
        },
    }
    assert policy.schema_failure_event.is_set()
    assert not policy.ready_event.is_set()
    assert policy.list_requests == {}
    assert failures == ["schema_mismatch"]
    assert events[-1]["tool_name"] == "tools.list"
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason_code"] == "schema_mismatch"


def test_protocol_state_machine_rejects_prelist_unknown_and_malformed_messages(monkeypatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("local-diagnostics"))
    call = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    forward, response = policy.client_line(call)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32002

    resources = b'{"jsonrpc":"2.0","id":2,"method":"resources/read","params":{}}\n'
    forward, response = policy.client_line(resources)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32601

    forward, response = policy.client_line(b'{not-json\n')
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32700

    initialize = b'{"jsonrpc":"2.0","id":3,"method":"initialize","params":{}}\n'
    assert policy.client_line(initialize) == (initialize, None)
    reverse_request = b'{"jsonrpc":"2.0","id":99,"method":"roots/list","params":{}}\n'
    assert policy.server_line(reverse_request) is None
    initialize_response = b'{"jsonrpc":"2.0","id":3,"result":{}}\n'
    assert policy.server_line(initialize_response) == initialize_response
    assert [event["reason_code"] for event in events] == [
        "schema_not_ready",
        "method_not_allowed",
        "invalid_request",
        "invalid_response",
    ]


def test_protocol_reconstructs_allowed_request_envelopes(monkeypatch):
    monkeypatch.setattr(launcher_module, "write_call_audit", lambda **_event: None)
    server = load_registry().server("context7")

    list_policy = launcher_module._ProtocolPolicy(server)
    raw_list = (
        b'{ "method": "tools/list", "params": {}, "id": 41, '
        b'"jsonrpc": "2.0" }\n'
    )
    forward, response = list_policy.client_line(raw_list)
    assert response is None
    assert forward == (
        b'{"jsonrpc":"2.0","id":41,"method":"tools/list","params":{}}\n'
    )
    assert forward != raw_list

    initialize_policy = launcher_module._ProtocolPolicy(server)
    initialize = launcher_module._ProtocolPolicy._json_line(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "roots": {"listChanged": True},
                    "experimental": {"opaque": "must-not-forward"},
                },
                "clientInfo": {
                    "name": "stage006-test",
                    "title": "Stage 006 test",
                    "version": "1.0",
                },
            },
        }
    )
    forward, response = initialize_policy.client_line(initialize)
    assert response is None
    assert json.loads(forward) == {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "stage006-test",
                "title": "Stage 006 test",
                "version": "1.0",
            },
        },
    }


def test_protocol_rejects_extra_top_level_and_params_fields(monkeypatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    server = load_registry().server("context7")
    policy = launcher_module._ProtocolPolicy(server)

    extra_top_level = launcher_module._ProtocolPolicy._json_line(
        {
            "jsonrpc": "2.0",
            "id": 51,
            "method": "tools/list",
            "params": {},
            "sideChannel": "must-not-forward",
        }
    )
    forward, response = policy.client_line(extra_top_level)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32600

    extra_list_param = launcher_module._ProtocolPolicy._json_line(
        {
            "jsonrpc": "2.0",
            "id": 52,
            "method": "tools/list",
            "params": {"metadata": {"opaque": "must-not-forward"}},
        }
    )
    forward, response = policy.client_line(extra_list_param)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32600

    policy.ready_event.set()
    extra_call_param = launcher_module._ProtocolPolicy._json_line(
        {
            "jsonrpc": "2.0",
            "id": 53,
            "method": "tools/call",
            "params": {
                "name": "resolve-library-id",
                "arguments": {"query": "FastAPI lifespan", "libraryName": "FastAPI"},
                "metadata": {"opaque": "must-not-forward"},
            },
        }
    )
    forward, response = policy.client_line(extra_call_param)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32600
    assert policy.list_requests == {}
    assert policy.pending == {}
    assert [event["reason_code"] for event in events] == [
        "invalid_request",
        "invalid_request",
        "invalid_request",
    ]


def test_protocol_allows_only_exact_safe_progress_token_meta(monkeypatch):
    monkeypatch.setattr(launcher_module, "write_call_audit", lambda **_event: None)
    server = load_registry().server("context7")

    for request_id, progress_token in (
        (60, 7),
        (61, "qwen-progress-7"),
    ):
        accepted = launcher_module._ProtocolPolicy(server)
        accepted.ready_event.set()
        request = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "resolve-library-id",
                    "arguments": {
                        "query": "FastAPI lifespan",
                        "libraryName": "FastAPI",
                    },
                    "_meta": {"progressToken": progress_token},
                },
            }
        )
        forward, response = accepted.client_line(request)
        assert response is None
        assert json.loads(forward)["params"]["_meta"] == {
            "progressToken": progress_token
        }

    invalid_metadata = [
        {"progressToken": 8, "opaque": "must-not-forward"},
        {"progressToken": None},
        {"progressToken": True},
        {},
        {"progressToken": "AK" + "IA" + ("P" * 16)},
    ]
    for index, metadata in enumerate(invalid_metadata, start=62):
        policy = launcher_module._ProtocolPolicy(server)
        policy.ready_event.set()
        candidate = launcher_module._ProtocolPolicy._json_line(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {
                    "name": "resolve-library-id",
                    "arguments": {
                        "query": "FastAPI lifespan",
                        "libraryName": "FastAPI",
                    },
                    "_meta": metadata,
                },
            }
        )
        forward, response = policy.client_line(candidate)
        assert forward is None
        assert json.loads(response)["error"]["code"] == -32600
        assert policy.pending == {}


def test_cancelled_direct_call_remains_pending_until_response_or_timeout(monkeypatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("local-diagnostics"))
    policy.ready_event.set()
    first = (
        b'{"jsonrpc":"2.0","id":11,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    assert policy.client_line(first) == (first, None)
    cancelled = (
        b'{"jsonrpc":"2.0","method":"notifications/cancelled",'
        b'"params":{"requestId":11,"reason":"test"}}\n'
    )
    assert policy.client_line(cancelled) == (cancelled, None)
    assert policy.pending

    second = first.replace(b'"id":11', b'"id":12')
    forward, response = policy.client_line(second)
    assert forward is None
    assert json.loads(response)["error"]["code"] == -32001

    late = (
        b'{"jsonrpc":"2.0","id":11,"result":'
        b'{"content":[],"isError":false,"structuredContent":{}}}\n'
    )
    assert policy.server_line(late) is None
    assert policy.pending == {}
    assert [event["status"] for event in events] == ["cancelled", "rejected"]


def test_direct_structured_output_schema_mismatch_fails_closed(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("local-diagnostics"))
    policy.ready_event.set()
    request = (
        b'{"jsonrpc":"2.0","id":21,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    assert policy.client_line(request) == (request, None)
    invalid = (
        b'{"jsonrpc":"2.0","id":21,"result":'
        b'{"content":[],"isError":false,"structuredContent":{"unexpected":true}}}\n'
    )
    filtered = json.loads(policy.server_line(invalid))

    assert filtered["error"]["code"] == -32603
    assert failures == ["schema_mismatch"]
    assert events[-1]["reason_code"] == "schema_mismatch"


def test_direct_tool_result_allows_omitted_is_error(monkeypatch):
    events: list[dict[str, Any]] = []
    successes: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module,
        "record_success",
        lambda server: successes.append(server.id),
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("local-diagnostics"))
    policy.ready_event.set()
    request = (
        b'{"jsonrpc":"2.0","id":22,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    assert policy.client_line(request) == (request, None)
    valid = (
        b'{"jsonrpc":"2.0","id":22,"result":{"content":[],'
        b'"structuredContent":{"schema_version":"1.0",'
        b'"policy_version":"2026-07-17.1","servers":[]}}}\n'
    )

    assert policy.server_line(valid) == valid
    assert successes == ["local-diagnostics"]
    assert events[-1]["status"] == "ok"
    assert events[-1]["reason_code"] == "ok"


def test_direct_tool_result_rejects_non_boolean_is_error(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("local-diagnostics"))
    policy.ready_event.set()
    request = (
        b'{"jsonrpc":"2.0","id":23,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    assert policy.client_line(request) == (request, None)
    invalid = (
        b'{"jsonrpc":"2.0","id":23,"result":{"content":[],"isError":"false",'
        b'"structuredContent":{"schema_version":"1.0",'
        b'"policy_version":"2026-07-17.1","servers":[]}}}\n'
    )

    filtered = json.loads(policy.server_line(invalid))
    assert filtered["error"]["code"] == -32603
    assert failures == ["schema_mismatch"]
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason_code"] == "schema_mismatch"


def _runtime_record(server, nonce: str, *, running: bool) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "server_id": server.id,
        "root_identity": runtime_module.root_identity(),
        "nonce_sha256": nonce,
        "owner_pid": os.getpid(),
        "owner_create_time": psutil.Process().create_time(),
        "parent_pid": os.getppid(),
        "parent_create_time": psutil.Process(os.getppid()).create_time(),
        "state": "acquiring",
        "started_at": "2026-07-17T00:00:00+00:00",
    }
    if running:
        record.update(
            state="running",
            child_pid=os.getpid(),
            child_create_time=psutil.Process().create_time(),
            child_command_sha256="a" * 64,
        )
    return record


def _isolate_runtime_paths(monkeypatch, tmp_path: Path) -> None:
    owners = tmp_path / "owners"
    locks = tmp_path / "locks"
    status = tmp_path / "status"
    monkeypatch.setattr(runtime_module, "OWNER_DIR", owners)
    monkeypatch.setattr(runtime_module, "LOCK_DIR", locks)
    monkeypatch.setattr(runtime_module, "STATUS_DIR", status)
    monkeypatch.setattr(launcher_module, "OWNER_DIR", owners)
    monkeypatch.setattr(launcher_module, "LOCK_DIR", locks)


def test_generation_cas_never_deletes_a_foreign_nonce(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    server = _local_registry().server("local-diagnostics")
    nonce = "a" * 64
    assert runtime_module.create_runtime_lock(
        server, _runtime_record(server, nonce, running=False)
    )

    assert not runtime_module.remove_runtime_record(server.id, "b" * 64)
    assert runtime_module.lock_path(server.id).is_file()
    assert runtime_module.remove_runtime_record(server.id, nonce)
    assert not runtime_module.lock_path(server.id).exists()


def test_stop_retains_ownership_when_an_exact_process_survives(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    nonce = "c" * 64
    lock = _runtime_record(server, nonce, running=False)
    owner = _runtime_record(server, nonce, running=True)
    runtime_module.LOCK_DIR.mkdir(parents=True)
    runtime_module.OWNER_DIR.mkdir(parents=True)
    runtime_module.lock_path(server.id).write_text(json.dumps(lock), encoding="utf-8")
    runtime_module.owner_path(server.id).write_text(json.dumps(owner), encoding="utf-8")
    sentinel = object()
    observed_timeouts: list[float] = []
    monkeypatch.setattr(runtime_module, "_owned_targets", lambda *_args: [sentinel])

    def survives(_targets, *, shutdown_timeout_seconds):
        observed_timeouts.append(shutdown_timeout_seconds)
        return False

    monkeypatch.setattr(runtime_module, "_terminate_owned_processes", survives)
    result = runtime_module.stop_owned_servers(registry)

    assert result == {
        "stopped": [],
        "stale_cleaned": [],
        "refused": [server.id],
    }
    assert observed_timeouts == [server.lifecycle.shutdown_timeout_seconds]
    assert runtime_module.lock_path(server.id).is_file()
    assert runtime_module.owner_path(server.id).is_file()


def test_shutdown_sequence_uses_configured_budget_and_second_wait(monkeypatch):
    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.alive = True
            self.actions: list[str] = []

        def terminate(self):
            self.actions.append("terminate")

        def kill(self):
            self.actions.append("kill")

    process = FakeProcess()
    waits: list[float] = []
    monkeypatch.setattr(runtime_module, "_identity", lambda _process: (4242, 1.0))
    monkeypatch.setattr(
        runtime_module,
        "_identity_alive",
        lambda _identity: process if process.alive else None,
    )

    def wait_procs(_processes, *, timeout):
        waits.append(timeout)
        if len(waits) == 2:
            process.alive = False
        return [], [] if not process.alive else [process]

    monkeypatch.setattr(runtime_module.psutil, "wait_procs", wait_procs)
    assert runtime_module._terminate_owned_processes(
        [process], shutdown_timeout_seconds=7
    )
    assert process.actions == ["terminate", "kill"]
    assert waits == pytest.approx([4.2, 2.8])


def test_launcher_error_after_acquisition_cleans_lock_and_session_without_poisoning(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    neutral_base = tmp_path / "neutral"
    transport = server.transport.model_copy(
        update={"neutral_workspace": "run/mcp/workspaces/local-diagnostics"}
    )
    server = server.model_copy(update={"transport": transport})
    registry = registry.model_copy(update={"servers": [server]})
    sessions: list[Path] = []
    failures: list[str] = []

    monkeypatch.setattr(launcher_module, "load_registry", lambda: registry)
    monkeypatch.setattr(launcher_module, "validate_installed_source", lambda _server: None)
    monkeypatch.setattr(launcher_module, "circuit_open", lambda _server: False)
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )

    def create_session(_server, nonce):
        neutral_base.mkdir(parents=True, exist_ok=True)
        session = neutral_base / f"session-{nonce[:16]}"
        session.mkdir()
        (session / ".playwright-mcp").mkdir()
        (session / ".playwright-mcp" / "page-1.yml").write_text(
            "ephemeral", encoding="utf-8"
        )
        sessions.append(session)
        return neutral_base, session

    monkeypatch.setattr(launcher_module, "_session_workspace", create_session)
    monkeypatch.setattr(
        launcher_module,
        "resolved_child_command",
        lambda _server: (_ for _ in ()).throw(OSError("synthetic setup failure")),
    )

    assert launcher_module.run_managed_server(server.id) == 70
    assert failures == []
    assert neutral_base.is_dir()
    assert sessions and not sessions[0].exists()
    assert not runtime_module.lock_path(server.id).exists()
    assert not runtime_module.owner_path(server.id).exists()


def test_hub_client_rechecks_circuit_after_runtime_lock_acquisition(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    checks = iter((False, True))
    failures: list[str] = []
    monkeypatch.setattr(launcher_module, "load_registry", lambda: registry)
    monkeypatch.setattr(launcher_module, "validate_installed_source", lambda _server: None)
    monkeypatch.setattr(launcher_module, "circuit_open", lambda _server: next(checks))
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )

    assert launcher_module.run_managed_server(server.id, hub_client=True) == 69
    assert failures == []
    assert not runtime_module.lock_path(server.id).exists()
    assert not runtime_module.owner_path(server.id).exists()


def test_busy_acquisition_does_not_poison_circuit(monkeypatch):
    registry = _local_registry()
    server = registry.server("local-diagnostics")
    failures: list[str] = []
    clock = iter((0.0, 3.0))
    monkeypatch.setattr(launcher_module, "load_registry", lambda: registry)
    monkeypatch.setattr(launcher_module, "validate_installed_source", lambda _server: None)
    monkeypatch.setattr(launcher_module, "circuit_open", lambda _server: False)
    monkeypatch.setattr(
        launcher_module,
        "acquire_operation_guard",
        lambda *_args, **_kwargs: SimpleNamespace(release=lambda: None),
    )
    monkeypatch.setattr(launcher_module, "create_runtime_lock", lambda *_args: False)
    monkeypatch.setattr(
        launcher_module,
        "owner_inventory",
        lambda _registry: [{"server_id": server.id, "state": "owned_acquiring"}],
    )
    monkeypatch.setattr(launcher_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )

    assert launcher_module.run_managed_server(server.id) == 75
    assert failures == []


def test_runtime_environment_is_exact_allowlist(monkeypatch):
    server = _local_registry().server("local-diagnostics")
    transport = server.transport.model_copy(
        update={"runtime_environment_names": ["MCP_TEST_TOKEN"]}
    )
    server = server.model_copy(update={"transport": transport})
    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-only-value")
    monkeypatch.setenv("MCP_UNLISTED_VALUE", "must-not-cross-boundary")

    environment = launcher_module._child_environment(server)
    assert "MCP_TEST_TOKEN" in environment
    assert "MCP_UNLISTED_VALUE" not in environment


def test_child_stderr_is_drained_without_forwarding_secret(capsys):
    launcher_module._drain(
        io.BytesIO(b"s" + b"k-child" + b"SecretValue123456789\n")
    )
    captured = capsys.readouterr()
    assert "childSecretValue" not in captured.out
    assert "childSecretValue" not in captured.err


def test_direct_schema_drift_fails_closed_and_records_failure(monkeypatch):
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )
    policy = launcher_module._ProtocolPolicy(load_registry().server("playwright"))
    policy.client_line(b'{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n')
    drifted = (
        b'{"jsonrpc":"2.0","id":9,"result":{"tools":['
        b'{"name":"browser_navigate","inputSchema":{"type":"string"}}]}}\n'
    )

    response = json.loads(policy.server_line(drifted))
    assert response["result"]["tools"] == []
    assert failures == ["schema_mismatch"]
    assert events[-1]["reason_code"] == "schema_mismatch"
    assert policy.schema_failure_event.is_set()


def test_repeated_direct_call_timeouts_open_only_affected_server_circuit(
    monkeypatch, tmp_path
):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = load_registry()
    server = registry.server("local-diagnostics")
    monkeypatch.setattr(launcher_module, "write_call_audit", lambda **_event: None)
    request = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )

    for _attempt in range(server.lifecycle.circuit_failure_threshold):
        policy = launcher_module._ProtocolPolicy(server)
        policy.ready_event.set()
        forward, response = policy.client_line(request)
        assert forward == request and response is None
        _tool, started, timeout_seconds, _lease = next(iter(policy.pending.values()))
        assert policy.expire_pending(now=started + timeout_seconds + 0.1)

    assert runtime_module.read_status(server)["state"] == "circuit_open"
    assert runtime_module.read_status(registry.server("context7"))["state"] == "on_demand"


def test_direct_call_fails_fast_after_remote_errors_open_circuit(monkeypatch, tmp_path):
    _isolate_runtime_paths(monkeypatch, tmp_path)
    registry = load_registry()
    server = registry.server("local-diagnostics")
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    policy = launcher_module._ProtocolPolicy(server)
    policy.ready_event.set()

    for request_id in range(1, server.lifecycle.circuit_failure_threshold + 1):
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "mcp_registry_status", "arguments": {}},
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        forward, response = policy.client_line(request)
        assert forward == request and response is None
        policy.server_line(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "remote failure"},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )

    assert runtime_module.read_status(server)["state"] == "circuit_open"
    fourth = (
        b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":'
        b'{"name":"mcp_registry_status","arguments":{}}}\n'
    )
    forward, response = policy.client_line(fourth)
    rejected = json.loads(response)

    assert forward is None
    assert rejected == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {
            "code": -32000,
            "message": "Managed MCP server circuit is open.",
        },
    }
    assert policy.pending == {}
    assert events[-1]["status"] == "rejected"
    assert events[-1]["reason_code"] == "circuit_open"
    assert runtime_module.read_status(registry.server("context7"))["state"] == "on_demand"


def test_direct_readiness_watchdog_records_timeout_and_terminates_child(monkeypatch):
    server = _local_registry().server("local-diagnostics")
    lifecycle = server.lifecycle.model_copy(
        update={"startup_timeout_seconds": 0, "readiness_timeout_seconds": 0}
    )
    server = server.model_copy(update={"lifecycle": lifecycle})
    events: list[dict[str, Any]] = []
    failures: list[str] = []

    class Guard:
        def __init__(self):
            self.terminated = False

        def terminate(self, *, include_parent):
            assert include_parent is True
            self.terminated = True

    guard = Guard()
    policy = launcher_module._ProtocolPolicy(server)
    stop = threading.Event()
    lifecycle_failure = threading.Event()
    monkeypatch.setattr(
        launcher_module, "write_call_audit", lambda **event: events.append(event)
    )
    monkeypatch.setattr(
        launcher_module, "record_failure", lambda _server, reason: failures.append(reason)
    )

    launcher_module._watch_direct_lifecycle(
        policy, guard, stop, lifecycle_failure
    )
    assert guard.terminated
    assert lifecycle_failure.is_set()
    assert failures == ["timeout"]
    assert events[-1]["status"] == "timed_out"


@pytest.mark.parametrize("failure_point", ["readiness", "pending"])
@pytest.mark.parametrize("failure_source", ["audit", "status"])
def test_direct_watchdog_metadata_oserror_still_terminates_lifecycle(
    monkeypatch, failure_point, failure_source
):
    server = _local_registry().server("local-diagnostics")
    lifecycle = server.lifecycle.model_copy(
        update={"startup_timeout_seconds": 0, "readiness_timeout_seconds": 0}
    )
    server = server.model_copy(update={"lifecycle": lifecycle})

    class Guard:
        def __init__(self):
            self.terminated = False

        def terminate(self, *, include_parent):
            assert include_parent is True
            self.terminated = True

    policy = launcher_module._ProtocolPolicy(server)
    if failure_point == "pending":
        policy.ready_event.set()
        request = (
            b'{"jsonrpc":"2.0","id":81,"method":"tools/call","params":'
            b'{"name":"mcp_registry_status","arguments":{}}}\n'
        )
        assert policy.client_line(request) == (request, None)
        with policy.lock:
            tool, started, timeout_seconds, lease = policy.pending["81"]
            policy.pending["81"] = (
                tool,
                started - timeout_seconds - 1,
                timeout_seconds,
                lease,
            )

    if failure_source == "audit":
        monkeypatch.setattr(
            launcher_module,
            "write_call_audit",
            lambda **_event: (_ for _ in ()).throw(OSError("audit unavailable")),
        )
    else:
        monkeypatch.setattr(
            launcher_module, "write_call_audit", lambda **_event: None
        )
        monkeypatch.setattr(
            launcher_module,
            "record_failure",
            lambda *_args: (_ for _ in ()).throw(OSError("status unavailable")),
        )
    guard = Guard()
    stop = threading.Event()
    lifecycle_failure = threading.Event()
    watcher = threading.Thread(
        target=launcher_module._watch_direct_lifecycle,
        args=(policy, guard, stop, lifecycle_failure),
    )
    watcher.start()
    watcher.join(timeout=0.5)

    assert not watcher.is_alive()
    assert lifecycle_failure.is_set()
    assert policy.audit_failure_event.is_set() is (failure_source == "audit")
    assert guard.terminated
