from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict

from services.mcp_hub.config import load_registry
from services.mcp_hub.runtime import registry_snapshot


mcp = FastMCP(
    "Locestra MCP Registry Diagnostics",
    instructions=(
        "Read-only, local-only bounded MCP registry state. This server cannot access arbitrary "
        "paths, execute commands, mutate state or use the network."
    ),
    log_level="ERROR",
)


class RegistryServerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    version: str
    enabled: bool
    configured_state: str
    source_state: str
    runtime_state: str
    last_reason_code: str
    checked_at: str | None
    consumers: list[str]
    capabilities: list[str]
    locality: str
    data_egress: str
    permissions: list[str]
    risk: str


class RegistryStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    policy_version: str
    servers: list[RegistryServerStatus]


@mcp.tool(
    name="mcp_registry_status",
    description=(
        "Return a bounded registry/health snapshot without commands, paths, environment values, "
        "tool payloads, results or secrets."
    ),
)
def mcp_registry_status() -> RegistryStatusResult:
    return RegistryStatusResult.model_validate(registry_snapshot(load_registry()))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
