from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psutil

from services.coding.process import ProcessTreeGuard, safe_child_environment
from services.common import ROOT
from services.mcp_hub.config import (
    load_registry,
    neutral_workspace,
    resolved_child_command,
    validate_installed_source,
)
from services.mcp_hub.audit import write_call_audit
from services.mcp_hub.hub import (
    McpHubError,
    _contains_secret,
    _validate_arguments,
    _validate_structured_output,
    canonical_schema_hash,
)
from services.mcp_hub.runtime import (
    LOCK_DIR,
    OWNER_DIR,
    OperationGuardLease,
    acquire_operation_guard,
    circuit_open,
    create_runtime_lock,
    owner_inventory,
    promote_runtime_owner,
    reap_stale_server,
    record_failure,
    record_success,
    remove_runtime_record,
    root_identity,
)


def _command_hash(process: psutil.Process) -> str:
    return hashlib.sha256(
        "\x00".join(process.cmdline()).casefold().encode("utf-8")
    ).hexdigest()


def _write_line(target, line: bytes, lock: threading.Lock) -> None:
    with lock:
        target.write(line)
        target.flush()


class _AuditWriteFailure(RuntimeError):
    """Required audit metadata could not be persisted."""


_CORRELATION_ENV = "LOCESTRA_MCP_CORRELATION_ID"
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _runtime_correlation_id() -> str | None:
    value = os.environ.get(_CORRELATION_ENV)
    if value is None:
        return None
    if not _SAFE_CORRELATION_ID.fullmatch(value) or _contains_secret(value):
        raise ValueError("invalid managed MCP correlation metadata")
    return value


class _ProtocolPolicy:
    """Filter the stdio surface and audit metadata without retaining payloads."""

    def __init__(
        self,
        server,
        *,
        correlation_id: str = "mcp-session",
        task_id: str | None = None,
        playwright_fixture_url: str | None = None,
        emit_lifecycle_events: bool = True,
    ) -> None:
        self.server = server
        self.correlation_id = correlation_id
        self.task_id = task_id
        self.playwright_fixture_url = playwright_fixture_url
        self.emit_lifecycle_events = emit_lifecycle_events
        self.allowed = {tool.name: tool for tool in server.tools}
        self.pending: dict[
            str, tuple[object, float, float, OperationGuardLease | None]
        ] = {}
        self.list_requests: dict[str, float] = {}
        self.protocol_requests: dict[str, str] = {}
        self.cancelled_pending: set[str] = set()
        self.lock = threading.Lock()
        self.ready_event = threading.Event()
        self.schema_failure_event = threading.Event()
        self.audit_failure_event = threading.Event()
        self._readiness_failure_recorded = False

    def _audit(self, **event) -> None:
        if self.emit_lifecycle_events:
            event.setdefault("request_id", self.correlation_id)
            if self.task_id is not None:
                event.setdefault("task_id", self.task_id)
            try:
                write_call_audit(**event)
            except (OSError, TypeError, ValueError) as exc:
                self.audit_failure_event.set()
                raise _AuditWriteFailure("managed MCP audit persistence failed") from exc

    def _close_response_state(self, key: str) -> None:
        with self.lock:
            self.protocol_requests.pop(key, None)
            self.list_requests.pop(key, None)
            pending = self.pending.pop(key, None)
            self.cancelled_pending.discard(key)
        if pending is not None:
            operation_lease = pending[3]
            if operation_lease is not None:
                operation_lease.release()

    def _audit_failure_response(self, request_id: object, key: str) -> bytes:
        """Close one request and return a bounded payload-free lifecycle error."""

        self.audit_failure_event.set()
        self.ready_event.clear()
        self.schema_failure_event.set()
        self._close_response_state(key)
        return self._json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": "Managed MCP audit persistence failed.",
                },
            }
        )

    @staticmethod
    def _key(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _json_line(payload: dict) -> bytes:
        return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    @staticmethod
    def _valid_request_id(value: object) -> bool:
        return (
            value is None
            or (isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, str) and len(value.encode("utf-8")) <= 256)
        )

    def _external_value_is_safe(self, value: object) -> bool:
        return self.server.boundary.data_egress == "none" or not _contains_secret(value)

    def client_line(self, line: bytes) -> tuple[bytes | None, bytes | None]:
        def reject(
            *,
            request_id,
            tool_name: str,
            reason_code: str,
            code: int,
            message: str,
        ) -> tuple[None, bytes | None]:
            self._audit(
                server_id=self.server.id,
                tool_name=tool_name,
                duration_ms=0,
                status="rejected",
                attempt=1,
                reason_code=reason_code,
                request_id=self.correlation_id,
            )
            if request_id is ...:
                return None, None
            return None, self._json_line(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )

        if len(line) > 2_000_000:
            return reject(
                request_id=None,
                tool_name="protocol.input",
                reason_code="input_too_large",
                code=-32600,
                message="Managed MCP input exceeded its bound.",
            )
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return reject(
                request_id=None,
                tool_name="protocol.input",
                reason_code="invalid_request",
                code=-32700,
                message="Managed MCP rejected malformed JSON.",
            )
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return reject(
                request_id=None,
                tool_name="protocol.input",
                reason_code="invalid_request",
                code=-32600,
                message="Managed MCP requires one JSON-RPC 2.0 request object.",
            )
        method = payload.get("method")
        request_id = payload.get("id", ...)
        allowed_notifications = {"notifications/initialized", "notifications/cancelled"}
        allowed_requests = {"initialize", "ping", "tools/list", "tools/call"}
        if "id" not in payload:
            if method not in allowed_notifications:
                return reject(
                    request_id=...,
                    tool_name="protocol.method",
                    reason_code="method_not_allowed",
                    code=-32601,
                    message="Managed MCP method is not allowed.",
                )
            allowed_top_level = {"jsonrpc", "method"}
            if "params" in payload:
                allowed_top_level.add("params")
            if set(payload) != allowed_top_level:
                return reject(
                    request_id=...,
                    tool_name="protocol.notification",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected an invalid notification envelope.",
                )
            params = payload.get("params", {})
            if not isinstance(params, dict):
                return reject(
                    request_id=...,
                    tool_name="protocol.notification",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid notification parameters.",
                )
            if method == "notifications/initialized":
                if params:
                    return reject(
                        request_id=...,
                        tool_name="protocol.notification",
                        reason_code="invalid_request",
                        code=-32600,
                        message="Managed MCP rejected invalid initialized parameters.",
                    )
                canonical_notification = {
                    "jsonrpc": "2.0",
                    "method": method,
                }
                if "params" in payload:
                    canonical_notification["params"] = {}
                return self._json_line(canonical_notification), None
            if (
                set(params) - {"requestId", "reason"}
                or "requestId" not in params
                or not self._valid_request_id(params["requestId"])
                or (
                    "reason" in params
                    and (
                        not isinstance(params["reason"], str)
                        or len(params["reason"].encode("utf-8")) > 512
                    )
                )
                or not self._external_value_is_safe(params)
            ):
                return reject(
                    request_id=...,
                    tool_name="protocol.notification",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid cancellation parameters.",
                )
            canonical_params = {"requestId": params["requestId"]}
            if "reason" in params:
                canonical_params["reason"] = params["reason"]
            canonical_notification = {
                "jsonrpc": "2.0",
                "method": method,
                "params": canonical_params,
            }
            if method == "notifications/cancelled":
                cancelled_id = params["requestId"]
                key = self._key(cancelled_id)
                with self.lock:
                    pending = self.pending.get(key)
                    newly_cancelled = pending is not None and key not in self.cancelled_pending
                    if newly_cancelled:
                        self.cancelled_pending.add(key)
                if pending is not None and newly_cancelled:
                    tool, started, _timeout, lease = pending
                    self._audit(
                        server_id=self.server.id,
                        tool_name=tool.name,
                        duration_ms=int((time.monotonic() - started) * 1_000),
                        status="cancelled",
                        attempt=1,
                        reason_code="cancelled",
                        request_id=self.correlation_id,
                    )
            return self._json_line(canonical_notification), None
        if method not in allowed_requests:
            return reject(
                request_id=request_id,
                tool_name="protocol.method",
                reason_code="method_not_allowed",
                code=-32601,
                message="Managed MCP method is not allowed.",
            )
        if (
            not self._valid_request_id(request_id)
            or not self._external_value_is_safe(request_id)
        ):
            return reject(
                request_id=None,
                tool_name="protocol.request",
                reason_code="invalid_request",
                code=-32600,
                message="Managed MCP rejected an invalid request id.",
            )
        expected_top_level = {"jsonrpc", "id", "method"}
        if "params" in payload:
            expected_top_level.add("params")
        if set(payload) != expected_top_level:
            return reject(
                request_id=request_id,
                tool_name="protocol.request",
                reason_code="invalid_request",
                code=-32600,
                message="Managed MCP rejected an invalid request envelope.",
            )
        key = self._key(request_id)
        with self.lock:
            duplicate_id = (
                key in self.protocol_requests
                or key in self.list_requests
                or key in self.pending
            )
        if duplicate_id:
            return reject(
                request_id=request_id,
                tool_name="protocol.request",
                reason_code="invalid_request",
                code=-32600,
                message="Managed MCP request id is already pending.",
            )
        if method == "initialize":
            params = payload.get("params", {})
            if not isinstance(params, dict) or set(params) - {
                "protocolVersion",
                "capabilities",
                "clientInfo",
            }:
                return reject(
                    request_id=request_id,
                    tool_name="protocol.initialize",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid initialize parameters.",
                )
            protocol_version = params.get("protocolVersion")
            capabilities = params.get("capabilities")
            client_info = params.get("clientInfo")
            if (
                (
                    protocol_version is not None
                    and (
                        not isinstance(protocol_version, str)
                        or not 1 <= len(protocol_version.encode("utf-8")) <= 64
                        or any(ord(character) < 0x20 for character in protocol_version)
                    )
                )
                or (capabilities is not None and not isinstance(capabilities, dict))
                or (
                    client_info is not None
                    and (
                        not isinstance(client_info, dict)
                        or bool(set(client_info) - {"name", "title", "version"})
                        or any(
                            not isinstance(value, str)
                            or not 1 <= len(value.encode("utf-8")) <= 256
                            or any(ord(character) < 0x20 for character in value)
                            for value in client_info.values()
                        )
                    )
                )
            ):
                return reject(
                    request_id=request_id,
                    tool_name="protocol.initialize",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid initialize parameters.",
                )
            canonical_params: dict[str, object] = {}
            if protocol_version is not None:
                canonical_params["protocolVersion"] = protocol_version
            if capabilities is not None:
                # The proxy rejects server-to-client requests, so advertising
                # arbitrary client capabilities would be both inaccurate and
                # an unnecessary data channel.
                canonical_params["capabilities"] = {}
            if client_info is not None:
                canonical_params["clientInfo"] = {
                    key: client_info[key]
                    for key in ("name", "title", "version")
                    if key in client_info
                }
            if not self._external_value_is_safe(canonical_params):
                return reject(
                    request_id=request_id,
                    tool_name="protocol.initialize",
                    reason_code="policy_rejected",
                    code=-32602,
                    message="Managed MCP rejected secret-shaped initialize data.",
                )
            with self.lock:
                self.protocol_requests[key] = method
            return self._json_line(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": canonical_params,
                }
            ), None
        if method == "ping":
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                return reject(
                    request_id=request_id,
                    tool_name="protocol.ping",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid ping parameters.",
                )
            canonical_request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if "params" in payload:
                canonical_request["params"] = {}
            with self.lock:
                self.protocol_requests[key] = method
            return self._json_line(canonical_request), None
        if method == "tools/list":
            params = payload.get("params", {})
            cursor = params.get("cursor") if isinstance(params, dict) else None
            if (
                not isinstance(params, dict)
                or set(params) - {"cursor"}
                or (
                    "cursor" in params
                    and (
                        not isinstance(cursor, str)
                        or len(cursor.encode("utf-8")) > 4_096
                    )
                )
                or not self._external_value_is_safe(params)
            ):
                return reject(
                    request_id=request_id,
                    tool_name="tools.list",
                    reason_code="invalid_request",
                    code=-32600,
                    message="Managed MCP rejected invalid tools/list parameters.",
                )
            canonical_request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if "params" in payload:
                canonical_request["params"] = (
                    {"cursor": cursor} if "cursor" in params else {}
                )
            with self.lock:
                self.list_requests[key] = time.monotonic()
            return self._json_line(canonical_request), None
        params = payload.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        metadata = params.get("_meta") if isinstance(params, dict) else None
        progress_token = (
            metadata.get("progressToken") if isinstance(metadata, dict) else None
        )
        valid_progress_token = (
            isinstance(progress_token, int)
            and not isinstance(progress_token, bool)
        ) or (
            isinstance(progress_token, str)
            and 1 <= len(progress_token.encode("utf-8")) <= 256
        )
        if (
            not isinstance(params, dict)
            or set(params) - {"name", "arguments", "_meta"}
            or "name" not in params
            or ("arguments" in params and not isinstance(arguments, dict))
            or (
                "_meta" in params
                and (
                    not isinstance(metadata, dict)
                    or set(metadata) != {"progressToken"}
                    or not valid_progress_token
                    or _contains_secret(progress_token)
                )
            )
        ):
            return reject(
                request_id=request_id,
                tool_name=name if isinstance(name, str) else "invalid.tool",
                reason_code="invalid_request",
                code=-32600,
                message="Managed MCP rejected invalid tools/call parameters.",
            )
        if not self.ready_event.is_set() or self.schema_failure_event.is_set():
            return reject(
                request_id=request_id,
                tool_name=name if isinstance(name, str) else "invalid.tool",
                reason_code="schema_not_ready",
                code=-32002,
                message="Managed MCP tool schemas are not ready.",
            )
        if self.emit_lifecycle_events and circuit_open(self.server):
            return reject(
                request_id=request_id,
                tool_name=name if isinstance(name, str) else "invalid.tool",
                reason_code="circuit_open",
                code=-32000,
                message="Managed MCP server circuit is open.",
            )
        with self.lock:
            call_in_flight = bool(self.pending)
        if call_in_flight:
            return reject(
                request_id=request_id,
                tool_name=name if isinstance(name, str) else "invalid.tool",
                reason_code="busy",
                code=-32001,
                message="Managed MCP already has a tool call in flight.",
            )
        tool = self.allowed.get(name) if isinstance(name, str) else None
        reason = "tool_not_allowlisted"
        if tool is not None:
            try:
                _validate_arguments(
                    self.server,
                    tool,
                    arguments,
                    playwright_fixture_url=self.playwright_fixture_url,
                )
                with self.lock:
                    self.pending[key] = (
                        tool,
                        time.monotonic(),
                        float(tool.call_timeout_seconds),
                        None,
                    )
                return self._json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": {
                            "name": name,
                            "arguments": arguments,
                            **(
                                {"_meta": {"progressToken": progress_token}}
                                if "_meta" in params
                                else {}
                            ),
                        },
                    }
                ), None
            except McpHubError as exc:
                reason = exc.reason_code
        return reject(
            request_id=request_id,
            tool_name=name if isinstance(name, str) else "invalid.tool",
            reason_code=reason,
            code=-32602,
            message="Managed MCP policy rejected this tool call.",
        )

    def server_line(self, line: bytes) -> bytes | None:
        def reject_output(reason_code: str) -> None:
            self._audit(
                server_id=self.server.id,
                tool_name="protocol.output",
                duration_ms=0,
                status="rejected",
                attempt=1,
                reason_code=reason_code,
                request_id=self.correlation_id,
            )

        if len(line) > 2_000_000:
            reject_output("output_too_large")
            return None
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reject_output("invalid_response")
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or "id" not in payload
            or "method" in payload
            or (("result" in payload) == ("error" in payload))
            or ("error" in payload and not isinstance(payload.get("error"), dict))
        ):
            reject_output("invalid_response")
            return None
        key = self._key(payload["id"])
        with self.lock:
            protocol_method = self.protocol_requests.get(key)
            list_started = self.list_requests.get(key)
            is_list = list_started is not None
            pending = self.pending.get(key)
            was_cancelled = key in self.cancelled_pending
        if protocol_method is not None:
            self._close_response_state(key)
            return line
        if is_list:
            def fail_list(reason_code: str, response: bytes) -> bytes:
                self.schema_failure_event.set()
                try:
                    self._audit(
                        server_id=self.server.id,
                        tool_name="tools.list",
                        duration_ms=int((time.monotonic() - list_started) * 1_000),
                        status="failed",
                        attempt=1,
                        reason_code=reason_code,
                        request_id=self.correlation_id,
                    )
                    if self.emit_lifecycle_events:
                        record_failure(self.server, reason_code)
                except (_AuditWriteFailure, OSError, TypeError, ValueError):
                    return self._audit_failure_response(payload["id"], key)
                self._close_response_state(key)
                return response

            if "error" in payload:
                return fail_list("remote_tool_error", line)
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                return fail_list(
                    "schema_mismatch",
                    self._json_line(
                        {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "error": {
                                "code": -32603,
                                "message": "Managed MCP returned an invalid tools/list result.",
                            },
                        }
                    ),
                )
            discovered: dict[str, list[dict]] = {}
            for tool in result["tools"]:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    discovered.setdefault(tool["name"], []).append(tool)
            selected: list[dict] = []
            schema_mismatch = False
            for expected in self.server.tools:
                matches = discovered.get(expected.name, [])
                if len(matches) != 1:
                    schema_mismatch = True
                    break
                actual = matches[0]
                input_schema = actual.get("inputSchema")
                output_schema = actual.get("outputSchema")
                if not isinstance(input_schema, dict):
                    schema_mismatch = True
                    break
                if (
                    expected.upstream_input_schema_sha256
                    and canonical_schema_hash(input_schema)
                    != expected.upstream_input_schema_sha256
                ):
                    schema_mismatch = True
                    break
                if expected.upstream_output_schema_sha256:
                    if (
                        not isinstance(output_schema, dict)
                        or canonical_schema_hash(output_schema)
                        != expected.upstream_output_schema_sha256
                    ):
                        schema_mismatch = True
                        break
                selected.append(actual)
            if schema_mismatch:
                result["tools"] = []
                return fail_list("schema_mismatch", self._json_line(payload))
            result["tools"] = selected
            self.ready_event.set()
            if self.emit_lifecycle_events:
                try:
                    self._audit(
                        server_id=self.server.id,
                        tool_name="tools.list",
                        duration_ms=int(
                            (time.monotonic() - list_started) * 1_000
                        ),
                        status="ok",
                        attempt=1,
                        reason_code="ok",
                        request_id=self.correlation_id,
                    )
                except _AuditWriteFailure:
                    return self._audit_failure_response(payload["id"], key)
            self._close_response_state(key)
            return self._json_line(payload)
        if pending is not None:
            if was_cancelled:
                self._close_response_state(key)
                return None
            tool, started, _timeout, operation_lease = pending
            result = payload.get("result")
            malformed_result = "result" in payload and (
                not isinstance(result, dict)
                or not isinstance(result.get("content"), list)
                or (
                    "isError" in result
                    and not isinstance(result.get("isError"), bool)
                )
            )
            failed = "error" in payload or malformed_result or (
                isinstance(result, dict) and result.get("isError") is True
            )
            reason = (
                "schema_mismatch"
                if malformed_result
                else "remote_tool_error"
                if failed
                else "ok"
            )
            if not failed:
                structured_content = (
                    result.get("structuredContent") if isinstance(result, dict) else None
                )
                try:
                    _validate_structured_output(tool, structured_content)
                except McpHubError:
                    failed = True
                    reason = "schema_mismatch"
                    payload = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {
                            "code": -32603,
                            "message": "Managed MCP output failed schema validation.",
                        },
                    }
            elif malformed_result:
                payload = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {
                        "code": -32603,
                        "message": "Managed MCP returned an invalid tool result envelope.",
                    },
                }
            try:
                self._audit(
                    server_id=self.server.id,
                    tool_name=tool.name,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="failed" if failed else "ok",
                    attempt=1,
                    reason_code=reason,
                    request_id=self.correlation_id,
                )
                if self.emit_lifecycle_events:
                    if failed:
                        record_failure(self.server, reason)
                    else:
                        record_success(self.server)
            except (_AuditWriteFailure, OSError, TypeError, ValueError):
                return self._audit_failure_response(payload["id"], key)
            self._close_response_state(key)
            return self._json_line(payload) if reason == "schema_mismatch" else line
        reject_output("unsolicited_response")
        return None

    def cancel_pending(self) -> None:
        with self.lock:
            pending = [
                pending
                for key, pending in self.pending.items()
                if key not in self.cancelled_pending
            ]
            self.pending.clear()
            self.cancelled_pending.clear()
        for tool, started, _timeout, operation_lease in pending:
            try:
                self._audit(
                    server_id=self.server.id,
                    tool_name=tool.name,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                    status="cancelled",
                    attempt=1,
                    reason_code="cancelled",
                    request_id=self.correlation_id,
                )
            finally:
                if operation_lease is not None:
                    operation_lease.release()

    def expire_pending(self, *, now: float | None = None) -> bool:
        """Expire direct-consumer calls at their canonical registry timeout."""

        observed = time.monotonic() if now is None else now
        expired: list[
            tuple[object, float, float, OperationGuardLease | None, bool]
        ] = []
        with self.lock:
            for key, pending in list(self.pending.items()):
                _tool, started, timeout_seconds, _lease = pending
                if observed - started >= timeout_seconds:
                    was_cancelled = key in self.cancelled_pending
                    tool, began, timeout, lease = self.pending.pop(key)
                    expired.append((tool, began, timeout, lease, was_cancelled))
                    self.cancelled_pending.discard(key)
        for tool, started, _timeout, operation_lease, was_cancelled in expired:
            if was_cancelled:
                continue
            try:
                self._audit(
                    server_id=self.server.id,
                    tool_name=tool.name,
                    duration_ms=int((observed - started) * 1_000),
                    status="timed_out",
                    attempt=1,
                    reason_code="timeout",
                    request_id=self.correlation_id,
                )
                if self.emit_lifecycle_events:
                    record_failure(self.server, "timeout")
            finally:
                if operation_lease is not None:
                    operation_lease.release()
        return bool(expired)

    def fail_readiness_timeout(self) -> bool:
        with self.lock:
            if self.ready_event.is_set() or self._readiness_failure_recorded:
                return False
            self._readiness_failure_recorded = True
        self._audit(
            server_id=self.server.id,
            tool_name="tools.list",
            duration_ms=0,
            status="timed_out",
            attempt=1,
            reason_code="timeout",
            request_id=self.correlation_id,
        )
        if self.emit_lifecycle_events:
            record_failure(self.server, "timeout")
        return True


def _pump(source, target, output_lock: threading.Lock) -> None:
    try:
        while True:
            reader = getattr(source, "read1", source.read)
            chunk = reader(16_384)
            if not chunk:
                break
            _write_line(target, chunk, output_lock)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _drain(source) -> None:
    """Drain untrusted child diagnostics without forwarding sensitive content."""

    try:
        while True:
            reader = getattr(source, "read1", source.read)
            if not reader(16_384):
                return
    except (BrokenPipeError, OSError, ValueError):
        return


def _pump_input(source, target, response_target, policy: _ProtocolPolicy, output_lock: threading.Lock) -> None:
    try:
        while True:
            line = source.readline(2_000_001)
            if not line:
                break
            if len(line) > 2_000_000 and not line.endswith(b"\n"):
                while True:
                    remainder = source.readline(2_000_001)
                    if not remainder or remainder.endswith(b"\n"):
                        break
            forward, response = policy.client_line(line)
            if forward is not None:
                target.write(forward)
                target.flush()
            if response is not None:
                _write_line(response_target, response, output_lock)
    finally:
        try:
            target.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def _pump_output(
    source,
    target,
    policy: _ProtocolPolicy,
    output_lock: threading.Lock,
    guard: ProcessTreeGuard | None = None,
) -> None:
    try:
        while True:
            line = source.readline(2_000_001)
            if not line:
                break
            if len(line) > 2_000_000 and not line.endswith(b"\n"):
                while True:
                    remainder = source.readline(2_000_001)
                    if not remainder or remainder.endswith(b"\n"):
                        break
            filtered = policy.server_line(line)
            if filtered is not None:
                _write_line(target, filtered, output_lock)
            if policy.audit_failure_event.is_set():
                if guard is not None:
                    guard.terminate(include_parent=True)
                return
    except (BrokenPipeError, OSError, ValueError, _AuditWriteFailure):
        if policy.audit_failure_event.is_set() and guard is not None:
            guard.terminate(include_parent=True)


def _child_environment(server) -> dict[str, str]:
    environment = safe_child_environment({"PYTHONPATH": str(ROOT)})
    runtime = dict(os.environ)
    for name in server.transport.runtime_environment_names:
        # Registry names are the exact allowlist.  Values are runtime-only and
        # intentionally bypass generic secret-name rejection.
        if name not in runtime:
            continue
        value = runtime[name]
        if "\x00" in value:
            raise ValueError("invalid MCP runtime environment value")
        environment[name] = value
    return environment


def _session_workspace(server, nonce_sha256: str) -> tuple[Path, Path]:
    base = neutral_workspace(server).resolve(strict=True)
    session = Path(
        tempfile.mkdtemp(prefix=f"session-{nonce_sha256[:16]}-", dir=base)
    ).resolve(strict=True)
    session.relative_to(base)
    return base, session


def _remove_session_workspace(base: Path, session: Path) -> bool:
    try:
        session.resolve().relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        return False
    for _attempt in range(5):
        try:
            shutil.rmtree(session)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.05)
    return not session.exists()


def _guard_has_live_members(
    process: subprocess.Popen[bytes] | None, guard: ProcessTreeGuard | None
) -> bool:
    if process is not None and process.poll() is None:
        return True
    if guard is None:
        return False
    guard.refresh()
    for identity in guard.descendants:
        try:
            member = psutil.Process(identity[0])
            if (
                abs(member.create_time() - identity[1]) <= 0.02
                and member.is_running()
                and member.status() != psutil.STATUS_ZOMBIE
            ):
                return True
        except (psutil.Error, OSError):
            continue
    return False


def _watch_direct_lifecycle(
    policy: _ProtocolPolicy,
    guard: ProcessTreeGuard,
    stop_event: threading.Event,
    lifecycle_failure: threading.Event,
) -> None:
    readiness_deadline = time.monotonic() + (
        policy.server.lifecycle.startup_timeout_seconds
        + policy.server.lifecycle.readiness_timeout_seconds
    )
    while not stop_event.wait(0.05):
        failed = False
        try:
            if policy.audit_failure_event.is_set() or policy.schema_failure_event.is_set():
                failed = True
            elif not policy.ready_event.is_set() and time.monotonic() >= readiness_deadline:
                failed = policy.fail_readiness_timeout()
            elif policy.ready_event.is_set():
                failed = policy.expire_pending()
        except (_AuditWriteFailure, OSError, TimeoutError, TypeError, ValueError):
            failed = True
        if failed:
            lifecycle_failure.set()
            guard.terminate(include_parent=True)
            return


def run_managed_server(
    server_id: str,
    *,
    playwright_fixture_url: str | None = None,
    hub_client: bool = False,
) -> int:
    registry = load_registry()
    try:
        server = registry.server(server_id)
    except KeyError:
        print("managed MCP server is not registered", file=sys.stderr)
        return 64
    if not server.enabled or server.configured_state != "on_demand":
        print("managed MCP server is disabled", file=sys.stderr)
        return 69
    if hub_client:
        return _run_registered_server(
            registry,
            server,
            playwright_fixture_url=playwright_fixture_url,
            hub_client=True,
        )
    try:
        operation_lease = acquire_operation_guard(server.id, timeout_seconds=2.0)
    except TimeoutError:
        print("managed MCP server operation gate is busy", file=sys.stderr)
        return 75
    try:
        return _run_registered_server(
            registry,
            server,
            playwright_fixture_url=playwright_fixture_url,
            hub_client=False,
        )
    finally:
        operation_lease.release()


def _run_registered_server(
    registry,
    server,
    *,
    playwright_fixture_url: str | None,
    hub_client: bool,
) -> int:
    runtime_correlation_id = None
    if not hub_client:
        try:
            runtime_correlation_id = _runtime_correlation_id()
        except ValueError:
            print("managed MCP correlation metadata is invalid", file=sys.stderr)
            return 64
    if circuit_open(server):
        print("managed MCP server circuit is open", file=sys.stderr)
        return 69
    try:
        validate_installed_source(server)
    except (OSError, ValueError, json.JSONDecodeError):
        if not hub_client:
            record_failure(server, "source_unavailable")
        print("managed MCP source is unavailable or differs from its evaluated version", file=sys.stderr)
        return 69

    OWNER_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_bytes(32)
    nonce_sha256 = hashlib.sha256(nonce).hexdigest()
    try:
        owner = psutil.Process(os.getpid())
        parent = psutil.Process(os.getppid())
        lock_record = {
            "schema_version": "1.0",
            "server_id": server.id,
            "root_identity": root_identity(),
            "nonce_sha256": nonce_sha256,
            "owner_pid": owner.pid,
            "owner_create_time": owner.create_time(),
            "parent_pid": parent.pid,
            "parent_create_time": parent.create_time(),
            "state": "acquiring",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    except psutil.Error:
        return 70
    deadline = time.monotonic() + 2.0
    while True:
        try:
            acquired = create_runtime_lock(server, lock_record)
        except (OSError, ValueError):
            return 70
        if acquired:
            if circuit_open(server):
                if not remove_runtime_record(server.id, nonce_sha256):
                    return 73
                print("managed MCP server circuit opened during acquisition", file=sys.stderr)
                return 69
            break
        observed = next(
            (
                item
                for item in owner_inventory(registry)
                if item["server_id"] == server.id
            ),
            None,
        )
        if observed and observed["state"] in {"stale_lock", "stale_owner_record"}:
            result = reap_stale_server(registry, server)
            if result["refused"]:
                return 73
            continue
        if time.monotonic() >= deadline:
            print("managed MCP server is busy", file=sys.stderr)
            return 75
        time.sleep(0.05)

    process: subprocess.Popen[bytes] | None = None
    guard: ProcessTreeGuard | None = None
    workspace_base: Path | None = None
    session_workspace: Path | None = None
    watchdog_stop = threading.Event()
    lifecycle_failure = threading.Event()
    watchdog: threading.Thread | None = None
    result_code = 70
    try:
        workspace_base, session_workspace = _session_workspace(server, nonce_sha256)
        command = resolved_child_command(server)
        environment = _child_environment(server)
        process = subprocess.Popen(
            command,
            cwd=session_workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
        guard = ProcessTreeGuard(process)
        child = psutil.Process(process.pid)
        record = {
            **lock_record,
            "state": "running",
            "child_pid": child.pid,
            "child_create_time": child.create_time(),
            "child_command_sha256": _command_hash(child),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if not promote_runtime_owner(server, record):
            raise RuntimeError("MCP ownership generation was lost during launch")

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        policy = _ProtocolPolicy(
            server,
            correlation_id=(
                runtime_correlation_id
                or f"mcp-{server.id}-{nonce_sha256[:16]}"
            ),
            task_id=runtime_correlation_id,
            playwright_fixture_url=playwright_fixture_url,
            emit_lifecycle_events=not hub_client,
        )
        output_lock = threading.Lock()
        threads = [
            threading.Thread(
                target=_pump_input,
                args=(
                    sys.stdin.buffer,
                    process.stdin,
                    sys.stdout.buffer,
                    policy,
                    output_lock,
                ),
                name=f"mcp-{server.id}-stdin",
                daemon=True,
            ),
            threading.Thread(
                target=_pump_output,
                args=(process.stdout, sys.stdout.buffer, policy, output_lock, guard),
                name=f"mcp-{server.id}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_drain,
                args=(process.stderr,),
                name=f"mcp-{server.id}-stderr",
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        if not hub_client:
            watchdog = threading.Thread(
                target=_watch_direct_lifecycle,
                args=(policy, guard, watchdog_stop, lifecycle_failure),
                name=f"mcp-{server.id}-watchdog",
                daemon=True,
            )
            watchdog.start()
        return_code = process.wait()
        for thread in threads[1:]:
            thread.join(timeout=2)
        policy.cancel_pending()
        if (
            return_code != 0
            and not hub_client
            and not lifecycle_failure.is_set()
            and not policy.schema_failure_event.is_set()
        ):
            record_failure(server, "transport_failure")
            print("managed MCP child exited with a transport failure", file=sys.stderr)
        result_code = return_code
    except (KeyboardInterrupt, OSError, psutil.Error, RuntimeError, ValueError):
        result_code = 70
    finally:
        watchdog_stop.set()
        if watchdog is not None:
            watchdog.join(timeout=1)
        try:
            if guard is not None:
                guard.terminate(include_parent=True)
            elif process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=server.lifecycle.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            result_code = 70
        tree_alive = _guard_has_live_members(process, guard)
        if session_workspace is not None and workspace_base is not None:
            if not _remove_session_workspace(workspace_base, session_workspace):
                result_code = 70
        if not tree_alive and not remove_runtime_record(server.id, nonce_sha256):
            # A newer generation is never removed.  If our generation vanished
            # concurrently, there is nothing left for this launcher to clean.
            current = next(
                (
                    item
                    for item in owner_inventory(registry)
                    if item["server_id"] == server.id
                ),
                None,
            )
            if current is not None:
                result_code = 70
    return result_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Owned stdio launcher for one registered MCP server")
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--playwright-fixture-url")
    parser.add_argument("--hub-client", action="store_true")
    args = parser.parse_args()
    return run_managed_server(
        args.server_id,
        playwright_fixture_url=args.playwright_fixture_url,
        hub_client=args.hub_client,
    )


if __name__ == "__main__":
    raise SystemExit(main())
