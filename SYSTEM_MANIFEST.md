# System Manifest

- `schema_version`: 1
- `manifest_status`: authoritative mutable-facts registry
- `active_stage`: 004 scoped knowledge and archive engine
- `stage_status`: complete; scoped live index, full regression, foundation validator, doctor and smoke gates are green; gateway/Qwen consumer wiring remains Stage 005
- `checked_at`: 2026-07-16 public snapshot
- `repository`: the root of the current Locestra checkout
- `baseline_branch`: `main`
- `public_history`: fresh privacy-sanitized root snapshot
- `architecture_revision`: the Git commit containing this manifest
- Владелец: владелец платформы.
- Изменение: только вместе с evidence соответствующего runtime/config change; секреты запрещены.

Этот файл — единый источник истины для изменяемых component/endpoint/model/tool/lifecycle facts. Нормативные правила находятся в [Конституции](constitution/CORE.md), а подробный снимок — в [CURRENT_STATE.md](docs/CURRENT_STATE.md). `Configured` не означает `verified end-to-end`.

## Components and endpoints

| ID / component | Responsibility/profile | Endpoint | Status at checked_at | Evidence/source |
|---|---|---|---|---|
| Open WebUI | Единый UI, model `local-agent-auto` | http://127.0.0.1:3737 | Verified: 200, version 0.10.2; Docker port published on loopback only; live fast prompt returned `LOCAL_UI_OK`; read-only repository prompt returned the README heading | `/health`, `/api/version`, Docker container, browser E2E |
| Gateway | Entry + deterministic Normalizer/Planner/Router + optional scoped memory retrieval + execution compatibility control plane, OpenAI-compatible `/v1` | http://127.0.0.1:8787 | All `/v1/*` require a runtime-generated bearer credential; Stage 003 offline/live regression: 292 tests, fixed routing corpus 117/117, `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK` | middleware/config, full/targeted pytest, validator, runtime gate |
| Fast Ollama | `local-fast`, CPU/RAM chat profile | http://127.0.0.1:11435 | Verified: API/model loaded | `/api/version`, `/api/ps`, Modelfile |
| Strong Ollama | `local-strong`, GPU reasoning/coding profile | http://127.0.0.1:11434 | Verified: API/model loaded | `/api/version`, `/api/ps`, Modelfile |
| Qwen Code local coding agent | model `local-strong`; filesystem/terminal/Git for code, Context7-only profile for docs | http://127.0.0.1:11434/v1 | Verified disposable Git workspace edit through gateway; read-only README inspection through Open WebUI | smoke, browser E2E, `qwen --version`, immutable profiles |
| Voice / Whisper | faster-whisper `large-v3-turbo`, CPU/int8; standalone API + bounded gateway chat bridge | http://127.0.0.1:8788 | Standalone health/model verified; chat execution unit-tested, current-revision live E2E pending | `/health`, gateway contract tests |
| n8n | Automation UI/runtime | http://127.0.0.1:5678 | Verified health/readiness, version 2.30.4; Docker port published on loopback only | `/healthz`, container CLI |
| ComfyUI | On-demand image generation | http://127.0.0.1:8388 | Installed: portable runtime/checkpoint verified; stopped in idle; generation not tested in stage audit | doctor and exact path audit |
| Telegram adapter | Text/voice/photo ingress and reply | none | Discovered, not verified; credential state intentionally unread | `services/telegram/bot.py` |
| Browser adapter | Playwright Chromium navigation with public-target DNS/redirect/subrequest policy | process/stdio | Local fixture, policy tests and live external `https://example.com` → `Example Domain` verified | browser adapter, routing tests and gateway E2E |
| Task journal | SQLite legacy projection + privacy-filtered new writes + versioned `TaskStateV1` decision/plan/attempt snapshot | `data/memory.sqlite3` | Schema v3 preserves 190 historical rows as `legacy_payload=1`; post-smoke state has 5 bounded/redacted `stage003-v1` rows; separate scoped legacy purge exists | task-state, privacy and migration tests |
| Controlled Memory Engine | Explicit typed user/project/task/operational/archive-reference records, provenance, conflicts, retention, audit, CLI and bounded Planner retrieval | `data/memory.sqlite3`; `python -m services.memory.cli` | Record schema `1.0`, DB schema v3, 0 imported records; content injection is local-code only, docs performs no retrieval, Codex keeps opaque IDs only; strict archive metadata, owner-only storage/export ACL and success/error diagnostics verified; management is CLI-only; no archive import and no vector DB | `services/memory/`, `docs/MEMORY_ENGINE.md`, Stage 003 tests |
| Scoped Knowledge Engine | Explicit project/source registration, archive adapters, incremental Git-tracked indexing, Repository Map v1, FTS5/rg retrieval, Context Envelope v1, invalidation/conflicts and coordinated source purge | `data/knowledge.sqlite3`; `python -m services.knowledge.cli` | Contract `1.0`, DB schema v1, policy `2026-07-15.2`; Stage 004 complete; approved gate snapshot: 143 tracked, 128 indexed, 15 blocked, 129 active sources and 1,384 active fragments; no real external archive imported; CLI-only/single-user boundary, gateway/Qwen consumer wiring pending Stage 005 | `services/knowledge/`, `config/knowledge.json`, `docs/KNOWLEDGE_ENGINE.md` |
| Config resolvers | Platform precedence plus separate immutable versioned routing policy | process-local | Platform resolver verified; routing schema `1.0`, policy `2026-07-14.1`, no env/secrets, LLM signal disabled | `services/config.py`, `services/orchestration/config.py`, config tests |
| Core contracts | Pydantic v1 NormalizedRequest/Plan/MemoryContextItem/RouteDecision/ExecutionAttempt/ToolSpec/TaskState/ArtifactMetadata | process-local | Optional provenance-preserving memory refs/context added without changing contract version; invariants and runtime use verified | `services/contracts/v1.py`, contract/execution/memory tests |
| Lifecycle ownership | Actual listener PID + versioned owner metadata | `run/*.pid`, `run/*.owner.json` | Verified start/readiness/stop/no-orphan/restart; sibling-root collision rejected; unowned fast listener fail-closed; false stop-success markers removed | lifecycle gate, regression test and scripts |

Open WebUI and n8n Docker ports are explicitly published on `127.0.0.1`. Gateway/voice processes still bind `0.0.0.0` so containers can reach host services; gateway `/v1/*` requires the generated bearer credential, while voice has no equivalent authentication boundary. See [Security Model](docs/SECURITY_MODEL.md).

## Model profiles

| Profile | Base model | Backend/endpoint | Context/device policy | Observed state 2026-07-14 | Config source |
|---|---|---|---|---|---|
| `local-fast` | `qwen3.5:4b`, 4.7B Q4_K_M | Fast Ollama http://127.0.0.1:11435 | 8192, `num_gpu=0`, CPU/RAM | Loaded; `size_vram=0` | `models/fast.Modelfile`, `.env.example` |
| `local-strong` | `qwen3.6:35b`, 36.0B Q4_K_M | Strong Ollama http://127.0.0.1:11434 | 32768, GPU | Loaded; 23747822592 bytes VRAM | `models/strong.Modelfile`, `.env.example` |
| Qwen Code coding agent | `local-strong` | OpenAI-compatible http://127.0.0.1:11434/v1 | context 32768; executable-plan input ceiling 6000, plan reserve 4000, model output ceiling 4096 | Configured; CLI 0.19.10; code/docs profiles copied into ignored writable runtime homes | `config/qwen-code/settings.json`, `config/qwen-docs/settings.json` |
| Codex | `gpt-5.6-sol`, reasoning `high` | Cloud via Codex CLI | `workspace-write` default; review `read-only` | CLI 0.144.1/login verified; exec not audited | `.env.example`, gateway code |
| Whisper | `large-v3-turbo` | faster-whisper http://127.0.0.1:8788 | CPU/int8 | Loaded | public config and `/health` |
| SDXL Turbo | local checkpoint, 6938081905 bytes | ComfyUI http://127.0.0.1:8388 | Shares GPU on demand | Installed; semantic generation not tested | doctor/path audit |

## Routes

| Route | Executor/capability | Data boundary | Current evidence |
|---|---|---|---|
| `auxiliary` | `local-fast` | Local | Route verified by smoke |
| `fast_chat` | `local-fast` | Local | Semantic non-stream/stream/tool-call E2E verified |
| `strong_chat` | `local-strong` | Local | Route/model readiness verified; independent quality not benchmarked |
| `local_code` | Qwen Code + `local-strong` | Local workspace | Disposable workspace edit via gateway and read-only README inspection via Open WebUI verified |
| `codex` | `codex_bundle` unless separate scoped approval is injected | Local handoff; cloud only after approval | Current ingress fail-closed with typed 409; CLI/login verified, exec not run |
| `codex_bundle` | Create-exclusive bounded/redacted Markdown handoff | Local inbox | Idempotency/content/redaction unit-tested; это ready artifact, не completed task |
| `docs` | Qwen Code + Context7 | External documentation input; always neutral runtime workspace | Live FastAPI lifespan retrieval verified through gateway; dependency remains mutable `@latest` |
| `browser` | Playwright | Public external URL input | Literal private/local blocked by Router; DNS/redirect/subrequest checks in adapter; live `Example Domain` E2E green |
| `image` | ComfyUI/SDXL Turbo | Local | Runtime/checkpoint installed; on-demand generation not tested in stage audit |
| `voice` | Whisper bridge to `/v1/audio/transcriptions` | Local | Bounded inline chat audio execution unit-tested; standalone voice health verified; current-revision E2E pending |
| `vision` | explicit degraded response | Local | Executor unavailable; no false success |

## Toolchain and integrations

| Tool | Verified version/state | Source/evidence |
|---|---|---|
| Project Python | 3.12.13 | `.venv\Scripts\python.exe --version` |
| uv | 0.10.11 | `uv --version` |
| Git | 2.52.0.windows.1 | `git --version` |
| Docker engine/Desktop | 29.4.2 / 4.72.0 | Docker info/version |
| Node.js / npm | 24.14.0 / 11.9.0 | CLI versions |
| Ollama | 0.22.1 | both `/api/version` endpoints |
| Qwen Code | 0.19.10 | CLI version |
| Codex CLI | 0.144.1 observed on the reference host | Account state and credentials are not part of the public snapshot |
| Context7 MCP | Connected via `@upstash/context7-mcp@latest` | `qwen mcp list`; mutable dependency |
| Playwright MCP | Connected via `@playwright/mcp@latest`; package 0.0.78 | Qwen MCP list/npm; mutable dependency |
| Playwright | 1.62.0-alpha-1783623505000; Chromium installed | npm/path audit |
| Open WebUI image | tag `main`, observed image revision `ecd48e2f...` | Docker inspect/API |
| n8n image | tag `latest`, observed version 2.30.4 | Docker inspect/CLI |

## Security-critical configured flags

| Setting | Configured value/source | Enforcement status |
|---|---|---|
| `ENABLE_LOCAL_CODE_EXEC` | `true` default in `.env.example` | Qwen Code can mutate selected workspace; no central allowlist. |
| `ENABLE_CODEX_EXEC` | `true` default in `.env.example` | Capability only. Current ingress still requires separate scoped approval and otherwise creates local bundle. |
| `CODEX_SANDBOX` | `workspace-write` default; review path uses read-only | Codex CLI sandbox only; cloud data boundary separate. |
| Qwen approval | Runtime `plan` for read-only, `yolo` for write; config `approvalMode=auto` | Write permission ceiling and OS-level sandbox not technically enforced. |
| Open WebUI auth | `WEBUI_AUTH=false` | Single-user convenience behind loopback-only Docker publishing; it is not an independent multi-user identity boundary. |
| Gateway `/v1` auth | 256-bit credential generated in ignored `run/gateway-api-key.txt`; passed at runtime to trusted clients | Bearer authentication is enforced before routing, memory retrieval or execution; key DACL is protected current-user-only; one shared local credential, no per-actor scopes. |
| Gateway/voice bind | `0.0.0.0` in start script | Gateway `/v1` is authenticated; voice and non-`/v1` endpoints do not share that boundary, and listeners can accept external interfaces. |
| Docker port publishing | Open WebUI and n8n explicitly use `127.0.0.1` host bindings | UI/automation ports are loopback-only; host firewall still matters for gateway/voice listeners. |

## Data and runtime directories

| Path | Purpose | Git/retention state |
|---|---|---|
| `services/` | Source for gateway, voice, browser, Telegram | Tracked |
| `scripts/` | Existing lifecycle, diagnostics and module scripts | Tracked |
| `config/qwen-code/settings.json` | Immutable code-agent profile without repository-provided MCP/extensions/hooks | Tracked; no credential values; copied to ignored writable runtime home |
| `config/qwen-docs/settings.json` | Immutable documentation profile with Context7 only | Tracked; no credential values; copied to ignored writable runtime home |
| `config/routing.json` | Immutable deterministic routing thresholds/rules/policy version | Tracked; strict non-secret schema; no `.env` override |
| `data/` | SQLite task journal + controlled memory schema v3 + ignored verified migration backups | Ignored except placeholder; directory, DB/WAL/SHM and backups use protected current-user-only DACL; 190 legacy rows quarantined, new task writes filtered, memory export/delete/retention available; backup still needs separate retention |
| `data/knowledge.sqlite3` | Separate rebuildable Knowledge Engine source catalog, generations, fragments/FTS, facts/conflicts and repository maps | Ignored derived database; approved gate snapshot has 129 active sources/1,384 active fragments and a fresh Repository Map for 143 tracked/128 indexed/15 blocked paths; exact-source purge/compaction implemented; originals and external/OS copies have separate retention |
| `archives/` | Explicit local archive drop zone inside the registered project | Contents ignored except `.gitkeep`; never scanned or imported automatically; each file requires exact registration, consent and privacy checks |
| `knowledge/` | Human-readable source-backed knowledge inventory and operating summaries | Tracked; no secret/personal archive content; unverified fields are labelled, and files do not become active Memory Engine records |
| `inbox/` | Codex handoff bundles containing task prompt/context | Ignored except placeholder; redaction/TTL/delete incomplete |
| `outputs/` | Generated user artifacts served by gateway `/outputs` | Ignored except placeholder; retention/access control incomplete |
| `logs/` | Process logs | Ignored except placeholder |
| `run/` | PID/owner runtime state, neutral `docs-workspace` and writable `qwen-homes/{qwen-code,qwen-docs}` copies | Ignored except placeholder; runtime copies are refreshed from committed profiles; docs workspace is never a project permission grant |
| `modules/` | Large optional local runtimes/models | Ignored; ComfyUI portable and SDXL Turbo installed |
| `prompts/` | Versioned staged implementation prompts | Tracked foundation artifact; stages 000–012 validated |
| `%TEMP%\local-agent-*.txt` | Codex last-message output | Outside repo; removed in adapter `finally`, but abrupt process/OS termination can still leave residue |
| `run/*.owner.json` | Versioned process identity, actual PID/port/root/fragments/start time | Ignored runtime evidence; removed after verified stop |
| Docker volume `local_agent_open_webui_data` | Open WebUI history/settings | Persistent local volume; auth/export/delete policy incomplete |
| Docker volume `local_agent_n8n_data` | n8n workflows/executions/credentials metadata | Persistent local volume; managed by n8n, backup/delete policy incomplete |
| `.env` | Runtime credential/config channel | Ignored and intentionally unread by stage audit; never model/log/manifest content |

`.env`, tokens, cookies, keys, browser profiles and credential stores are never manifest content.

## Lifecycle commands

| Action | Canonical command | Verification |
|---|---|---|
| Bootstrap/update dependencies | `powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1` | Only when installation/update is intended |
| Start | `powershell -ExecutionPolicy Bypass -File scripts/start.ps1` | Registers actual owned listener/process identity and waits for core readiness/voice/UI/n8n |
| Stop | `powershell -ExecutionPolicy Bypass -File scripts/stop.ps1` | Revalidates identity/start-time/port, stops only owned local workers, preserves strong Ollama and volumes |
| Diagnose | `powershell -ExecutionPolicy Bypass -File scripts/doctor.ps1` | Includes foundation validator |
| Smoke test | `powershell -ExecutionPolicy Bypass -File scripts/smoke-test.ps1` | Semantic gateway/tool/browser/voice/Qwen coding checks |
| Unit tests | `uv run pytest` | Python suite |
| Memory management | `uv run python -m services.memory.cli status` | Deliberate CLI-only local operator boundary for scoped list/search/show/add/confirm/reject/edit/delete/purge/export/retrieve/retention/backup/restore; no HTTP memory management API |
| Knowledge management | `uv run python -m services.knowledge.cli status` | CLI-only explicit import/index/retrieve/map/rg/context/candidate/purge/compact boundary; import/index require `--approved`, destructive apply requires exact source ID |
| Image smoke | `powershell -ExecutionPolicy Bypass -File scripts/image-smoke-test.ps1` | Heavy optional semantic check; not required for core Stage 001 gate |

## Governance and ADR

- [Project Charter](docs/PROJECT_CHARTER.md)
- [Current Architecture](docs/ARCHITECTURE.md)
- [Target Architecture](docs/TARGET_ARCHITECTURE.md)
- [Contracts](docs/CONTRACTS.md)
- [Operations](docs/OPERATIONS.md)
- [Configuration](docs/CONFIGURATION.md)
- [Routing](docs/ROUTING.md)
- [Memory Engine](docs/MEMORY_ENGINE.md)
- [Knowledge Engine](docs/KNOWLEDGE_ENGINE.md)
- [Archive Import Plan](docs/ARCHIVE_IMPORT_PLAN.md)
- [Permissions](docs/PERMISSIONS.md)
- [Security Model](docs/SECURITY_MODEL.md)
- [Roadmap](docs/ROADMAP.md)
- [ADR 0001: governance/evidence](docs/adr/0001-governance-and-evidence.md)
- [ADR 0002: local-first/Codex boundary](docs/adr/0002-local-first-and-codex-boundary.md)
- [ADR 0003: permissions/protected change](docs/adr/0003-permissions-and-protected-change.md)
- [ADR 0004: versioned boundary contracts](docs/adr/0004-versioned-boundary-contracts.md)
- [ADR 0005: configuration and health semantics](docs/adr/0005-configuration-and-health-semantics.md)
- [ADR 0006: process ownership and resource boundaries](docs/adr/0006-process-ownership-and-resource-boundaries.md)
- [ADR 0007: deterministic planner and router](docs/adr/0007-deterministic-planner-router.md)

## Known limitations

Canonical current limitations are listed in [CURRENT_STATE.md](docs/CURRENT_STATE.md). Highest-priority gaps: shared-credential gateway plus unauthenticated voice/all-interface listeners and Qwen write-mode `yolo`/arbitrary workspace, missing scoped Codex approval ledger and Telegram policy, incomplete outbound/browser hardening, quarantined/other-application payload outside controlled memory, no forensic erase guarantee, mutable upstream execution, Stage 004 Context Envelope not yet wired into gateway/Qwen, no durable event/cancellation recovery and in-process-only resource locks. Knowledge retrieval is lexical/structured without vector eval and current management is CLI-only/single-user.
