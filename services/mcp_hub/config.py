from __future__ import annotations

import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Literal

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common import ROOT, RUN_DIR
from services.knowledge.privacy import detect_secret


MCP_REGISTRY_PATH = ROOT / "config" / "mcp-registry.json"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{8,}|sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceSpec(StrictModel):
    kind: Literal["npm", "project"]
    package: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def exact_version(self) -> "SourceSpec":
        if not _SEMVER.fullmatch(self.version) or any(
            marker in self.version.lower() for marker in ("latest", "*", "^", "~")
        ):
            raise ValueError("MCP sources require an exact semantic version")
        return self


class TransportSpec(StrictModel):
    kind: Literal["stdio"]
    runtime: Literal["node", "python_module"]
    entrypoint: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list, max_length=32)
    neutral_workspace: str = Field(min_length=1, max_length=256)
    runtime_environment_names: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def safe_launch_metadata(self) -> "TransportSpec":
        if any("\x00" in value or len(value) > 512 for value in self.args):
            raise ValueError("MCP arguments must be bounded strings")
        if self.runtime == "python_module" and not _MODULE_NAME.fullmatch(self.entrypoint):
            raise ValueError("invalid Python module entrypoint")
        if Path(self.neutral_workspace).is_absolute() or ".." in Path(
            self.neutral_workspace
        ).parts:
            raise ValueError("neutral workspace must be a repository-relative path")
        if Path(self.neutral_workspace).parts[:3] != ("run", "mcp", "workspaces"):
            raise ValueError("neutral workspace must remain under run/mcp/workspaces")
        forbidden_arguments = (
            "--allow-unrestricted-file-access",
            "--cdp-header",
            "--cdp-endpoint",
            "--config",
            "--extension",
            "--grant-permissions",
            "--init-page",
            "--init-script",
            "--no-sandbox",
            "--proxy-server",
            "--secrets",
            "--storage-state",
            "--user-data-dir",
        )
        if any(
            argument.casefold().split("=", 1)[0] in forbidden_arguments
            for argument in self.args
        ):
            raise ValueError("credential-bearing or sandbox-weakening MCP argument is forbidden")
        credential_markers = (
            "api-key",
            "apikey",
            "authorization",
            "cookie",
            "credential",
            "password",
            "secret",
            "token",
        )
        if any(
            argument.startswith("-")
            and any(marker in argument.casefold().split("=", 1)[0] for marker in credential_markers)
            for argument in self.args
        ):
            raise ValueError("credential-bearing MCP arguments are forbidden")
        if len(set(self.runtime_environment_names)) != len(self.runtime_environment_names):
            raise ValueError("runtime environment names contain duplicates")
        if any(not _ENVIRONMENT_NAME.fullmatch(item) for item in self.runtime_environment_names):
            raise ValueError("invalid runtime environment name")
        dangerous_environment_names = {
            "COMSPEC",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "HOME",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "NODE_OPTIONS",
            "NODE_PATH",
            "PATHEXT",
            "PATH",
            "PSMODULEPATH",
            "PYTHONHOME",
            "PYTHONPATH",
            "QWEN_HOME",
            "QWEN_RUNTIME_DIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
        }
        if any(item in dangerous_environment_names for item in self.runtime_environment_names):
            raise ValueError("runtime environment allowlist contains a process-injection name")
        return self


class ToolSpec(StrictModel):
    name: str
    input_schema_version: str = Field(min_length=1, max_length=32)
    upstream_input_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_schema: dict[str, Any]
    output_schema_version: str | None = Field(default=None, max_length=32)
    upstream_output_schema_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    output_schema: dict[str, Any] | None = None
    idempotent: bool
    call_timeout_seconds: int = Field(ge=1, le=120)
    max_attempts: int = Field(ge=1, le=2)

    @model_validator(mode="after")
    def bounded_tool(self) -> "ToolSpec":
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("invalid MCP tool name")
        if self.max_attempts > 1 and not self.idempotent:
            raise ValueError("only idempotent tools may be retried")
        output_contract = (
            self.output_schema_version,
            self.upstream_output_schema_sha256,
            self.output_schema,
        )
        if any(value is not None for value in output_contract) and not all(
            value is not None for value in output_contract
        ):
            raise ValueError("output schema version, hash and schema must be declared together")
        try:
            validator_for(self.input_schema).check_schema(self.input_schema)
            if self.output_schema is not None:
                validator_for(self.output_schema).check_schema(self.output_schema)
        except SchemaError as exc:
            raise ValueError("MCP tool contains an invalid JSON Schema") from exc
        return self


class BoundarySpec(StrictModel):
    locality: str = Field(min_length=1, max_length=64)
    data_egress: str = Field(min_length=1, max_length=128)
    permissions: list[str] = Field(min_length=1, max_length=16)
    risk: Literal["low", "medium", "high"]
    output_trust: str = Field(min_length=1, max_length=64)


class LifecycleSpec(StrictModel):
    startup_timeout_seconds: int = Field(ge=1, le=60)
    readiness_timeout_seconds: int = Field(ge=1, le=60)
    shutdown_timeout_seconds: int = Field(ge=1, le=30)
    circuit_failure_threshold: int = Field(ge=1, le=10)
    circuit_cooldown_seconds: int = Field(ge=1, le=600)


class ConcurrencySpec(StrictModel):
    max_instances: Literal[1]
    resource_lock: str = Field(pattern=r"^mcp:[a-z][a-z0-9-]{1,63}$")


class AuditSpec(StrictModel):
    log_payloads: Literal[False]
    log_results: Literal[False]
    redaction_policy: Literal["metadata_only_v1"]


class ServerSpec(StrictModel):
    id: str
    display_name: str = Field(min_length=1, max_length=128)
    enabled: bool
    configured_state: Literal["on_demand", "disabled"]
    version: str
    source: SourceSpec
    transport: TransportSpec
    consumers: list[Literal["qwen_platform", "qwen_docs", "mcp_hub"]] = Field(
        min_length=1, max_length=8
    )
    capabilities: list[str] = Field(min_length=1, max_length=16)
    tools: list[ToolSpec] = Field(min_length=1, max_length=16)
    boundary: BoundarySpec
    lifecycle: LifecycleSpec
    concurrency: ConcurrencySpec
    audit: AuditSpec

    @model_validator(mode="after")
    def consistent_server(self) -> "ServerSpec":
        if not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("invalid MCP server id")
        if self.version != self.source.version:
            raise ValueError("server and source versions differ")
        if self.configured_state == "disabled" and self.enabled:
            raise ValueError("a disabled server cannot be enabled")
        if len(set(self.consumers)) != len(self.consumers):
            raise ValueError("MCP consumers contain duplicates")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("MCP capabilities contain duplicates")
        tool_names = [tool.name for tool in self.tools]
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("MCP tool names contain duplicates")
        if self.concurrency.resource_lock != f"mcp:{self.id}":
            raise ValueError("resource lock must be derived from the server id")
        return self

    def tool(self, name: str) -> ToolSpec:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise KeyError(name)


class McpRegistry(StrictModel):
    schema_version: Literal["1.0"]
    policy_version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$")
    servers: list[ServerSpec] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_registry(self) -> "McpRegistry":
        ids = [server.id for server in self.servers]
        if len(set(ids)) != len(ids):
            raise ValueError("MCP registry contains duplicate server ids")
        capabilities = [item for server in self.servers for item in server.capabilities]
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("MCP registry contains duplicate capability ids")
        return self

    def server(self, server_id: str) -> ServerSpec:
        for server in self.servers:
            if server.id == server_id:
                return server
        raise KeyError(server_id)


def _reject_secret_values(payload: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = key.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "credential")):
                if not (key == "runtime_environment_names" and isinstance(value, list)):
                    raise ValueError(f"secret-shaped registry field is forbidden: {'.'.join((*path, key))}")
            _reject_secret_values(value, path=(*path, key))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _reject_secret_values(value, path=(*path, str(index)))
    elif isinstance(payload, str):
        if _SECRET_VALUE.search(payload):
            raise ValueError(f"secret-shaped registry value is forbidden: {'.'.join(path)}")
        try:
            secret_reason = detect_secret(payload.encode("utf-8", errors="strict"))
        except Exception as exc:
            raise ValueError(
                f"registry secret scanner failed closed: {'.'.join(path)}"
            ) from exc
        if secret_reason is not None:
            raise ValueError(f"secret-shaped registry value is forbidden: {'.'.join(path)}")


def load_registry(path: Path = MCP_REGISTRY_PATH) -> McpRegistry:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _reject_secret_values(payload)
    return McpRegistry.model_validate(payload)


def _inside_root(relative: str) -> Path:
    candidate = (ROOT / relative).resolve(strict=True)
    try:
        candidate.relative_to(ROOT.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("MCP entrypoint escapes the repository") from exc
    return candidate


def validate_installed_sources(registry: McpRegistry) -> None:
    for server in registry.servers:
        validate_installed_source(server)


def validate_installed_source(server: ServerSpec) -> None:
    """Validate one optional integration without coupling unrelated servers."""

    if server.source.kind == "npm":
        if server.transport.runtime != "node":
            raise ValueError("npm MCP sources must use the Node runtime")
        package_root = _inside_root(f"node_modules/{server.source.package}")
        manifest = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        if manifest.get("version") != server.source.version:
            raise ValueError(f"installed MCP version mismatch: {server.id}")
        entrypoint = _inside_root(server.transport.entrypoint)
        try:
            entrypoint.relative_to(package_root)
        except ValueError as exc:
            raise ValueError(f"MCP entrypoint is outside its package: {server.id}") from exc
    else:
        if server.transport.runtime != "python_module":
            raise ValueError("project MCP sources must use a Python module")
        if server.source.package != server.transport.entrypoint:
            raise ValueError("project MCP source must name its exact module entrypoint")
        module_parts = server.transport.entrypoint.split(".")
        module_file = ROOT.joinpath(*module_parts).with_suffix(".py")
        package_main = ROOT.joinpath(*module_parts, "__main__.py")
        candidates = [path for path in (module_file, package_main) if path.is_file()]
        if len(candidates) != 1:
            raise ValueError("project MCP module origin is missing or ambiguous")
        origin = candidates[0].resolve(strict=True)
        try:
            origin.relative_to(ROOT.resolve(strict=True))
        except ValueError as exc:
            raise ValueError("project MCP module origin escapes the repository") from exc


def managed_python() -> Path:
    candidates = [
        ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise FileNotFoundError("managed Python runtime is unavailable")


def neutral_workspace(server: ServerSpec) -> Path:
    path = (ROOT / server.transport.neutral_workspace).resolve()
    path.relative_to(RUN_DIR.resolve())
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolved_child_command(server: ServerSpec) -> list[str]:
    if server.transport.runtime == "node":
        entrypoint = _inside_root(server.transport.entrypoint)
        node = _resolve_executable("node.exe" if os.name == "nt" else "node")
        return [str(node), str(entrypoint), *server.transport.args]
    return [str(managed_python()), "-m", server.transport.entrypoint, *server.transport.args]


def _resolve_executable(name: str) -> Path:
    from shutil import which

    resolved = which(name) or which(Path(name).stem)
    if not resolved:
        raise FileNotFoundError(f"required MCP runtime is unavailable: {name}")
    return Path(resolved).resolve(strict=True)


def launcher_command(server: ServerSpec) -> tuple[str, list[str], str]:
    launcher = (ROOT / "services" / "mcp_hub" / "launcher.py").resolve(strict=True)
    return (
        str(managed_python()),
        [str(launcher), "--server-id", server.id],
        str(neutral_workspace(server)),
    )


def qwen_server_view(server: ServerSpec) -> dict[str, Any]:
    command, args, cwd = launcher_command(server)
    # The launcher owns the canonical deadlines and must have enough time to
    # return its bounded failure response before Qwen tears down stdio itself.
    # This transport-only grace does not extend the actual tool-call budget.
    transport_grace_ms = 2_000
    maximum_call_ms = (
        max(tool.call_timeout_seconds for tool in server.tools) * 1000
        + transport_grace_ms
    )
    discovery_ms = (
        server.lifecycle.startup_timeout_seconds
        + server.lifecycle.readiness_timeout_seconds
    ) * 1000 + transport_grace_ms
    return {
        "command": command,
        "args": args,
        "cwd": cwd,
        "timeout": maximum_call_ms,
        "discoveryTimeoutMs": discovery_ms,
        "trust": True,
        "includeTools": [tool.name for tool in server.tools],
        "description": (
            f"Managed {server.display_name}; capability={','.join(server.capabilities)}; "
            f"egress={server.boundary.data_egress}."
        ),
    }


def render_qwen_settings(profile: Literal["qwen", "qwen-docs", "qwen-code"], registry: McpRegistry) -> dict[str, Any]:
    source = ROOT / "config" / profile / "settings.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    if profile == "qwen-code":
        selected: list[ServerSpec] = []
    else:
        consumer = "qwen_docs" if profile == "qwen-docs" else "qwen_platform"
        selected = [
            server
            for server in registry.servers
            if server.enabled
            and server.configured_state == "on_demand"
            and consumer in server.consumers
        ]
    payload["mcpServers"] = {server.id: qwen_server_view(server) for server in selected}
    return payload


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_qwen_views(registry: McpRegistry | None = None) -> dict[str, Path]:
    active = registry or load_registry()
    outputs: dict[str, Path] = {}
    for profile in ("qwen", "qwen-docs", "qwen-code"):
        runtime_name = "qwen-platform" if profile == "qwen" else profile
        target = RUN_DIR / "qwen-homes" / runtime_name / "settings.json"
        write_json_atomic(target, render_qwen_settings(profile, active))
        outputs[profile] = target
    return outputs
