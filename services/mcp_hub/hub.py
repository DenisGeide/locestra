from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from services.knowledge.privacy import detect_secret
from services.mcp_hub.audit import write_call_audit
from services.mcp_hub.config import McpRegistry, ServerSpec, ToolSpec, launcher_command, load_registry
from services.mcp_hub.runtime import (
    OperationGuardLease,
    circuit_open,
    owner_inventory,
    reap_stale_server,
    record_failure,
    record_success,
    try_acquire_operation_guard,
)


_MAX_ARGUMENT_BYTES = 16_384


def _fixture_handler(path: str, title: str) -> type[BaseHTTPRequestHandler]:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>"
        + title
        + "</title></head><body>managed fixture</body></html>"
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; connect-src 'none'; form-action 'none'; frame-src 'none'; img-src 'none'; script-src 'none'; style-src 'none'",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


class McpHubError(RuntimeError):
    reason_code = "hub_error"


class McpHubRejected(McpHubError):
    reason_code = "policy_rejected"


class McpHubUnavailable(McpHubError):
    reason_code = "server_unavailable"


class McpHubDisabled(McpHubUnavailable):
    reason_code = "disabled"


class McpHubCircuitOpen(McpHubUnavailable):
    reason_code = "circuit_open"


class McpHubTimeout(McpHubUnavailable):
    reason_code = "timeout"


class McpHubBusy(McpHubUnavailable):
    reason_code = "busy"


class McpSchemaMismatch(McpHubError):
    reason_code = "schema_mismatch"


class McpRemoteToolError(McpHubError):
    reason_code = "remote_tool_error"


class McpToolNotAllowlisted(McpHubRejected):
    reason_code = "tool_not_allowlisted"


def canonical_schema_hash(schema: dict[str, Any]) -> str:
    encoded = json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_discovered_tools(
    server: ServerSpec, discovered: list[Any]
) -> list[dict[str, Any]]:
    """Fail closed on duplicate, missing, or drifted canonical tool schemas."""

    names = [getattr(tool, "name", None) for tool in discovered]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise McpSchemaMismatch("MCP discovery returned duplicate or invalid tool names")
    by_name = {tool.name: tool for tool in discovered}
    selected: list[dict[str, Any]] = []
    for expected in server.tools:
        actual = by_name.get(expected.name)
        if actual is None:
            raise McpSchemaMismatch("registered MCP tool was not discovered exactly once")
        input_schema = getattr(actual, "inputSchema", None)
        output_schema = getattr(actual, "outputSchema", None)
        if not isinstance(input_schema, dict):
            raise McpSchemaMismatch("MCP input schema is missing or invalid")
        actual_input_hash = canonical_schema_hash(input_schema)
        if (
            expected.upstream_input_schema_sha256
            and actual_input_hash != expected.upstream_input_schema_sha256
        ):
            raise McpSchemaMismatch("MCP input schema changed from the evaluated version")
        actual_output_hash = (
            canonical_schema_hash(output_schema)
            if isinstance(output_schema, dict)
            else None
        )
        if (
            expected.upstream_output_schema_sha256
            and actual_output_hash != expected.upstream_output_schema_sha256
        ):
            raise McpSchemaMismatch("MCP output schema changed from the evaluated version")
        selected.append(
            {
                "name": actual.name,
                "input_schema_sha256": actual_input_hash,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "output_schema_sha256": actual_output_hash,
            }
        )
    return selected


def _contains_secret(value: Any) -> bool:
    """Apply the canonical privacy scanner to both structure and string payloads.

    The serialized pass detects credential-shaped keys and ordinary token
    formats.  Scanning each string again is deliberate: a tool argument may
    itself contain encoded JSON, an assignment, or a URL-encoded credential,
    which must not become opaque merely because it is nested in a JSON string.
    """

    if isinstance(value, str):
        return detect_secret(value.encode("utf-8", errors="strict")) is not None
    try:
        serialized = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return True
    if detect_secret(serialized) is not None:
        return True
    if isinstance(value, dict):
        return any(_contains_secret(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(child) for child in value)
    return False


def _validate_arguments(
    server: ServerSpec,
    tool: ToolSpec,
    arguments: dict[str, Any],
    *,
    playwright_fixture_url: str | None = None,
) -> None:
    if not isinstance(arguments, dict):
        raise McpHubRejected("MCP tool arguments must be an object")
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        raise McpHubRejected("MCP tool arguments exceed the policy bound")
    try:
        validator = validator_for(tool.input_schema)
        validator.check_schema(tool.input_schema)
        validator(tool.input_schema).validate(arguments)
    except JsonSchemaValidationError as exc:
        raise McpHubRejected("MCP tool arguments do not match the registry schema") from exc
    if server.boundary.data_egress != "none" and _contains_secret(arguments):
        raise McpHubRejected("secret-shaped data cannot cross an MCP egress boundary")
    if server.id == "context7":
        if any(not isinstance(value, str) or len(value) > 2_000 for value in arguments.values()):
            raise McpHubRejected("Context7 arguments must be bounded public strings")
    if server.id == "playwright" and tool.name == "browser_navigate":
        parsed = urlsplit(str(arguments.get("url", "")))
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.port < 1_024
            or parsed.port > 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise McpHubRejected("Playwright MCP is limited to bounded loopback UI fixtures")
        if playwright_fixture_url is None or arguments["url"] != playwright_fixture_url:
            raise McpHubRejected("Playwright MCP requires a Hub-owned immutable fixture")


def _validate_structured_output(tool: ToolSpec, structured_content: Any) -> None:
    if tool.output_schema is None:
        return
    try:
        validator = validator_for(tool.output_schema)
        validator.check_schema(tool.output_schema)
        validator(tool.output_schema).validate(structured_content)
    except JsonSchemaValidationError as exc:
        raise McpSchemaMismatch(
            "MCP structured output does not match the registered schema"
        ) from exc


def _reason_for_exception(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, McpHubError):
        return exc.reason_code
    return "transport_failure"


def _contains_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_cancellation(nested) for nested in exc.exceptions)
    return False


def _audit_cancellation(
    cancellation: asyncio.CancelledError,
    **event: Any,
) -> None:
    """Cancellation must survive even when its metadata-only audit sink is unavailable."""

    try:
        write_call_audit(**event)
    except (OSError, TypeError, ValueError):
        cancellation.add_note("MCP cancellation audit persistence failed")


def _reap_after_cancellation(
    cancellation: asyncio.CancelledError,
    registry: McpRegistry,
    server: ServerSpec,
) -> None:
    """Best-effort cleanup must never replace the caller's cancellation."""

    try:
        reap_stale_server(registry, server)
    except Exception as exc:
        cancellation.add_note(
            f"MCP cancellation stale-owner cleanup failed ({type(exc).__name__})"
        )


def _release_operation_lease_after_cancellation(
    cancellation: asyncio.CancelledError,
    lease: OperationGuardLease,
) -> None:
    """Operation-gate cleanup must not replace cooperative cancellation."""

    try:
        lease.release()
    except Exception as exc:
        cancellation.add_note(
            f"MCP cancellation operation-gate cleanup failed ({type(exc).__name__})"
        )


class ManagedMcpHub:
    def __init__(self, registry: McpRegistry | None = None) -> None:
        self.registry = registry or load_registry()
        self._owned_playwright_fixtures: set[str] = set()

    @staticmethod
    def _audit_entry_rejection(
        *,
        server_id: str,
        tool_name: str,
        started: float,
        reason_code: str,
        request_id: str | None,
        task_id: str | None,
    ) -> None:
        write_call_audit(
            server_id=server_id,
            tool_name=tool_name,
            duration_ms=int((time.monotonic() - started) * 1_000),
            status="rejected",
            attempt=1,
            reason_code=reason_code,
            request_id=request_id,
            task_id=task_id,
        )

    @staticmethod
    def _operation_gate_timeout_seconds(server: ServerSpec, *, discovery: bool) -> float:
        return 2.0

    async def _acquire_operation_gate(
        self, server: ServerSpec, *, discovery: bool
    ) -> OperationGuardLease:
        deadline = time.monotonic() + self._operation_gate_timeout_seconds(
            server, discovery=discovery
        )
        while True:
            attempt = asyncio.create_task(
                asyncio.to_thread(try_acquire_operation_guard, server.id)
            )
            try:
                lease = await asyncio.shield(attempt)
            except asyncio.CancelledError as cancellation:
                lease = await attempt
                if lease is not None:
                    _release_operation_lease_after_cancellation(cancellation, lease)
                raise
            if lease is not None:
                return lease
            if time.monotonic() >= deadline:
                raise McpHubBusy("MCP operation gate reached its bounded wait")
            await asyncio.sleep(0.05)

    def _enabled_server(self, server_id: str) -> ServerSpec:
        try:
            server = self.registry.server(server_id)
        except KeyError as exc:
            raise McpHubRejected("MCP server is not registered") from exc
        if not server.enabled or server.configured_state == "disabled":
            raise McpHubDisabled("MCP server is disabled")
        if circuit_open(server):
            raise McpHubCircuitOpen("MCP server circuit is open")
        return server

    def _ownership_state(self, server: ServerSpec) -> str | None:
        observed = next(
            (
                item
                for item in owner_inventory(self.registry)
                if item["server_id"] == server.id
            ),
            None,
        )
        return observed["state"] if observed is not None else None

    async def _wait_for_slot(self, server: ServerSpec, *, seconds: float = 2.0) -> None:
        deadline = time.monotonic() + seconds
        while True:
            state = self._ownership_state(server)
            if state is None:
                return
            if state in {"stale_lock", "stale_owner_record"}:
                result = reap_stale_server(self.registry, server)
                if result["refused"]:
                    raise McpHubUnavailable("MCP stale ownership could not be verified")
                continue
            if state not in {"owned_acquiring", "owned_running"}:
                raise McpHubUnavailable("MCP ownership evidence is invalid")
            if time.monotonic() >= deadline:
                raise McpHubBusy("MCP server has reached its configured concurrency bound")
            await asyncio.sleep(0.05)

    @staticmethod
    def _parameters(
        server: ServerSpec,
        *,
        playwright_fixture_url: str | None = None,
    ) -> StdioServerParameters:
        command, args, cwd = launcher_command(server)
        args = [*args, "--hub-client"]
        if playwright_fixture_url is not None:
            args = [*args, "--playwright-fixture-url", playwright_fixture_url]
        return StdioServerParameters(command=command, args=args, cwd=cwd)

    async def discover(
        self,
        server_id: str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        try:
            server = self._enabled_server(server_id)
        except McpHubError as exc:
            self._audit_entry_rejection(
                server_id=server_id,
                tool_name="tools.list",
                started=started,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        try:
            operation_lease = await self._acquire_operation_gate(server, discovery=True)
        except asyncio.CancelledError as cancellation:
            _audit_cancellation(
                cancellation,
                server_id=server.id,
                tool_name="tools.list",
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="cancelled",
                attempt=1,
                reason_code="cancelled",
                request_id=request_id,
                task_id=task_id,
            )
            raise
        except McpHubBusy as exc:
            self._audit_entry_rejection(
                server_id=server.id,
                tool_name="tools.list",
                started=started,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        try:
            try:
                server = self._enabled_server(server_id)
            except McpHubError as exc:
                self._audit_entry_rejection(
                    server_id=server_id,
                    tool_name="tools.list",
                    started=started,
                    reason_code=exc.reason_code,
                    request_id=request_id,
                    task_id=task_id,
                )
                raise
            return await self._discover_under_gate(
                server,
                started=started,
                request_id=request_id,
                task_id=task_id,
            )
        except asyncio.CancelledError as cancellation:
            _release_operation_lease_after_cancellation(
                cancellation, operation_lease
            )
            raise
        finally:
            operation_lease.release()

    async def _discover_under_gate(
        self,
        server: ServerSpec,
        *,
        started: float,
        request_id: str | None,
        task_id: str | None,
    ) -> list[dict[str, Any]]:
        try:
            await self._wait_for_slot(server)
            timeout_seconds = server.lifecycle.startup_timeout_seconds + server.lifecycle.readiness_timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(self._parameters(server), errlog=errlog) as (read, write):
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(
                                seconds=server.lifecycle.readiness_timeout_seconds
                            ),
                        ) as session:
                            await session.initialize()
                            discovered = (await session.list_tools()).tools
                            selected = _validate_discovered_tools(server, discovered)
            write_call_audit(
                server_id=server.id,
                tool_name="tools.list",
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="ok",
                attempt=1,
                reason_code="ok",
                request_id=request_id,
                task_id=task_id,
            )
            return selected
        except asyncio.CancelledError as cancellation:
            _reap_after_cancellation(cancellation, self.registry, server)
            _audit_cancellation(
                cancellation,
                server_id=server.id,
                tool_name="tools.list",
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="cancelled",
                attempt=1,
                reason_code="cancelled",
                request_id=request_id,
                task_id=task_id,
            )
            raise
        except Exception as exc:
            task = asyncio.current_task()
            if _contains_cancellation(exc) or (task is not None and task.cancelling()):
                cancellation = asyncio.CancelledError()
                _reap_after_cancellation(cancellation, self.registry, server)
                _audit_cancellation(
                    cancellation,
                    server_id=server.id,
                    tool_name="tools.list",
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="cancelled",
                    attempt=1,
                    reason_code="cancelled",
                    request_id=request_id,
                    task_id=task_id,
                )
                raise cancellation from exc
            reap_stale_server(self.registry, server)
            reason = _reason_for_exception(exc)
            if reason == "transport_failure" and circuit_open(server):
                reason = "circuit_open"
            if reason in {"busy", "circuit_open"}:
                write_call_audit(
                    server_id=server.id,
                    tool_name="tools.list",
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="rejected",
                    attempt=1,
                    reason_code=reason,
                    request_id=request_id,
                    task_id=task_id,
                )
                if reason == "circuit_open":
                    raise McpHubCircuitOpen("MCP server circuit opened during discovery") from exc
                raise exc
            record_failure(server, reason)
            write_call_audit(
                server_id=server.id,
                tool_name="tools.list",
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="timed_out" if reason == "timeout" else "failed",
                attempt=1,
                reason_code=reason,
                request_id=request_id,
                task_id=task_id,
            )
            if isinstance(exc, McpHubError):
                raise
            raise McpHubUnavailable("MCP discovery failed within its bounded lifecycle") from exc
        except BaseExceptionGroup as exc:
            if _contains_cancellation(exc):
                cancellation = asyncio.CancelledError()
                _reap_after_cancellation(cancellation, self.registry, server)
                _audit_cancellation(
                    cancellation,
                    server_id=server.id,
                    tool_name="tools.list",
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="cancelled",
                    attempt=1,
                    reason_code="cancelled",
                    request_id=request_id,
                    task_id=task_id,
                )
                raise cancellation from exc
            reap_stale_server(self.registry, server)
            raise

    async def call(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        validation_started = time.monotonic()
        try:
            server = self._enabled_server(server_id)
        except McpHubError as exc:
            self._audit_entry_rejection(
                server_id=server_id,
                tool_name=tool_name,
                started=validation_started,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        if arguments is None:
            arguments = {}
        try:
            entry_tool = server.tool(tool_name)
        except KeyError as exc:
            rejected = McpToolNotAllowlisted("MCP tool is not allowlisted")
            self._audit_entry_rejection(
                server_id=server.id,
                tool_name=tool_name,
                started=validation_started,
                reason_code=rejected.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise rejected from exc
        entry_fixture_url = None
        if server.id == "playwright" and entry_tool.name == "browser_navigate":
            candidate_url = arguments.get("url") if isinstance(arguments, dict) else None
            if candidate_url in self._owned_playwright_fixtures:
                entry_fixture_url = str(candidate_url)
        try:
            _validate_arguments(
                server,
                entry_tool,
                arguments,
                playwright_fixture_url=entry_fixture_url,
            )
        except McpHubError as exc:
            self._audit_entry_rejection(
                server_id=server.id,
                tool_name=entry_tool.name,
                started=validation_started,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        try:
            operation_lease = await self._acquire_operation_gate(server, discovery=False)
        except asyncio.CancelledError as cancellation:
            _audit_cancellation(
                cancellation,
                server_id=server.id,
                tool_name=tool_name,
                duration_ms=int((time.monotonic() - validation_started) * 1_000),
                status="cancelled",
                attempt=1,
                reason_code="cancelled",
                request_id=request_id,
                task_id=task_id,
            )
            raise
        except McpHubBusy as exc:
            self._audit_entry_rejection(
                server_id=server.id,
                tool_name=tool_name,
                started=validation_started,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        try:
            try:
                server = self._enabled_server(server_id)
            except McpHubError as exc:
                self._audit_entry_rejection(
                    server_id=server_id,
                    tool_name=tool_name,
                    started=validation_started,
                    reason_code=exc.reason_code,
                    request_id=request_id,
                    task_id=task_id,
                )
                raise
            return await self._call_under_gate(
                server,
                tool_name,
                arguments,
                validation_started=validation_started,
                request_id=request_id,
                task_id=task_id,
            )
        except asyncio.CancelledError as cancellation:
            _release_operation_lease_after_cancellation(
                cancellation, operation_lease
            )
            raise
        finally:
            operation_lease.release()

    async def _call_under_gate(
        self,
        server: ServerSpec,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        validation_started: float,
        request_id: str | None,
        task_id: str | None,
    ) -> dict[str, Any]:
        try:
            tool = server.tool(tool_name)
        except KeyError as exc:
            rejected = McpToolNotAllowlisted("MCP tool is not allowlisted")
            self._audit_entry_rejection(
                server_id=server.id,
                tool_name=tool_name,
                started=validation_started,
                reason_code=rejected.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise rejected from exc
        if arguments is None:
            arguments = {}
        playwright_fixture_url = None
        if server.id == "playwright" and tool.name == "browser_navigate":
            candidate_url = arguments.get("url") if isinstance(arguments, dict) else None
            if candidate_url in self._owned_playwright_fixtures:
                playwright_fixture_url = str(candidate_url)
        try:
            _validate_arguments(
                server,
                tool,
                arguments,
                playwright_fixture_url=playwright_fixture_url,
            )
        except McpHubError as exc:
            write_call_audit(
                server_id=server.id,
                tool_name=tool.name,
                duration_ms=int((time.monotonic() - validation_started) * 1_000),
                status="rejected",
                attempt=1,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise
        try:
            await self._wait_for_slot(server)
        except asyncio.CancelledError as cancellation:
            _audit_cancellation(
                cancellation,
                server_id=server.id,
                tool_name=tool.name,
                duration_ms=int((time.monotonic() - validation_started) * 1_000),
                status="cancelled",
                attempt=1,
                reason_code="cancelled",
                request_id=request_id,
                task_id=task_id,
            )
            raise
        except McpHubBusy:
            write_call_audit(
                server_id=server.id,
                tool_name=tool.name,
                duration_ms=int((time.monotonic() - validation_started) * 1_000),
                status="rejected",
                attempt=1,
                reason_code="busy",
                request_id=request_id,
                task_id=task_id,
            )
            raise
        except McpHubError as exc:
            write_call_audit(
                server_id=server.id,
                tool_name=tool.name,
                duration_ms=int((time.monotonic() - validation_started) * 1_000),
                status="rejected",
                attempt=1,
                reason_code=exc.reason_code,
                request_id=request_id,
                task_id=task_id,
            )
            raise

        last_error: Exception | None = None
        last_reason = "transport_failure"
        for attempt in range(1, tool.max_attempts + 1):
            started = time.monotonic()
            call_timed_out = False
            try:
                total_timeout = (
                    server.lifecycle.startup_timeout_seconds
                    + server.lifecycle.readiness_timeout_seconds
                    + tool.call_timeout_seconds
                )
                async with asyncio.timeout(total_timeout):
                    with open(os.devnull, "w", encoding="utf-8") as errlog:
                        parameters = (
                            self._parameters(
                                server,
                                playwright_fixture_url=playwright_fixture_url,
                            )
                            if playwright_fixture_url is not None
                            else self._parameters(server)
                        )
                        async with stdio_client(parameters, errlog=errlog) as (read, write):
                            async with ClientSession(
                                read,
                                write,
                                read_timeout_seconds=timedelta(seconds=total_timeout),
                            ) as session:
                                await session.initialize()
                                discovered = (await session.list_tools()).tools
                                _validate_discovered_tools(server, discovered)
                                try:
                                    async with asyncio.timeout(tool.call_timeout_seconds):
                                        result = await session.call_tool(tool.name, arguments)
                                except TimeoutError:
                                    call_timed_out = True
                                    raise
                if result.isError:
                    raise McpRemoteToolError("MCP tool returned an error result")
                _validate_structured_output(
                    tool, getattr(result, "structuredContent", None)
                )
                payload = result.model_dump(mode="json", exclude_none=True)
                if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_000_000:
                    raise McpRemoteToolError("MCP tool result exceeded the bounded output policy")
                record_success(server)
                write_call_audit(
                    server_id=server.id,
                    tool_name=tool.name,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="ok",
                    attempt=attempt,
                    reason_code="ok",
                    request_id=request_id,
                    task_id=task_id,
                )
                return payload
            except asyncio.CancelledError as cancellation:
                _reap_after_cancellation(cancellation, self.registry, server)
                _audit_cancellation(
                    cancellation,
                    server_id=server.id,
                    tool_name=tool.name,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="cancelled",
                    attempt=attempt,
                    reason_code="cancelled",
                    request_id=request_id,
                    task_id=task_id,
                )
                raise
            except Exception as exc:
                task = asyncio.current_task()
                if _contains_cancellation(exc) or (task is not None and task.cancelling()):
                    cancellation = asyncio.CancelledError()
                    _reap_after_cancellation(cancellation, self.registry, server)
                    _audit_cancellation(
                        cancellation,
                        server_id=server.id,
                        tool_name=tool.name,
                        duration_ms=int((time.monotonic() - started) * 1_000),
                        status="cancelled",
                        attempt=attempt,
                        reason_code="cancelled",
                        request_id=request_id,
                        task_id=task_id,
                    )
                    raise cancellation from exc
                reap_stale_server(self.registry, server)
                last_error = exc
                reason = "timeout" if call_timed_out else _reason_for_exception(exc)
                if reason == "transport_failure" and circuit_open(server):
                    reason = "circuit_open"
                elif reason == "transport_failure" and self._ownership_state(server) in {
                    "owned_acquiring",
                    "owned_running",
                }:
                    reason = "busy"
                last_reason = reason
                write_call_audit(
                    server_id=server.id,
                    tool_name=tool.name,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status=(
                        "timed_out"
                        if reason == "timeout"
                        else "rejected"
                        if reason in {"busy", "circuit_open"}
                        else "failed"
                    ),
                    attempt=attempt,
                    reason_code=reason,
                    request_id=request_id,
                    task_id=task_id,
                )
                retryable = reason in {"timeout", "transport_failure", "server_unavailable"}
                if attempt >= tool.max_attempts or not tool.idempotent or not retryable:
                    break
                try:
                    await asyncio.sleep(0.1 * attempt)
                except asyncio.CancelledError as cancellation:
                    _audit_cancellation(
                        cancellation,
                        server_id=server.id,
                        tool_name=tool.name,
                        duration_ms=0,
                        status="cancelled",
                        attempt=attempt,
                        reason_code="cancelled",
                        request_id=request_id,
                        task_id=task_id,
                    )
                    raise
            except BaseExceptionGroup as exc:
                if _contains_cancellation(exc):
                    cancellation = asyncio.CancelledError()
                    _reap_after_cancellation(cancellation, self.registry, server)
                    _audit_cancellation(
                        cancellation,
                        server_id=server.id,
                        tool_name=tool.name,
                        duration_ms=int((time.monotonic() - started) * 1_000),
                        status="cancelled",
                        attempt=attempt,
                        reason_code="cancelled",
                        request_id=request_id,
                        task_id=task_id,
                    )
                    raise cancellation from exc
                reap_stale_server(self.registry, server)
                raise
        assert last_error is not None
        if last_reason == "busy":
            raise McpHubBusy("MCP server has reached its configured concurrency bound") from last_error
        if last_reason == "circuit_open":
            raise McpHubCircuitOpen("MCP server circuit opened during call") from last_error
        record_failure(server, last_reason)
        if last_reason == "timeout":
            raise McpHubTimeout("MCP call exceeded its bounded timeout") from last_error
        if isinstance(last_error, McpHubError):
            raise last_error
        raise McpHubUnavailable("MCP call failed within its bounded lifecycle") from last_error

    async def playwright_title_fixture(
        self,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Exercise Playwright only against an immutable Hub-owned page."""

        title = "Locestra MCP Fixture"
        fixture_path = f"/fixture/{secrets.token_urlsafe(24)}"
        fixture = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _fixture_handler(fixture_path, title),
        )
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        url = f"http://127.0.0.1:{fixture.server_port}{fixture_path}"
        self._owned_playwright_fixtures.add(url)
        thread.start()
        try:
            result = await self.call(
                "playwright",
                "browser_navigate",
                {"url": url},
                request_id=request_id,
                task_id=task_id,
            )
            if title not in json.dumps(result, ensure_ascii=False):
                raise McpRemoteToolError("Playwright fixture title was not observed")
            return result
        finally:
            self._owned_playwright_fixtures.discard(url)
            fixture.shutdown()
            fixture.server_close()
            thread.join(timeout=3)
