# Managed MCP Hub

Status: Stage 006 verified complete and published. Registry schema `1.0`;
policy `2026-07-17.1`.

## Purpose

The MCP Hub is a small registry, lifecycle, and policy layer for integrations
with a demonstrated workflow. It is not a proxy for every application and it
does not replace native filesystem, terminal, Git, browser, or coding tools.

One canonical project registry records, for every server:

- exact source/version and stdio launch specification;
- consumers, capabilities, and minimal allowed tool schemas;
- locality, data egress, permissions, and risk;
- startup/readiness/call timeouts and bounded idempotent retry;
- process ownership, concurrency locks, circuit state, and health;
- metadata-only audit and redaction policy;
- enabled, disabled, or degraded state.

Runtime executable paths are resolved locally and are never committed.
Consumer-specific Qwen settings are generated from the registry into ignored
runtime homes. Global Qwen and Codex profiles are not modified.

## Included integrations

| Integration | Consumer and workflow | Boundary |
|---|---|---|
| Context7 `3.2.3` | Documentation route resolves a library and retrieves current public docs | External, untrusted public-documentation egress; secrets, personal data, and proprietary code are prohibited |
| Playwright MCP `0.0.78` | Hub health and UI QA retrieve the title of a Hub-owned immutable fixture | Loopback fixture only; arbitrary navigation and mutation tools are not exposed |
| Local diagnostics `1.0.0` | Platform Qwen reads bounded registry/health metadata | Local, no network, no path/command input, and no arbitrary file access |

The coding Qwen profile remains MCP-free. The primary browser route keeps its
separate public-target, DNS, redirect, and subrequest policy.

## Deliberately rejected or deferred

| Candidate | Decision |
|---|---|
| Filesystem, shell, and Git MCP | Rejected because the Coding Engine already has narrower native adapters, worktree policy, and verification |
| Generic memory MCP | Rejected because typed Memory and Knowledge contracts already exist and a generic tool would widen disclosure |
| GitHub MCP | Deferred until a concrete workflow and scoped authentication exist; no token is requested or logged |
| ComfyUI MCP | Deferred until an evaluated image consumer needs it |
| Documents/Office/PDF MCP | Deferred pending a bounded workflow, permission review, and end-to-end test |
| Telegram MCP | Deferred while the existing ingress lacks the broader actor boundary required for a new tool surface |
| Arbitrary Playwright tools | Rejected; the Stage 006 MCP workflow needs one bounded fixture-navigation capability |

## Lifecycle and isolation

Servers start lazily for discovery or a call. The Hub resolves a tracked
launcher, uses a neutral runtime directory, launches without a shell, waits for
readiness, enforces a per-call deadline, and tears down only an exactly owned
process tree.

Retry is limited to declared idempotent calls and retryable transport failures.
A broken server becomes degraded or circuit-open without making chat, coding,
or other integrations unready. Passive status does not start a server.
Cancellation remains cancellation even if secondary audit or cleanup also
fails. Stop and watchdog paths are idempotent and detect unowned/stale evidence
without killing by process name.

This is process and policy isolation, not an OS sandbox. Evaluated upstream
Node/Python code still runs with the local user's authority and its declared
network boundary.

## Node dependency compatibility

The locked MCP SDK `1.29.0` still declares `@hono/node-server ^1.19.9`, but
that entire declared line is affected by GHSA-frvp-7c67-39w9. The project
therefore overrides it to audited `2.0.11` and requires Node.js 20 or newer.
This exception is bounded by `npm audit --audit-level=low` and
`npm run mcp:node-compat`, which imports the SDK's Node transport and completes
a real MCP initialize handshake on an ephemeral loopback listener. Re-evaluate
and remove the override as soon as the SDK declares a non-vulnerable range;
dependency updates must change the lock, compatibility assertion, and MCP E2E
together.

## Schemas, calls, and audit

The launcher filters `tools/list` to the registry allowlist, verifies upstream
schema hashes, validates JSON-RPC envelopes and arguments, and enforces a
bounded request size before forwarding a call. Unknown tools, extra fields,
secret-shaped input, schema drift, and disallowed egress fail closed.

Audit records contain only event time, server/tool identifiers, duration,
status, attempt, reason code, and filtered request/task correlation. Arguments,
results, documentation/page content, commands, paths, environment, credentials,
and exception text are not logged. Failure to write mandatory audit evidence
fails the operation and closes owned runtime state.

## Generated consumer views

| View | MCP surface |
|---|---|
| Platform Qwen | local diagnostics only |
| Documentation Qwen | Context7 resolve/query only, in a fresh neutral per-request workspace |
| Coding Qwen | none |
| Codex | unchanged; user/global MCP definitions are outside Hub ownership |

Generated views are disposable outputs. Edit the canonical registry and run its
validation/generation flow instead of modifying runtime settings manually.

## Operations

From the repository root:

```powershell
uv run python -m services.mcp_hub.cli validate
uv run python -m services.mcp_hub.cli list
uv run python -m services.mcp_hub.cli status
uv run python -m services.mcp_hub.cli generate
uv run python -m services.mcp_hub.cli doctor --live
uv run python -m services.mcp_hub.cli stop
```

`scripts/start.ps1` generates views but does not keep optional MCP servers
resident. `scripts/doctor.ps1` runs bounded live discovery/calls.
`scripts/stop.ps1` stops proven owned MCP processes before the rest of the
platform.

## Acceptance evidence

The exact-final Stage 006 revision reported:

- MCP-only: `97 passed`;
- expanded focused regression: `164 passed, 1 skipped`;
- full regression: `855 passed, 12 skipped`;
- live Context7 documentation retrieval, Playwright title fixture, and local
  diagnostics through managed consumers;
- `DOCTOR_OK` and `SMOKE_TEST_OK`;
- failure isolation, timeout/cancel/retry, audit redaction, generated-config
  consistency, lifecycle cleanup, and zero remaining owned MCP processes;
- clean dependency and secret scans and no unresolved P0/P1/P2 audit findings.

The numbers document the accepted revision; they are not runtime performance
claims.

## Troubleshooting

- `configuration_error`: validate the registry and exact dependency lock.
- `schema_mismatch`: keep the integration degraded until its version/schema is
  reviewed and evaluated together.
- `policy_rejected`: inspect the minimal tool schema, input bounds, secret
  detector, and egress classification.
- `timeout` or `server_unavailable`: inspect metadata-only status, then run live
  doctor after the dependency is restored.
- stale owner evidence: run managed stop; do not kill a process by name.
- missing Qwen tool: regenerate the project-scoped view and use the project
  wrapper.

A new MCP belongs here only with a real consumer, exact source, minimal schema,
permission/egress review, lifecycle health, failure isolation, redaction tests,
and a bounded end-to-end workflow.
