# Target Architecture

- Статус: target design updated through verified Stage 005 Coding Engine and
  Stage 006 Managed MCP Hub. Stage 007 Tool/Application Registry is the next
  planned boundary.
- Владелец: владелец платформы.
- Scope: стабильные module, data, failure, resource and replacement boundaries для одной local-first workstation.
- Current implementation: [Current Architecture](ARCHITECTURE.md).
- Изменение: architectural decision требует ADR, compatibility analysis и обновления contract tests.

## Цель

Целевая система сохраняет один пользовательский entry point и OpenAI-compatible /v1, но отделяет transport от решения и выполнения. Замена модели, agent CLI, browser adapter, storage implementation или UI не должна требовать переписывания остальных слоёв.

Это не предложение сразу превратить платформу в набор сетевых микросервисов. Базовая форма — **modular monolith внутри gateway process** со строгими typed contracts; отдельными процессами остаются только зрелые runtimes и adapters, которым действительно нужна process isolation: Ollama, Qwen/Codex CLI, Whisper, browser, ComfyUI, Open WebUI и n8n.

## Неподвижные compatibility constraints

1. Open WebUI продолжает видеть model local-agent-auto.
2. Gateway сохраняет порт 8787 и OpenAI-compatible /v1.
3. Open WebUI остаётся на 3737; voice — 8788; n8n — 5678.
4. Strong Ollama остаётся на 11434/GPU, fast Ollama — 11435/CPU/RAM.
5. ComfyUI остаётся on-demand на 8388 и использует coordinated GPU lease.
6. Qwen Code, Codex review/exec, Context7, Playwright, Whisper и ComfyUI заменяются только за adapter contracts.
7. Existing workspaces, SQLite data, inbox, outputs, workflows, Docker volumes и Git history мигрируются без destructive reset.
8. Local-first не скрывает внешние boundaries: Codex, Context7, browser, Telegram и n8n webhooks проходят policy и provenance.

## Целевая схема

~~~mermaid
flowchart LR
  subgraph EntryBoundary["Entry and transport boundary"]
    WebUI["Open WebUI adapter<br/>/v1 compatibility"]
    API["Direct API adapter"]
    Telegram["Telegram adapter"]
    N8N["n8n/webhook adapter"]
  end

  subgraph Core["Gateway modular core"]
    Entry["Entry Layer"]
    Normalize["Request Normalizer<br/>NormalizedRequest v1"]
    Planner["Planner<br/>Plan v1"]
    Router["Router<br/>RouteDecision v1"]
    Policy["Permissions / approval gate"]
    Execute["Execution Engine"]
    Review["Reviewer boundary"]
    Response["Response adapter<br/>OpenAI JSON/SSE"]
    Health["Health aggregator"]
    Resources["Resource coordinator"]
  end

  subgraph Registries["Replaceable capability boundaries"]
    Tools["Tool Registry<br/>ToolSpec v1"]
    Models["Model/Profile Registry"]
    ExecAdapters["Executor adapters<br/>chat / Qwen / Codex"]
  end

  subgraph Data["Separated storage boundaries"]
    Tasks[("Task Store<br/>TaskState v1 + events")]
    Artifacts[("Artifact Store<br/>ArtifactMetadata v1")]
    Memory[("Memory Engine<br/>implemented stage 003")]
    Knowledge[("Knowledge Engine<br/>stage 004 implementation")]
  end

  subgraph Runtime["Existing external runtimes"]
    Fast["Fast Ollama :11435"]
    Strong["Strong Ollama :11434"]
    Voice["Whisper :8788"]
    Browser["Playwright adapter"]
    Comfy["ComfyUI :8388"]
    Qwen["Qwen Code"]
    Codex["Codex CLI / cloud"]
    Context7["Context7 MCP"]
  end

  WebUI --> Entry
  API --> Entry
  Telegram --> Entry
  N8N --> Entry
  Entry --> Normalize --> Planner --> Router --> Policy
  Policy -->|approved or local-safe| Execute
  Policy -->|approval required| Tasks
  Execute <--> Tools
  Execute <--> ExecAdapters
  Execute <--> Resources
  Execute --> Tasks
  Execute --> Artifacts
  Execute --> Review
  Review --> Tasks
  Review --> Response
  Execute --> Response
  Response --> Entry

  Planner -.-> Memory
  Planner -.-> Knowledge
  Execute -.-> Memory
  Execute -.-> Knowledge
  Health --> Tools
  Health --> ExecAdapters
  Router --> Models

  ExecAdapters --> Fast
  ExecAdapters --> Strong
  ExecAdapters --> Qwen
  ExecAdapters --> Codex
  Tools --> Voice
  Tools --> Browser
  Tools --> Comfy
  Tools --> Context7
~~~

Dashed links show optional consumers, not permission grants. Scoped Memory
retrieval is connected to allowed local-code paths. The Stage 005 Coding Engine
is the verified automatic consumer of Knowledge Context Envelope for isolated
coding worktrees; this still does not imply repository retrieval in every
chat/docs request.

## Module contracts и replacement boundaries

| Module | Responsibility | Stable input | Stable output | Storage ownership | Failure contract | Replacement boundary | Stage owner |
|---|---|---|---|---|---|---|---|
| Entry Layer | Transport auth, request size, source identity, idempotency, OpenAI compatibility, response transport | HTTP/OpenAI, Telegram update или workflow payload | RawIngressRequest в Normalizer; rendered response клиенту | Не хранит model/task memory; transport audit refs only | 4xx для invalid/auth; 429 overload; stable error envelope; disconnect/cancel signal | UI или interface adapter знает только Entry contract, не models/tools | Contract 001; durable interfaces/auth 010 |
| Request Normalizer | Один раз canonicalize text, attachments, source, project resolution, correlation and leading override | RawIngressRequest | NormalizedRequest v1 или validation failure | Не хранит binary; positional attachment refs only | Invalid explicit project не получает default; override conflict blocked downstream | Не знает executors/models; project resolver и attachment adapter подменяемы | 001; override/resolution 002 implemented |
| Planner | Извлечь bounded intent/action/complexity/risk и при необходимости subtasks, acceptance, tools, constraints and verification; fast/aux path plan не создаёт | NormalizedRequest v1 | PlanningResult: signals + optional Plan v1 | Plan сохраняется через Task Store при execution | Не выполняет tools; missing scope решает Router/Policy | Deterministic implementation заменяема за PlanningResult/Plan contracts | Contract 001; implementation/evals 002 implemented |
| Router | Детерминированно выбрать route, executor/profile, locks, fallback and reason codes | NormalizedRequest v1, planning result, capability/permission/failure facts | RouteDecision v1 | Decision сохраняется в TaskState snapshot | unavailable/degraded/approval-required как typed decision; никаких side effects | Model names/endpoints скрыты за profile ids; Entry не меняется | Contract 001; implementation 002 implemented |
| Permissions / approval gate | Enforce workspace/data/recipient/action ceiling before execution or external transfer | Request, Plan, RouteDecision, actor/session policy | ApprovedExecution, ApprovalRequired или Denied | Approval ledger с scope/expiry/provenance, без secrets | Stage 002 fail-closes Codex without scoped approval, critical local override and private browser target; общего ledger ещё нет | Executor не может расширить decision; policy implementation заменяема | Partial 002; full 005, 007, 010 |
| Execution Engine | Orchestrate attempts, cancellation, timeouts, locks, tools, executors, artifacts and verification | ApprovedExecution + Plan + RouteDecision | TaskState transitions, Artifact refs, ExecutionResult | Task Store events/snapshot; Artifact Store refs | Classified Failure v1; bounded retry/fallback; process termination on cancel where supported | Qwen/Codex/chat/tool adapters реализуют отдельный interface | Boundary 001; coding 005; tools 006–007 |
| Tool Registry | Единственный каталог реальных capabilities, schemas, permissions, health and locality | Tool lookup/call по ToolSpec v1 | Typed ToolResult или Failure v1 | Spec/version registry и bounded audit refs | unavailable/degraded изолирует capability; retry только по spec | Browser, voice, image, MCP и будущие apps заменяются по ToolSpec | Contract 001; MCP 006; unified registry 007 |
| Memory Engine | Typed preferences/project facts/task memory с provenance, CRUD/export/delete and scope | Memory query/write command с project/user scope | Bounded MemoryRecord refs | Versioned additive schema v3 в controlled memory DB | Stale/conflict status; fail closed across project boundary | Planner/executor зависят от query API, не SQLite tables | Implemented 003; management CLI-only |
| Knowledge Engine | Scoped import/index/retrieval/invalidation for repository/docs/archive and bounded Context Envelope | Source registration/query with exact owner/project, allowlist, budget and freshness | Ranked untrusted/local-only evidence refs, Repository Map v1, Context Envelope v1 | Separate SQLite schema v1: source versions/generations, fragments/FTS5, facts/conflicts, maps/audit | Missing/stale/privacy-invalid evidence excluded/degraded; generation publish CAS; no fabricated context | FTS/vector/reranker backend replaceable behind retrieval contract; Stage 005 coding consumer is verified | Stage 004 complete; coding consumer complete in Stage 005 |
| Reviewer | Независимо сопоставить result/diff/tests с acceptance и risk | Plan, RouteDecision, TaskState, artifacts/evidence | ReviewDecision: accepted, changes_required, blocked | Review event/evidence refs | Self-claim executor не принимается; review unavailable не превращается в success | Codex review/local deterministic gates заменяемы | Boundary 001; coding enforcement 005 |
| Artifact Store | Register bounded files/tool outputs with hash, provenance, retention and access policy | Artifact create/register/read/delete commands | ArtifactMetadata v1 and content stream/ref | outputs/inbox/temp migration targets; metadata store | Hash mismatch, missing/expired/forbidden as typed failure | Filesystem/object backend скрыт; request/task JSON хранит только refs | Contract 001; progressive implementation 003, 008–010 |
| Task Store | Durable state snapshot; target также требует append-only transition/evidence records | Valid state update/event | TaskState v1 snapshot; target history/query | SQLite initially; versioned migrations | Stage 002 хранит structured attempts в snapshot; compare-and-set/crash recovery ещё нет | SQLite repository replaceable without changing core contracts | 001 snapshot; route/attempt fields 002; events later |
| Health Aggregator | Separate process liveness, platform readiness and per-capability state | Adapter health probes and config | HealthSnapshot with evidence timestamp | Последний bounded snapshot optional | Probe failure degrades only dependent capability | /health aggregate remains compatible; adapters expose common health result | 001 |
| Resource Coordinator | Lease GPU-heavy, worktree, per-agent and outbound-action resources | required_locks from RouteDecision | Lease set or queued/timeout result | Lease owner/request/timestamps; interprocess backend later | Timeout/cancel/release guaranteed; orphan lease recoverable | In-process implementation can move to OS/SQLite lease without caller changes | Contract 001; hardening 005, 009, 010 |

## Versioned contracts

Единственный реализованный машинный источник v1 — [services/contracts/v1.py](../services/contracts/v1.py); [Contracts](CONTRACTS.md) документирует его без второго schema definition. Таблицы ниже намеренно повторяют только фактические поля/семантику v1. Любое target-обогащение сначала получает compatibility review: optional backward-compatible addition может остаться в major v1, а новое required field, enum semantics или transition model требует новой major version и migration adapter. Наличие schema не означает policy enforcement.

### NormalizedRequest v1

Минимальные поля:

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| request_id | Generated once at first trusted entry; immutable |
| user_message | Normalized text, bounded by configured limit |
| attachments | Array of ArtifactRef metadata; никаких inline unbounded binary/base64 |
| source | open_webui, api, telegram, n8n или internal |
| project_hint | Optional bounded resolved reference или null |
| explicit_route | Optional compatibility route projection для применимых overrides |
| created_at | UTC timestamp |
| correlation_id | Stable across retries, adapters and response headers |
| routing_override / override_conflict | Leading typed override и conflict flag; не permission bypass |
| project_resolution | Typed source/status: explicit/default/none и resolved/invalid/missing |

Target-кандидаты locale, actor/session reference, idempotency и data classification не являются частью v1 до schema review. Credentials, cookies и raw secret values запрещены при любой версии.

### Plan v1

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| goal | Проверяемая формулировка результата |
| subtasks | Ordered bounded text steps; dependency graph в v1 отсутствует |
| tools | Requested capability names, не executable shell strings |
| acceptance_criteria | Objective conditions/artifacts |
| risk | Enum low/medium/high/critical; factors/data-boundary envelope в v1 отсутствует |
| approvals | Required scopes/actions/recipients, даже если список пуст |
| verification_plan | Checks/evidence expected before success |
| context_budget | Token/byte/tool-output limits and compression policy |
| request_id | Optional связь с NormalizedRequest |
| action / complexity | Optional typed planning classification |
| constraints | Bounded explicit user constraints, включая read-only запреты |

Runtime deterministic Planner этапа 002 создаёт `PlanV1` для non-fast задач; fast chat и auxiliary используют `plan=null`. Plan остаётся untrusted proposal до Router/Policy и не является разрешением на tool или cloud action. Provenance refs остаются target addition.

### RouteDecision v1

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| route | auxiliary, fast_chat, strong_chat, local_code, codex, codex_bundle, docs, browser, image, voice или vision |
| executor | Adapter id, а не command line |
| model / profile | Logical model и profile ids; endpoint mutable facts не копируются в client request |
| reason_codes | Machine-readable ordered reasons |
| risk | Effective risk/data-boundary decision |
| fallback | Optional один typed route/executor/reason envelope |
| project | Optional bounded resolved project reference; schema не выдаёт permission |
| required_locks | Bounded unique logical resource keys; lock mode в v1 отсутствует |
| policy_version | Versioned decision semantics |
| action / complexity / execution_mode | Typed intent and effective read-only/write/none mode |
| requested_route / override_disposition | Requested preference и applied/rejected/none result |
| decision_status / permission_disposition | ready/degraded/blocked и allowed/approval-required/denied |
| capability / capability_status / checked_at | Availability evidence, не hidden readiness assumption |
| blocking_reason_codes / max_attempts | Explicit gate reasons и bounded attempt ceiling |

Target-кандидаты decision_id и immutable plan/artifact refs ещё не входят. Stage 002 runtime сохраняет полный decision и plan в TaskState snapshot; отдельный approval ledger остаётся target.

### ToolSpec v1

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| name / version | Stable capability identity and implementation version |
| input_schema / output_schema | Bounded JSON-serializable schema objects без inline data |
| health | Probe kind/target/timeout contract; last observation хранится отдельно |
| permissions | Required actions, data classes and recipients |
| risk | Resource/security risk labels |
| timeout_seconds | Bounded default/hard timeout; отдельного cancellation field в v1 нет |
| retry | Retryable categories, max attempts and backoff |
| availability | available, degraded, unavailable, disabled или on_demand |
| locality | local, cloud или hybrid |

Command path, secret source and runtime endpoint являются adapter configuration, а не model-visible ToolSpec data.

### TaskState v1

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| status | pending, ready, running, blocked, complete, failed или cancelled |
| attempts | Bounded count 0–100; running/complete/failed требуют минимум одну попытку |
| executor | Optional selected executor enum |
| project / worktree | Optional bounded refs; ownership/allowlist проверяет отдельная policy |
| artifacts | До 128 `ArtifactMetadataV1` metadata objects; binary content остаётся вне state JSON |
| modified_files | Bounded unique references; relative-path enforcement относится к workspace policy |
| unresolved_errors | Bounded text summaries; failed требует непустой список |
| next_action | Concrete retry, approval, user input, rollback or terminal action |
| route / route_decision / plan | Desired versioned control decision и bounded plan |
| model / profile / fallback_used | Фактически выбранные execution facts |
| attempt_history | До 100 structured `ExecutionAttemptV1`: executor/model/outcome/reasons/commands/errors/files/artifacts/timestamps |

Текущая persistence хранит только последний `TaskStateV1` snapshot, но Stage 002 сохраняет route decision, plan и structured attempt history внутри него. Append-only transition record, previous version/actor/evidence и richer phases (`received`, `normalized`, `planned`, `routed`, `queued`, `verifying`) остаются target TaskEvent/new-version design. Текущий terminal enum называется `complete`; право выставить его после acceptance/review будет усилено последующими gates.

### ArtifactMetadata v1

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| type | Bounded non-empty short name; registry semantics ещё не enforced |
| path | Bounded reference без inline data; store-relative/access enforcement ещё является Artifact Store policy |
| hash | SHA-256 content digest в v1 |
| producer | Tool/executor/version |
| source_request | request_id/correlation reference |
| created_at | UTC timestamp |
| provenance | Input artifacts/source/tool evidence chain |
| retention | Class, expiry, export/delete policy |

Metadata может содержать bounded size/media type/access classification. Binary content и unbounded logs не помещаются в request/task JSON.

### Target Failure contract (ещё не реализован)

Будущий versioned failure envelope нужен, чтобы transport не маскировал ошибку текстом успешного assistant response. Он не является частью текущего machine v1:

- category: validation, permission, unavailable, timeout, cancelled, tool, executor, verification, storage или internal;
- retryable и retry_after;
- public_message без secrets;
- internal_evidence_ref вместо полного raw stderr;
- component, attempt and correlation_id;
- fallback eligibility.

## Target request lifecycle

~~~mermaid
sequenceDiagram
  actor User
  participant Entry
  participant Normalizer
  participant Planner
  participant Router
  participant Policy
  participant Engine
  participant Registry
  participant Executor
  participant Reviewer
  participant Stores

  User->>Entry: OpenAI/API/Telegram/n8n input
  Entry->>Normalizer: RawIngressRequest + source/correlation
  Normalizer->>Stores: Register attachment metadata
  Normalizer-->>Planner: NormalizedRequest v1
  Planner-->>Router: Plan v1
  Router->>Registry: Profiles/tools/capability health
  Registry-->>Router: Versioned capability snapshot
  Router-->>Policy: RouteDecision v1

  alt approval or missing scope required
    Policy->>Stores: awaiting_approval or blocked
    Policy-->>Entry: Typed approval/blocked response
  else execution allowed
    Policy-->>Engine: ApprovedExecution
    Engine->>Stores: queued/running transition
    Engine->>Registry: Acquire tools/adapters and required locks
    Engine->>Executor: Bounded attempt
    Executor-->>Engine: Result + artifact/evidence refs
    Engine->>Reviewer: Acceptance package
    Reviewer-->>Engine: accepted / changes_required / blocked
    Engine->>Stores: terminal state and provenance
    Engine-->>Entry: Result or Failure v1
    Entry-->>User: OpenAI-compatible JSON/SSE
  end
~~~

Fast chat не запускает expensive model planner: текущий deterministic Planner возвращает typed signals и `plan=null` для fast/auxiliary path. Request/correlation, route decision, bounded failure semantics и reason при этом сохраняются.

## Target state machine

Эта диаграмма описывает целевые execution phases, а не enum текущего `TaskStateV1`. Этапы 002/005 должны оформить их отдельными events или новой совместимой версией, не переопределяя v1 задним числом.

~~~mermaid
stateDiagram-v2
  [*] --> Received
  Received --> Normalized
  Received --> Failed: invalid transport/schema
  Normalized --> Planned
  Normalized --> Blocked: missing required project/scope
  Planned --> Routed
  Routed --> AwaitingApproval: external/sensitive/action gate
  AwaitingApproval --> Routed: scoped approval
  AwaitingApproval --> Cancelled: denied or expired
  Routed --> Queued
  Queued --> Running: leases acquired
  Queued --> Cancelled: user cancellation
  Queued --> Failed: queue/lease timeout
  Running --> Verifying
  Running --> RetryableFailure
  Running --> Cancelled
  RetryableFailure --> Queued: policy permits retry/fallback
  RetryableFailure --> Failed: attempts exhausted
  Verifying --> Completed: acceptance and reviewer pass
  Verifying --> Queued: bounded remediation attempt
  Verifying --> Failed: verification/review rejects
  Blocked --> Normalized: missing fact supplied
  Completed --> [*]
  Failed --> [*]
  Cancelled --> [*]
~~~

Каждый arrow — durable transition. Process crash переводит незавершённый owned attempt в interrupted/retryable или failed после recovery; он не может оставить ложный completed.

## Health and degraded model

Target health имеет три независимых уровня:

1. **Liveness** — process/event loop отвечает; внешние dependencies не опрашиваются. Реализован `/health/live`.
2. **Readiness** — SQLite task journal, fast model и strong model готовы. Реализован `/health/ready` с 503 при not-ready.
3. **Capabilities** — versioned observations для voice, Qwen/Codex CLI installation, browser, ComfyUI и Telegram находятся в canonical report внутри `/health`; отдельный registry/probe coverage расширяется на этапах 006–010.

Existing /health остаётся backward-compatible aggregate. Optional capability failure не делает gateway dead; Router этапа 002 получает injected bounded `CapabilitySnapshot` с observation timestamp и выбирает только policy-allowed fallback/degraded response. Полноценный registry freshness SLA и unified snapshot persistence остаются target. Секреты и private paths не возвращаются.

## Resource coordination

Target RouteDecision объявляет locks до execution. Stage 002 уже публикует logical `fast_model`, `gpu_heavy`, `qwen_agent`, `codex_agent`, `image` и hashed `worktree:<id>`; текущие in-process adapters используют соответствующие locks. Целевой namespace можно нормализовать позже без утечки raw path:

- gpu:heavy для strong/Qwen/Comfy flows;
- worktree:<canonical-id> для чтения или изменения workspace;
- agent:qwen и agent:codex для executor capacity;
- outbound:<capability-or-recipient> для controlled external actions.

Target locks have owner request/attempt, mode, timestamps, deadline, expiry,
and deterministic acquisition order. Stage 005 now provides a cross-process
file lease and durable ownership evidence for exact coding worktrees; Stage 006
provides exact managed-process/session ownership for its MCP integrations.
Qwen/GPU/Codex capacity locks are still gateway-local rather than a system-wide
resource scheduler. Multi-process resource coordination remains replaceable
behind this contract.

## Storage separation

| Store | Allowed content | Forbidden coupling |
|---|---|---|
| Task Store | State snapshots, transition events, attempts, bounded evidence refs | Raw binary, secrets, full unbounded tool output |
| Artifact Store | Content streams/files plus hash/provenance/retention metadata | Использование path как permission grant |
| Memory Engine | Typed scoped records с provenance/status/delete | Repository chunks, task logs, assumptions as facts |
| Knowledge Engine | Registered sources, chunks/index, retrieval evidence, invalidation | User preferences, credentials, arbitrary full-disk scan |
| Approval ledger | Scoped action/data/recipient/expiry decisions | Tokens, model-generated self-approval |
| Runtime logs | Redacted operational events and correlation ids | Raw prompts/results by default |

SQLite можно сохранить как первый backend, но schemas/migrations and repositories разделяются. Existing task rows мигрируются или читаются legacy adapter; silent deletion недопустим.

## Configuration boundary

Целевой precedence:

1. code defaults;
2. committed non-secret local configuration;
3. runtime environment overrides;
4. designated secret channel только для конкретного adapter.

Один Config Resolver валидирует effective configuration и отдаёт typed snapshot компонентам. Python, PowerShell, Compose, Qwen and workflow adapters не должны независимо изобретать endpoint/model defaults. [System Manifest](../SYSTEM_MANIFEST.md) хранит observed mutable facts, но не является runtime config и никогда не содержит secrets.

## Replacement rules

1. Entry adapter знает NormalizedRequest/response envelope, но не model names или shell commands.
2. Planner не вызывает tools; Router не имеет side effects.
3. Router выбирает logical profile/tool/executor ids, не concrete URLs.
4. Execution Engine не знает CLI flags конкретного Qwen/Codex; adapter отвечает за translation и cancellation.
5. Tool adapters проходят только ToolSpec/ToolResult/Failure contracts.
6. Storage доступен через repositories; module не импортирует SQLite table layout другого module.
7. Reviewer получает immutable evidence refs и не изменяет executor result/worktree напрямую.
8. Artifact path не даёт permission; access проверяется по request/project/data scope.
9. OpenAI transport quirks локализованы в Response adapter; core state не зависит от streaming framing.
10. Старый route остаётся доступным через compatibility adapter до подтверждённой migration.

## Stage ownership

| Stage | Что становится реальным только после gate |
|---|---|
| 001 | Current/target boundaries, versioned core contracts, configuration/health/resource model and contract tests |
| 002 | Complete: deterministic Planner/Router, explicit overrides, reason codes, bounded fallback, 117/117 routing evaluation and live runtime gates |
| 003 | Complete: Memory Engine schema, migrations, CRUD/export/delete, provenance/privacy |
| 004 | Complete: scoped source generations, archive adapters, Repository Map v1, FTS5/rg retrieval, Context Envelope, invalidation/purge and green regression/doctor/smoke/live-index gates |
| 005 | Complete: hardened Qwen/Codex Coding Engine, owned worktree safety, bounded retries, verification, independent review and resumable handoff |
| 006 | Complete: managed MCP Hub with Context7, loopback Playwright fixture, local diagnostics, generated consumer views and failure isolation |
| 007 | Unified Tool/Application Registry and permission-gated real adapters |
| 008 | Durable voice jobs, long transcription, Artifact Store integration |
| 009 | Verified vision/image workflows and cross-process GPU coordination |
| 010 | Durable Entry interfaces, API/Telegram/n8n auth, idempotency, inbox/recovery |
| 011 | Controlled evidence → experiment → approval → apply/rollback consumer of these contracts |
| 012 | Versioned evaluation suites, baseline/resource metrics and regression gates |

Stages 001–006 closed core routing, Controlled Memory, scoped Knowledge,
repository-aware coding, and a deliberately small managed MCP layer. Stage 007
must unify native tools, MCP capabilities, applications, policy, health, and
resource metadata without duplicating the Coding Engine's filesystem/shell/Git
authority. Durable interfaces remain Stage 010. The initial routing EvalKit is
published, but broader Stage 012 evaluation remains planned.

## Acceptance for architectural replacement

Module replacement допустима, когда:

1. old and new implementations pass the same versioned contract tests;
2. compatibility /v1 streaming/non-stream behavior verified;
3. failure, cancellation, health and degraded behavior tested, а не только success;
4. storage migration/rollback доказаны на fixture и existing data preserved;
5. permission/data boundary не ослаблена;
6. resource usage and latency не выходят за declared budget;
7. [System Manifest](../SYSTEM_MANIFEST.md), [Current State](CURRENT_STATE.md) и применимый ADR обновлены evidence.

До этого target component остаётся **planned**, даже если создан class, schema или placeholder endpoint.
