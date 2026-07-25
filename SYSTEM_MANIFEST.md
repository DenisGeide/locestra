# System Manifest

- `schema_version`: 1
- `manifest_status`: authoritative public mutable-facts registry
- `active_stage`: 007 unified Tool/Application Registry (planned)
- `stage_status`: Stages 000–006 are verified complete and published
- `checked_at`: 2026-07-25 public documentation snapshot
- `repository`: current Locestra checkout root, resolved at runtime
- `baseline_branch`: `main`
- `public_release_content`: current tracked content and release diff are privacy-sanitized
- `license`: `AGPL-3.0-only`

This manifest is the public source of truth for mutable component, route,
model-profile, lifecycle, and acceptance facts. It deliberately omits private
workstation paths, credentials, account state, local-only commit identifiers,
request identifiers, and internal implementation prompts. “Configured” never
means “verified end-to-end.”

Legacy public commits retain pre-existing author/committer email metadata.
Removing that metadata requires a separate, explicitly reviewed history
migration; this release does not rewrite public history.

## Stage status

| Stage | Capability | Public status |
|---|---|---|
| 000 | Governance, charter, permissions, manifest, validation | Verified complete |
| 001 | Architecture, contracts, health, lifecycle | Verified complete |
| 002 | Deterministic planner/router and routing evaluation | Verified complete |
| 003 | Controlled scoped Memory Engine | Verified complete |
| 004 | Scoped Knowledge/Archive Engine and Context Envelope | Verified complete |
| 005 | Hardened local Qwen/Codex Coding Engine | Verified complete |
| 006 | Managed MCP Hub | Verified complete |
| 007 | Unified Tool/Application Registry | Planned next |
| 008–011 | Voice, image, interfaces, controlled improvement | Planned |
| 012 | Broader versioned capability evaluation | Planned; initial routing EvalKit and CI already published |

## Components and boundaries

| Component | Responsibility | Public endpoint/storage | Current status |
|---|---|---|---|
| Open WebUI | Single-user UI for model `local-agent-auto` | Loopback port 3737 | Verified in platform acceptance; not a multi-user security boundary |
| Gateway | OpenAI-compatible ingress, normalization, planning, routing, execution, health | Host IPv4 port 8787 for Docker bridge access; `/v1/*` requires a runtime-generated bearer credential | Verified; production `local_code` uses the Coding Engine |
| Fast local model | Low-latency chat/auxiliary profile | Ollama loopback port 11435 | Reference profile runs CPU/RAM to preserve GPU residency |
| Strong local model | Reasoning/coding profile | Ollama loopback port 11434 | Reference profile runs on GPU; exact hardware is deployment-specific |
| Coding Engine | Versioned task state, isolated worktrees, Qwen/Codex adapters, verification, review, artifacts, optional local commit | `services/coding/`; ignored coding DB/runtime roots | Stage 005 verified complete; contract `1.0`, coding policy `2026-07-15.4` |
| Managed MCP Hub | Canonical MCP registry, policy, lifecycle, generated consumer views, health and payload-free audit | `config/mcp-registry.json`; `services/mcp_hub/`; ignored runtime roots | Stage 006 verified complete |
| Controlled Memory | Typed, scoped, inspectable records and retention | ignored SQLite storage; local CLI | Stage 003 verified complete |
| Scoped Knowledge | Source registration, repository map, FTS5/rg retrieval, provenance, Context Envelope | ignored SQLite storage; local CLI/class consumers | Stage 004 complete; Coding Engine consumer verified |
| Voice | faster-whisper OpenAI-compatible transcription service | Host IPv4 port 8788 for Docker bridge access; `/v1/*` requires the same runtime bearer | Existing short-audio foundation; durable Stage 008 pipeline remains planned |
| Browser | Policy-gated Playwright adapter | Per-request process | Public-target adapter verified; MCP exposure is fixture-only |
| Image | On-demand ComfyUI workflow | Loopback port 8388 when running | Installed reference capability; hardened Stage 009 workflow remains planned |
| Automation/interfaces | n8n and Telegram adapters | n8n loopback port 5678; adapter process | Existing foundation only; durable Stage 010 contract remains planned |

## Model and executor profiles

The public configuration defines role-based profiles rather than requiring a
specific workstation:

| Profile | Configured model/base | Endpoint/role/boundary |
|---|---|---|
| fast model `local-fast` | base `qwen3.5:4b` | Fast Ollama `http://127.0.0.1:11435`; routine chat and auxiliary work on the CPU/RAM reference profile |
| strong model `local-strong` | base `qwen3.6:35b` | Strong Ollama `http://127.0.0.1:11434`; strong reasoning on the GPU reference profile |
| Qwen Code local coding agent | `local-strong` | Stage 005 owned worktree/container boundary; coding profile has no MCP |
| Codex | `gpt-5.6-sol` | Optional cloud coding/review; explicit public-data approval only, otherwise local handoff |
| Voice / Whisper | `large-v3-turbo` | faster-whisper `http://127.0.0.1:8788`; existing CPU/int8 reference profile, with Stage 008 jobs/artifacts still planned |

Reference service endpoints remain:

| Service | Endpoint |
|---|---|
| Gateway | `http://127.0.0.1:8787` |
| Open WebUI | `http://127.0.0.1:3737` |
| n8n | `http://127.0.0.1:5678` |
| ComfyUI | `http://127.0.0.1:8388` |

Exact models, weights, quantization, hardware capacity, and upstream versions
can evolve behind these profiles. The checked-in public configuration, lock
files, and doctor checks are authoritative for a particular checkout.

## Routes

| Route | Executor | Boundary/status |
|---|---|---|
| `auxiliary` / `fast_chat` | fast local model | Local |
| `strong_chat` | strong local model | Local |
| `local_code` | Stage 005 Coding Engine with Qwen | Local owned worktree; verification/review required |
| `codex` | Codex only with explicit scoped approval | Cloud boundary; ordinary ingress fails closed to local handoff |
| `codex_bundle` | Versioned resumable handoff | Local artifact; readiness is not task success |
| `docs` | Qwen docs profile plus managed Context7 | External public-documentation boundary |
| `browser` | Direct policy-gated Playwright adapter | Public websites only |
| `image` | ComfyUI | Local/on-demand; broader workflow planned |
| `voice` | faster-whisper bridge | Local; durable long-job workflow planned |
| `vision` | typed degraded response when unavailable | No false success |

## Stage 005 acceptance

| Gate | Accepted evidence |
|---|---|
| Mandatory live matrix | 20 Qwen/Codex/Playwright and lifecycle cases passed |
| Historical full regression | `744 passed, 11 skipped` |
| Lifecycle and readiness | Stop/start and `DOCTOR_OK` |
| Production workflows | Gateway and Open WebUI coding smoke green; source repository and remote state preserved; owned worktree cleaned |
| Security/review | Secret scan and independent audit green with no unresolved P0/P1/P2 |

See [Coding Engine](docs/CODING_ENGINE.md).

## Stage 006 integrations and acceptance

| Integration | Version | Consumers | Locality/egress |
|---|---|---|---|
| Context7 | `3.2.3` | Documentation Qwen/gateway | External public-documentation egress |
| Playwright MCP | `0.0.78` | Hub doctor/UI fixture | Loopback fixture only |
| Local diagnostics | `1.0.0` | Platform Qwen/doctor | Local, no network |

Stage 006 exact-final evidence: MCP-only `97 passed`; expanded focused
`164 passed, 1 skipped`; full regression `855 passed, 12 skipped`; live
Context7 retrieval, Playwright title, and local diagnostics green;
`DOCTOR_OK`; `SMOKE_TEST_OK`; no owned MCP processes/orphans remained; secret
and dependency scans and independent audit were green.

Filesystem/shell/Git and generic memory MCPs were rejected as duplicative.
GitHub, ComfyUI, document, and messaging MCPs were deferred until a scoped
consumer and end-to-end workflow justify their permissions and data boundary.
See [MCP Hub](docs/MCP_HUB.md).

## Security-critical facts

- Services are intended for one trusted local operator. Do not publish them to
  a LAN or the Internet.
- Global Qwen/Codex profiles are not modified by the MCP Hub.
- Coding Qwen runs MCP-free in an owned, restricted task container.
- Context7 receives public documentation queries only. Repository/private data
  must not cross that boundary.
- Playwright MCP is restricted to a Hub-owned loopback fixture; public browsing
  remains a separate adapter.
- MCP audit records never contain tool arguments/results or content payloads.
- Runtime credentials, databases, worktrees, logs, generated views, models, and
  artifacts remain ignored by Git.
- Codex cloud execution requires explicit scoped permission and public
  classification; availability or login is not permission.

## Canonical operations

```powershell
./scripts/bootstrap.ps1
./scripts/start.ps1
./scripts/doctor.ps1
./scripts/smoke-test.ps1
./scripts/stop.ps1
```

Focused MCP operations are documented in [MCP Hub](docs/MCP_HUB.md); coding
recovery and inspection are documented in [Coding Engine](docs/CODING_ENGINE.md).

## Known limitations

- This alpha is not hardened for hostile multi-user use.
- Voice and gateway listeners still require deployment-level network review.
- Full actor/workspace allowlists and a general cloud approval ledger remain
  incomplete.
- Optional upstream tools execute with their declared host authority; the MCP
  Hub is not an OS sandbox.
- Host or daemon crashes may leave stale ownership evidence for doctor/recovery.
- Existing image, voice, Telegram, and n8n foundations are not equivalent to
  completed Stages 008–010.
- The initial routing EvalKit does not complete the broader Stage 012 program.

See [Current State](docs/CURRENT_STATE.md), [Roadmap](docs/ROADMAP.md), and
[Security Model](docs/SECURITY_MODEL.md).
