from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import psutil

from services.common import ROOT
from services.mcp_hub.config import generate_qwen_views, load_registry, validate_installed_source
from services.mcp_hub.hub import ManagedMcpHub, McpHubError
from services.mcp_hub.runtime import (
    owned_process_identities,
    owner_inventory,
    registry_snapshot,
    stop_owned_servers,
)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _managed_process_inventory(registry) -> list[dict[str, Any]]:
    """Return metadata only for exact project-owned launcher/server commands."""

    launcher = str((ROOT / "services" / "mcp_hub" / "launcher.py").resolve()).casefold()
    signatures: list[tuple[str, str]] = []
    for server in registry.servers:
        if server.transport.runtime == "node":
            signature = str((ROOT / server.transport.entrypoint).resolve()).casefold()
        else:
            signature = server.transport.entrypoint.casefold()
        signatures.append((server.id, signature))
    found: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "create_time", "cmdline"]):
        try:
            command = "\x00".join(process.info.get("cmdline") or []).casefold()
        except (psutil.Error, OSError):
            continue
        server_id = None
        role = None
        if launcher in command:
            role = "launcher"
            for candidate, _ in signatures:
                if f"--server-id\x00{candidate}" in command:
                    server_id = candidate
                    break
        else:
            for candidate, signature in signatures:
                if signature in command:
                    server_id = candidate
                    role = "server"
                    break
        if server_id and role:
            found.append(
                {
                    "pid": int(process.info["pid"]),
                    "create_time": float(process.info["create_time"]),
                    "server_id": server_id,
                    "role": role,
                }
            )
    return sorted(found, key=lambda item: (item["server_id"], item["role"], item["pid"]))


def _unowned_process_candidates(
    processes: list[dict[str, Any]],
    owned_identities: set[tuple[int, float]],
) -> list[dict[str, Any]]:
    return [
        item
        for item in processes
        if (item["pid"], item["create_time"]) not in owned_identities
    ]


async def _live_doctor() -> tuple[dict[str, Any], bool]:
    registry = load_registry()
    hub = ManagedMcpHub(registry)
    owners_before = owner_inventory(registry)
    processes_before = _managed_process_inventory(registry)
    unowned_before = _unowned_process_candidates(
        processes_before,
        owned_process_identities(registry),
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "registry": "ok",
        "failure_isolation": "checking_all_servers_independently",
        "servers": {},
        "owners_before": owners_before,
        "managed_processes_before": processes_before,
        "unowned_orphan_candidates": unowned_before,
    }
    healthy = (
        not unowned_before
        and not any(
            item["state"] not in {"owned_running", "owned_acquiring"}
            for item in owners_before
        )
    )
    for server in registry.servers:
        try:
            tools = await hub.discover(server.id, request_id="doctor-discovery")
            report["servers"][server.id] = {
                "discovery": "ok",
                "tools": [tool["name"] for tool in tools],
            }
        except Exception as exc:
            report["servers"][server.id] = {
                "discovery": "failed",
                "reason_code": getattr(exc, "reason_code", "transport_failure"),
            }
            healthy = False

    try:
        result = await hub.call(
            "context7",
            "query-docs",
            {
                "libraryId": "/fastapi/fastapi",
                "query": "Explain the current recommended lifespan parameter for application startup and shutdown.",
            },
            request_id="doctor-context7",
        )
        evidence = json.dumps(result, ensure_ascii=False).casefold()
        report["servers"]["context7"]["bounded_call"] = (
            "ok" if "lifespan" in evidence else "unexpected_result"
        )
        healthy = healthy and "lifespan" in evidence
    except Exception as exc:
        report["servers"]["context7"]["bounded_call"] = "failed"
        report["servers"]["context7"]["reason_code"] = getattr(
            exc, "reason_code", "transport_failure"
        )
        healthy = False

    try:
        result = await hub.playwright_title_fixture(
            request_id="doctor-playwright",
        )
        evidence = json.dumps(result, ensure_ascii=False)
        title_ok = "Locestra MCP Fixture" in evidence
        report["servers"]["playwright"]["title_fixture"] = "ok" if title_ok else "unexpected_result"
        healthy = healthy and title_ok
    except Exception as exc:
        report["servers"]["playwright"]["title_fixture"] = "failed"
        report["servers"]["playwright"]["reason_code"] = getattr(
            exc, "reason_code", "transport_failure"
        )
        healthy = False
    try:
        result = await hub.call(
            "local-diagnostics",
            "mcp_registry_status",
            {},
            request_id="doctor-local-diagnostics",
        )
        evidence = json.dumps(result, ensure_ascii=False)
        local_ok = all(server.id in evidence for server in registry.servers)
        report["servers"]["local-diagnostics"]["bounded_call"] = (
            "ok" if local_ok else "unexpected_result"
        )
        healthy = healthy and local_ok
    except Exception as exc:
        report["servers"]["local-diagnostics"]["bounded_call"] = "failed"
        report["servers"]["local-diagnostics"]["reason_code"] = getattr(
            exc, "reason_code", "transport_failure"
        )
        healthy = False
    report["failure_isolation"] = (
        "ok"
        if len(report["servers"]) == len(registry.servers)
        else "incomplete"
    )
    owners_after = owner_inventory(registry)
    processes_after = _managed_process_inventory(registry)
    before_owner_keys = {
        (item["server_id"], item["state"]) for item in owners_before
    }
    unexpected_owners = [
        item
        for item in owners_after
        if (item["server_id"], item["state"]) not in before_owner_keys
    ]
    before_processes = {
        (item["pid"], item["create_time"]) for item in processes_before
    }
    unexpected_processes = [
        item
        for item in processes_after
        if (item["pid"], item["create_time"]) not in before_processes
    ]
    report["owners_after"] = owners_after
    report["unexpected_owners"] = unexpected_owners
    report["managed_processes_after"] = processes_after
    report["unexpected_managed_processes"] = unexpected_processes
    lifecycle_clean = not unexpected_owners and not unexpected_processes
    report["lifecycle_cleanup"] = "ok" if lifecycle_clean else "orphan_detected"
    healthy = healthy and lifecycle_clean and report["failure_isolation"] == "ok"
    report["status"] = "ok" if healthy else "degraded"
    return report, healthy


async def _async_command(args: argparse.Namespace) -> int:
    registry = load_registry()
    hub = ManagedMcpHub(registry)
    if args.command == "discover":
        targets = [server.id for server in registry.servers] if args.server == "all" else [args.server]
        results: dict[str, Any] = {}
        ok = True
        for server_id in targets:
            try:
                results[server_id] = await hub.discover(server_id, request_id=args.request_id)
            except Exception as exc:
                results[server_id] = {
                    "status": "failed",
                    "reason_code": getattr(exc, "reason_code", "transport_failure"),
                }
                ok = False
        _emit({"schema_version": "1.0", "servers": results})
        return 0 if ok else 1
    if args.command == "call":
        try:
            arguments = json.loads(args.arguments)
            result = await hub.call(
                args.server,
                args.tool,
                arguments,
                request_id=args.request_id,
                task_id=args.task_id,
            )
            _emit({"schema_version": "1.0", "status": "ok", "result": result})
            return 0
        except (json.JSONDecodeError, McpHubError) as exc:
            _emit(
                {
                    "schema_version": "1.0",
                    "status": "failed",
                    "reason_code": getattr(exc, "reason_code", "invalid_arguments"),
                }
            )
            return 1
    raise AssertionError(args.command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Managed project-scoped MCP Hub")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("list")
    subparsers.add_parser("status")
    subparsers.add_parser("generate")
    subparsers.add_parser("stop")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--live", action="store_true")
    discover = subparsers.add_parser("discover")
    discover.add_argument("--server", default="all")
    discover.add_argument("--request-id", default="mcp-cli-discovery")
    call = subparsers.add_parser("call")
    call.add_argument("--server", required=True)
    call.add_argument("--tool", required=True)
    call.add_argument("--arguments", default="{}")
    call.add_argument("--request-id", default="mcp-cli-call")
    call.add_argument("--task-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_registry()
        if args.command == "validate":
            sources = {}
            for server in registry.servers:
                try:
                    validate_installed_source(server)
                    sources[server.id] = "ready"
                except (OSError, ValueError, json.JSONDecodeError):
                    sources[server.id] = "unavailable"
            healthy = all(value == "ready" for value in sources.values())
            _emit(
                {
                    "schema_version": registry.schema_version,
                    "policy_version": registry.policy_version,
                    "status": "ok" if healthy else "degraded",
                    "servers": [server.id for server in registry.servers],
                    "sources": sources,
                }
            )
            return 0 if healthy else 1
        if args.command in {"list", "status"}:
            _emit(registry_snapshot(registry))
            return 0
        if args.command == "generate":
            outputs = generate_qwen_views(registry)
            _emit({"schema_version": "1.0", "status": "ok", "views": sorted(outputs)})
            return 0
        if args.command == "stop":
            result = stop_owned_servers(registry)
            _emit({"schema_version": "1.0", **result})
            return 1 if result["refused"] else 0
        if args.command == "doctor":
            generate_qwen_views(registry)
            if args.live:
                report, healthy = asyncio.run(_live_doctor())
                _emit(report)
                return 0 if healthy else 1
            snapshot = registry_snapshot(registry)
            owners = owner_inventory(registry)
            sources_healthy = all(
                item["source_state"] == "ready" for item in snapshot["servers"]
            )
            runtime_healthy = all(
                item["runtime_state"] in {"on_demand", "ready", "disabled"}
                for item in snapshot["servers"]
            )
            ownership_healthy = all(
                item["state"] in {"owned_running", "owned_acquiring"}
                for item in owners
            )
            healthy = sources_healthy and runtime_healthy and ownership_healthy
            _emit(
                {
                    "schema_version": "1.0",
                    "status": "ok" if healthy else "degraded",
                    "registry": "ok",
                    "servers": snapshot["servers"],
                    "owners": owners,
                }
            )
            return 0 if healthy else 1
        return asyncio.run(_async_command(args))
    except Exception as exc:
        _emit(
            {
                "schema_version": "1.0",
                "status": "failed",
                "reason_code": getattr(exc, "reason_code", "configuration_error"),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
