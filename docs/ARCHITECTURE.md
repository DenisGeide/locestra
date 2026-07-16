# Current Architecture

- Статус: factual snapshot завершённого этапа 002; implementation, regression, doctor/smoke и Open WebUI E2E gates зелёны.
- Снимок: 2026-07-14.
- Владелец: владелец платформы.
- Источники истины: [System Manifest](../SYSTEM_MANIFEST.md), [Current State](CURRENT_STATE.md), исходный код и свежие runtime-проверки.
- Изменение: обновлять при изменении request path, process/storage boundary, порта, executor или failure behavior.

## Как читать статусы

- **Verified** — наблюдался работающий endpoint, процесс, test или artifact.
- **Discovered** — реализация или configuration найдены, но bounded end-to-end текущим audit не выполнялся.
- **Unavailable** — capability сейчас недоступна.
- **Planned** — описана только как будущая граница; planned modules находятся в [Target Architecture](TARGET_ARCHITECTURE.md).

HTTP 200, наличие файла и заявление модели сами по себе не являются end-to-end evidence. Фактические версии и mutable runtime facts остаются в [System Manifest](../SYSTEM_MANIFEST.md), а этот документ фиксирует связи и ответственность.

## Сохраняемые внешние контракты

Текущая платформа рассчитана на одного владельца Windows workstation. Следующие границы нельзя менять без migration path:

| Boundary | Текущий контракт |
|---|---|
| Основной UI | Open WebUI на порту 3737 |
| Model API | OpenAI-compatible /v1 на gateway, порт 8787 |
| Публичное имя модели | local-agent-auto |
| Voice API | OpenAI-compatible transcription endpoint, порт 8788 |
| Automation | n8n на порту 5678 |
| Strong Ollama | loopback 11434, GPU |
| Fast Ollama | loopback 11435, CPU/RAM |
| Image runtime | ComfyUI на loopback 8388, только on-demand |

## Фактическая схема

~~~mermaid
flowchart LR
  User["Single trusted owner"]

  subgraph External["External trust boundary"]
    TelegramAPI["Telegram API"]
    CodexCloud["Codex cloud"]
    Context7["Context7 / external documentation"]
    Web["External websites"]
  end

  subgraph Docker["Docker boundary"]
    UI["Open WebUI :3737"]
    N8N["n8n :5678<br/>repository workflow inactive"]
    UIVol[("Open WebUI volume")]
    N8NVol[("n8n volume")]
    UI --- UIVol
    N8N --- N8NVol
  end

  subgraph Host["Windows host boundary"]
    TG["Telegram polling adapter<br/>currently stopped"]
    GW["FastAPI gateway :8787<br/>entry + Normalizer/Planner/Router<br/>execution + locks + health + response"]
    Voice["Voice :8788<br/>faster-whisper CPU"]
    Fast["Ollama fast :11435<br/>CPU/RAM"]
    Strong["Ollama strong :11434<br/>GPU"]
    Qwen["Qwen Code CLI<br/>one process per request"]
    Codex["Codex CLI<br/>ephemeral process"]
    Browser["Node Playwright adapter<br/>one Chromium per request"]
    Image["PowerShell image lifecycle"]
    Comfy["ComfyUI :8388<br/>on-demand GPU"]
    DB[("SQLite task journal")]
    Inbox[("inbox Codex bundles")]
    Outputs[("outputs artifacts")]
    Temp[("%TEMP% Codex last message")]

    UI -->|"/v1/chat/completions"| GW
    UI -->|"/v1/audio/transcriptions"| Voice
    N8N -.->|"configured webhook; inactive file"| GW
    TG --> Voice
    TG --> GW

    GW --> Fast
    GW --> Strong
    GW --> Qwen
    GW --> Codex
    GW --> Browser
    GW --> Image
    GW --> Voice
    Qwen --> Strong
    Image --> Comfy
    Image -.->|"stops strong model before startup"| Strong

    GW --> DB
    GW --> Inbox
    GW --> Outputs
    Codex --> Temp
  end

  User --> UI
  User -.-> N8N
  TelegramAPI <--> TG
  Codex --> CodexCloud
  Qwen --> Context7
  Qwen --> Web
  Browser --> Web
~~~

Прямые client URLs используют loopback, но gateway и voice фактически запускаются на 0.0.0.0, а Docker публикует Open WebUI и n8n без host-IP restriction. Поэтому схема показывает отдельные trust boundaries, а не гарантированную сетевую изоляцию. Ограничения и threat controls определены в [Security Model](SECURITY_MODEL.md).

## Компоненты и фактическая ответственность

| Компонент | Статус | Вход | Выход | Storage / side effects | Failure behavior |
|---|---|---|---|---|---|
| Open WebUI | Verified health/version, fast `LOCAL_UI_OK` and read-only README-heading E2E | Browser session, text, attachments, audio | OpenAI request в gateway; audio в voice; rendered response | Persistent Docker volume с UI history/settings | Показывает typed HTTP/SSE failure; verified requests completed without transport truncation |
| Gateway | Stage 002 complete: deterministic routing, full regression and live gates verified | ChatRequest на /v1/chat/completions; typed route preview; health | Raw/synthetic OpenAI JSON/SSE или typed pre-stream error; static /outputs | Task SQLite, inbox bundles, outputs; subprocesses; in-memory locks | Model/tool failures имеют 4xx/5xx; unavailable/degraded work и handoff больше не выдаются за assistant success |
| Fast Ollama | Verified present/loaded | OpenAI chat payload для auxiliary, fast chat и prompt normalization | Chat completion/SSE | Model cache в RAM; gateway task marker | Timeout/upstream error; streaming error может произойти после отправки headers |
| Strong Ollama | Verified present/loaded | Strong chat и Qwen Code model calls | Chat completion/SSE/model tokens | GPU/VRAM residency | Конкурирует с ComfyUI; сериализуется только одним gateway process |
| Qwen Code | Verified gateway→Qwen disposable-workspace edit и Open WebUI→Qwen read-only README inspection | Exact bounded Plan через stdin, resolved cwd, read-only/write mode | Buffered final CLI text | Read-only использует approval `plan`; write — `yolo`; committed code/docs profiles копируются в ignored writable QWEN_HOME | Oversized executable Plan fail-closed до CLI; две explicit local strategies максимум; затем ровно один redacted Codex handoff и typed 502 |
| Codex CLI | CLI/login verified; execution/review path discovered | Prepared prompt, cwd, sandbox/review mode | Buffered last message | Scoped cloud approval через обычный ingress отсутствует; создаётся local inbox bundle | Route блокируется с typed 409 и bundle; direct cloud execution не считается разрешённым enable flag |
| Browser adapter | Verified local Chromium fixture and live external `Example Domain` navigation | Public HTTP(S) URL из prompt | JSON с final URL, title и первыми 12000 символами body | Временный Chromium process; task result в SQLite | Router блокирует literal local/private target; adapter проверяет DNS, redirects и subrequests; runtime errors typed |
| Voice | Standalone health/model verified; Stage 002 chat bridge unit-tested, live recheck pending | Multipart upload или bounded inline chat audio через gateway | JSON text/language/duration; gateway returns transcript | Standalone upload во временном файле, затем cleanup | Chat bridge errors typed; standalone exception может стать 500; auth/rate limit и durable job queue отсутствуют |
| Image adapter + ComfyUI | Runtime/checkpoint discovered; service idle; semantic generation not verified текущим audit | Text prompt | Generated file copied в outputs и Markdown URL | Останавливает strong model; запускает/останавливает ComfyUI; сохраняет output | Error → typed HTTP 502; PowerShell finally останавливает ComfyUI |
| Telegram adapter | Discovered; unavailable now because process is not running | Telegram text, voice/audio, photo | Reply chunks до 4000 characters | Temporary voice file; photo полностью в memory; gateway/voice side effects | Raw exception отправляется actor; actor allowlist и adapter health отсутствуют |
| n8n | Verified container health; repository workflow inactive | Webhook body prompt/text, если workflow активирован | Gateway answer/route | Persistent n8n volume | Live workflow/import/auth не проверены; file definition имеет active=false |
| Task journal | Additive v1 migration и state roundtrip verified; Stage 002 structured attempt tests implemented | task id, decision/plan, actual executor/model/profile, attempt evidence | Legacy projections + `TaskStateV1` snapshot | data/memory.sqlite3; raw prompt/result остаются legacy debt | Snapshot хранит route decision, plan и attempt history; append-only events/recovery API отсутствуют |
| Lifecycle scripts | Start/readiness/stop/restart verified 2026-07-14 | start, stop, doctor, smoke commands | Owned processes, actual listener PID/owner metadata and health | logs, run PID/owner files, Docker containers/volumes | Unknown/reused PID или foreign listener не завершается; partial whole-platform start всё ещё не транзакционный |

Реализация компонентов: [gateway](../services/gateway/app.py), [common storage](../services/common.py), [voice](../services/voice/app.py), [Telegram](../services/telegram/bot.py), [browser adapter](../services/browser/inspect.mjs), [lifecycle start](../scripts/start.ps1), [lifecycle stop](../scripts/stop.ps1) и [Compose](../docker-compose.yml).

## Точный request lifecycle

### 1. Ingress

Open WebUI отправляет model request в gateway через host.docker.internal и отдельно использует voice endpoint для STT. Telegram и n8n формируют тот же минимальный OpenAI payload, поэтому gateway не получает надёжный source, authenticated actor или correlation metadata. Репозиторный n8n workflow сейчас не активен; Telegram process сейчас не запущен.

### 2. Normalization boundary

Gateway сохраняет permissive внешний `ChatRequest`, затем создаёт строгий [NormalizedRequestV1](CONTRACTS.md). Inline attachment bytes/base64 не копируются: contract содержит positional metadata reference. Из-за отсутствия authenticated ingress metadata текущий `/v1` source честно записывается как `api`. Для control decisions gateway:

1. выбирает последнее сообщение role=user;
2. превращает text parts в строку, а image/audio/file parts — в bounded positional metadata markers;
3. распознаёт только leading standalone `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser`, удаляет prefix до execution и блокирует conflict;
4. извлекает project из маркера Project/Проект/Repo или ведущего Windows path;
5. сохраняет typed resolution: explicit resolved/invalid, default resolved или missing; invalid explicit path никогда не получает default.

Полная conversation history передаётся chat model через bounded context builder, который сохраняет complete assistant/tool exchanges атомарно и помечает omission. Classifier, project extraction и one-shot coding agent получают только последнюю user-реплику, поэтому follow-up вроде «исправь это» может потерять route/project context предыдущей реплики.

### 3. Planner и Router

Stage 002 вынес control decision в [orchestration modules](../services/orchestration/) и versioned [routing policy](../config/routing.json):

1. bounded deterministic Planner извлекает action, repository/read/write/review semantics, complexity, risk и media/network signals;
2. fast chat и auxiliary не создают тяжёлый plan; остальные routes получают bounded `PlanV1`;
3. pure Router применяет natural route, override, project/permission/network gates, injected capability status и failure limit;
4. результат — `RouteDecisionV1` с policy version, executor/model/profile, action/mode, reason/blocking codes, capability observation, fallback, max attempts и logical locks;
5. LLM classifier не используется: `llm_signal.enabled=false`.

Repository action приоритетнее incidental `docs/image/browser` слов, а educational/security-policy вопрос без repository action не расходует coding executor. Scoped `Project: ...; analyze/understand/explain this repository; do not modify files` попадает в read-only `local_code`, а не в общий chat. `/v1/route` возвращает safe control diagnostics без prompt/attachment bytes/command lines; resolved project остаётся в ответе для backward compatibility. Правила и fixed 117-case corpus описаны в [Routing](ROUTING.md).

### 4. Execution

- Auxiliary и fast chat используют fast Ollama и отключённое thinking.
- Strong chat использует strong Ollama и medium reasoning.
- Local code запускает максимум две bounded Qwen strategies; после первой failure стратегия меняется, после второй создаётся один handoff. Executable Plan сохраняет multiline goal, constraints, acceptance и verification без lossy rewrite; превышение 6000-token conservative ceiling блокируется до Qwen. Docs всегда запускает read-only Qwen/Context7 в neutral `run/docs-workspace`, даже если в request указан project.
- Codex route через обычный ingress не имеет scoped cloud approval и поэтому создаёт bundle с typed non-success response; enable flag не является approval.
- Browser route запускает Node adapter только для public HTTP(S), повторно проверяя DNS, redirects и subrequests.
- Image route нормализует prompt fast model, получает image/GPU locks и запускает PowerShell/ComfyUI workflow.
- Voice route декодирует bounded inline audio и проксирует его в существующий Whisper transcription API; vision остаётся explicit unavailable.

### 5. Persistence

Execution path записывает `blocked`, `ready` или `running`; running затем переходит в `complete`, `failed`, `cancelled` либо fallback `ready`. `TaskStateV1` теперь сохраняет исходный decision, optional plan, actual executor/model/profile, `fallback_used` и structured attempt history с bounded evidence. Additive SQLite migration сохраняет legacy columns; raw prompt/result не копируются в state JSON, но остаются legacy privacy debt. Это snapshot journal, а не append-only event log или Memory Engine.

### 6. Response

Fast/strong chat proxy streaming передаёт upstream bytes. Journal остаётся running до фактического завершения iterator; normal end записывает complete, generator error — failed, client cancellation — cancelled. Agent/tool routes ждут полного завершения и только затем создают synthetic OpenAI response; при stream=true это один content chunk, final chunk и DONE. Token usage для synthetic response равен нулю. Request/task correlation возвращается в `X-Local-Agent-Request-ID` и non-stream body.

## Текущая state machine

~~~mermaid
stateDiagram-v2
  [*] --> Normalized: non-empty ChatRequest
  Normalized --> Routed: RouteDecisionV1
  Routed --> Blocked: invalid scope, approval, URL or attachment
  Routed --> Ready: unavailable capability or handoff artifact
  Routed --> Running: allowed chat, agent, browser, voice or image

  Running --> Complete: result or stream ends
  Running --> Failed: model, tool, storage or stream error
  Running --> Cancelled: streamed client disconnect
  Running --> Running: second changed local strategy
  Running --> Ready: one Codex fallback bundle

  Blocked --> [*]
  Ready --> [*]
  Complete --> [*]
  Failed --> [*]
  Cancelled --> [*]
~~~

State является последним snapshot; durable transition history, crash reconciliation и CLI cancellation ещё не реализованы. Synthetic agent stream отражает завершение executor до доставки последнего SSE chunk, тогда как raw model stream получает `complete` только после terminal `data: [DONE]`; clean EOF без marker становится failed, disconnect — cancelled.

## Trust boundaries

| Boundary | Что пересекает границу | Текущее enforcement |
|---|---|---|
| Browser/UI → Open WebUI | User input, attachments, UI history | Open WebUI internal session behavior; compose устанавливает WEBUI_AUTH=false |
| Open WebUI/n8n → host gateway | Full chat payload | Gateway auth отсутствует; published ports не host-bound |
| Telegram API → adapter | Actor message and media | Token channel существует; actor allowlist отсутствует |
| Gateway → workspace/terminal/Git | Prompt-derived task and cwd | Existing-path check и prompt rules; central allowlist/sandbox отсутствуют |
| Gateway/Qwen → Context7/web | Documentation query or URL | External data считается untrusted; Context7 outbound policy всё ещё adapter-specific |
| Gateway/Playwright → web | Public HTTP(S) target | Router блокирует literal private/local targets; adapter проверяет DNS, redirects и subrequests; общий outbound audit/proxy отсутствует |
| Gateway → Codex cloud | Prompt и доступный Codex context | Обычный ingress fail-closed: без scoped approval создаётся bounded/redacted local bundle; approval ledger ещё отсутствует |
| Host → Docker | API calls and persistent volumes | Separate processes/volumes; published-port policy недостаточна |
| Model/tool output → control plane | Text, paths, errors | Output возвращается/сохраняется; typed evidence/provenance enforcement отсутствует |

Нормативные правила находятся в [Permissions](PERMISSIONS.md), [Security Model](SECURITY_MODEL.md) и [Constitution](../constitution/CORE.md). Их наличие не означает полного runtime enforcement.

## Resource boundaries

| Resource | Current owner/lock | Scope | Ограничение |
|---|---|---|---|
| Fast CPU model | FAST_MODEL_LOCK | Один gateway process | Сериализует даже независимые fast requests |
| GPU/strong model | GPU_LOCK | Один gateway process | Shared strong chat, Qwen и image; не защищает от второго gateway process |
| Qwen executor | AGENT_LOCK | Один gateway process | Все Qwen tasks serial, даже разные worktrees |
| Codex executor | CODEX_LOCK | Один gateway process | Все cloud tasks serial |
| Image lifecycle | IMAGE_LOCK + GPU_LOCK | Один gateway process | PowerShell отдельно останавливает strong model |
| Workspace | lock по canonical real path | Один gateway process | Qwen/Codex одного worktree serial; разные worktrees могут войти независимо для разных executors; OS/interprocess lease и junction policy отсутствуют |
| Outbound external action | Нет общего lock/policy | Нет | Browser, Context7, Codex, Telegram имеют разные ad hoc paths |
| Voice CPU/model | lru_cache без job lock | Voice process | Sync transcription внутри async endpoint блокирует worker |

## Storage boundaries

| Store | Producer/consumer | Содержимое | Текущий lifecycle |
|---|---|---|---|
| data/memory.sqlite3 | Gateway/common | Raw prompt, last route/status/result/metadata | Persistent, ignored; нет event history, redaction, TTL/export/delete API |
| inbox | Gateway/Codex handoff | Bounded Markdown goal/project/constraints/attempt evidence | Persistent ignored files; create-exclusive per task и pattern redaction есть, общего DLP/TTL/delete API нет |
| outputs | Image script/gateway static mount | Generated media | Served без отдельной auth policy; retention не определён |
| logs | Lifecycle child processes | stdout/stderr | Ignored; общий redaction/rotation contract отсутствует |
| run | lifecycle + docs adapter | Actual listener PID, versioned owner metadata, neutral docs workspace | Identity/start-time/root/port перепроверяются; stop не принимает foreign listener; docs workspace не заменяет explicit project |
| system temp | Codex adapter | Last-message text | Successful-path cleanup/TTL неполны |
| Open WebUI volume | Open WebUI | History and settings | Persistent Docker volume |
| n8n volume | n8n | Workflows, executions, credential metadata | Persistent Docker volume |
| workspace | Qwen/Codex/tools | Source, diffs and generated project files | User-owned; preservation зависит от prompts/sandbox/locks |

Большие binary attachments не имеют versioned artifact metadata; photo adapter помещает base64 непосредственно в request. Бесконечные tool outputs ограничиваются ad hoc truncation, а не Artifact Store.

## Process lifecycle

Canonical lifecycle остаётся [start.ps1](../scripts/start.ps1) и [stop.ps1](../scripts/stop.ps1).

- Start создаёт runtime directories, выполняет frozen uv sync и fail-fast config validation, проверяет Docker, запускает fast Ollama, gateway/voice, условно Telegram, затем Compose.
- Gateway readiness проверяет SQLite task journal и обе Ollama model profiles; optional capabilities публикуются отдельно.
- Generic URL wait считает любой status ниже 500 готовностью.
- Telegram при настроенном runtime credential регистрируется только после проверки rooted command identity; отдельного adapter health/actor allowlist всё ещё нет.
- `run/*.pid` хранит фактический worker/listener PID, а `run/*.owner.json` — root/name/port/fragments/start time/timestamp.
- Stop повторно проверяет identity и завершает только owned process; foreign/reused PID остаётся и приводит к nonzero result.
- Strong Ollama на 11434 считается independently managed и stop script его не останавливает.

## Monolith и hidden coupling

1. [gateway app](../services/gateway/app.py) всё ещё объединяет API, execution, response formatting, health, task persistence and resource arbitration; Normalizer/Planner/Router вынесены в pure modules.
2. Planner/Router являются modular boundaries; Tool Registry, Memory Engine, Knowledge Engine, Reviewer и Artifact Store ещё не являются отдельными runtime boundaries.
3. Qwen и Codex являются one-shot CLI invocations; task journal не возвращается им как controlled memory.
4. Общий `run_process` буферизует bounded output и использует общий process/error boundary для Qwen, Codex, Node и PowerShell. Qwen-specific environment передаётся только Qwen adapter; immutable `qwen-code`/`qwen-docs` settings копируются в `run/qwen-homes/*`. Qwen/Codex task prompts идут через stdin, а не process arguments; timeout завершает descendant process tree.
5. Browser существует как direct Node adapter и как Playwright MCP в Qwen configuration.
6. Python model/endpoint settings централизованы в typed resolver, но protected ports и некоторые adapter facts остаются продублированы в PowerShell/Compose/Qwen/n8n; resolver/start preflight отклоняют lifecycle address override до цельной migration.
7. Image lifecycle знает конкретные Ollama model names и останавливает их перед ComfyUI.
8. Task state и raw conversation persistence смешаны в таблице tasks, несмотря на разные retention/provenance requirements.
9. OpenAI response semantics различаются между raw model streaming, non-stream forwarding и synthetic agent streaming.

Эти связи являются основанием для стабильных target boundaries, но сами по себе не требуют переписывания рабочего gateway. Целевая декомпозиция описана в [Target Architecture](TARGET_ARCHITECTURE.md).

## Известное failure behavior

| Сценарий | Текущее наблюдаемое поведение |
|---|---|
| Malformed ChatRequest | FastAPI 422 |
| Fast/strong non-stream upstream failure | HTTP 502 и task failed |
| Fast/strong stream failure после headers | Transport уже нельзя заменить на 502, но exception или EOF без `data: [DONE]` делает task failed; disconnect становится cancelled |
| Invalid explicit project | Typed HTTP 422 + blocked state; default project не подставляется |
| Local coding disabled | Typed unavailable error; решение не выдаётся за success |
| Две Qwen failures | Ровно один bounded/redacted Codex bundle, ready state с actual executor и HTTP 502 |
| Docs/browser/voice/image failure | Typed HTTP 502 error с route/request id |
| Codex без scoped approval | Ready local handoff и typed HTTP 409; cloud CLI не запускается |
| Vision | Explicit unavailable/degraded response; ложного image understanding нет |
| Client disconnect during CLI task | Thread cancellation не гарантирует termination subprocess |
| Gateway restart during task | Running row может остаться; recovery/reconciliation нет |
| Optional capability unavailable | Core readiness сохраняется; Router формирует typed degraded/blocked decision и pre-stream 4xx/5xx |
| Несколько gateway processes | Locks и WORKTREE_LOCKS не координируются |

## Проверено и не проверено этим audit

**Verified на Stage 001 baseline:** versioned contracts/config/health tests; additive SQLite migration без потери legacy rows; actual owner PID; start → readiness → stop → no owned orphan → restart; strong Ollama PID preservation; Open WebUI container → gateway SSE transport; request correlation/TaskState v1; doctor; full smoke; ports/processes, voice, n8n, Playwright and fast/strong model presence.

**Implemented/verified на Stage 002 revision:** deterministic Normalizer/Planner/Router, strict policy `2026-07-14.1`, overrides/negation/collisions, typed degraded/blocked errors, route/plan/actual-attempt persistence, two failures → one handoff, same-worktree serialization and 117/117 fixed routing corpus. Full suite: `189 passed`; focused routing/config/eval/execution/gateway selection: `122 passed`. Warm fast-route test имеет CI ceiling p95 `<25 ms`; это не production benchmark.

**Verified current runtime:** `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK`; Qwen disposable-workspace edit через gateway; Open WebUI fast prompt → `LOCAL_UI_OK`; Open WebUI read-only repository request → exact README heading; live Context7 FastAPI lifespan retrieval; external browser → `Example Domain`. **Discovered:** Codex exec/review implementation, image runtime/checkpoint и n8n workflow definition.

**Не verified end-to-end и остаётся за рамками Stage 002 gate:** Telegram delivery, live n8n webhook, gateway chat→Whisper audio, Codex execution/review quality, semantic image generation, restart/recovery after OS reboot and multi-process lock behavior. Browser local/external paths и policy проверены, но residual WebSocket/service-worker/DNS-rebinding coverage остаётся ограничением.

Нельзя повышать эти статусы без bounded evidence и обновления [System Manifest](../SYSTEM_MANIFEST.md).
