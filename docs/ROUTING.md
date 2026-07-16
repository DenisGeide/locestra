# Deterministic Planner and Router

- Статус: Stage 002 complete; unit/contract/evaluation, doctor/smoke, Open WebUI, live Context7 и external browser gates green.
- Policy schema: `1.0`.
- Policy version: `2026-07-14.1`.
- Machine sources: [normalizer](../services/orchestration/normalizer.py), [planner](../services/orchestration/planner.py), [router](../services/orchestration/router.py), [routing policy](../config/routing.json) и [v1 contracts](../services/contracts/v1.py).
- Владелец: gateway orchestration boundary.
- Изменение: только вместе с новой `policy_version`, regression corpus и compatibility/security review.

## Гарантии

Routing является чистым и воспроизводимым решением над versioned policy и явными входными facts. Модель не выбирает executor, permissions или workspace. Текущий `llm_signal.enabled=false`; Planner и Router не вызывают LLM. Одинаковые normalized request, capability/permission snapshots, failure history и policy дают одинаковый `RouteDecisionV1`, кроме явно переданного observation timestamp.

Router:

- не выполняет tools и не имеет side effects;
- не принимает model output как permission;
- не заменяет invalid explicit project на `DEFAULT_PROJECT`;
- не выдаёт route override за workspace, network или cloud approval;
- возвращает typed `ready`, `degraded` или `blocked` decision с machine-readable reasons;
- хранит логические executor/profile/lock ids, а не shell command line или secret values.

## Pipeline

~~~mermaid
flowchart LR
  Ingress["OpenAI-compatible request"] --> Normalizer["Normalizer"]
  Normalizer --> NR["NormalizedRequestV1"]
  NR --> Planner["Bounded deterministic Planner"]
  Planner --> Signals["IntentSignals"]
  Planner --> Plan["PlanV1 when needed"]
  Signals --> Router["Deterministic Router"]
  Plan --> Router
  Health["Capability snapshot"] --> Router
  Permissions["Permission snapshot"] --> Router
  Failures["Failure history"] --> Router
  Policy["routing.json 2026-07-14.1"] --> Planner
  Policy --> Router
  Router --> Decision["RouteDecisionV1"]
  Decision --> Gate["Permission / availability gate"]
  Gate --> Executor["Actual executor or typed error/handoff"]
  Executor --> State["TaskStateV1 + attempt history"]
~~~

### 1. Normalizer

Normalizer выбирает последнюю `role=user` реплику, применяет Unicode NFKC и формирует bounded attachment references без копирования inline base64/audio bytes. Он разрешает project и сохраняет отдельный `ProjectResolutionV1`:

- `explicit/resolved` — существующий каталог из `Project:`, `Проект:`, `Repo:` или ведущего Windows path;
- `explicit/invalid` — явно указанный каталог не существует; default запрещён;
- `default/resolved` — explicit hint отсутствует и configured default существует;
- `none/missing` — project не найден.

Routing controls распознаются только как ведущие standalone tokens: `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser`. Prefix удаляется до Planner и executor. `/locality` не является override. Несколько разных ведущих override дают `override_conflict=true` и блокируются.

### 2. Lightweight Planner

Planner извлекает action, execution mode, complexity, risk, repository intent, review/mutation/read-only semantics, media attachments и public URL. Matching использует word boundaries и bounded negation handling, поэтому `test` не совпадает с частью `blacklist`, а `do not modify, create, or delete files` остаётся read-only.

Literal project declaration и absolute path удаляются только из текста intent matching, но сохраняются в normalized goal/project resolution. Поэтому segment вроде `pytest` в имени temporary worktree не превращает обычный `create a marketing launch plan` в coding task.

Для repository action количество matched high/critical markers сравнивается с versioned thresholds; в policy `2026-07-14.1` оба threshold равны `1`. Critical проверяется раньше high. `planner_routes` является config allowlist для создания `PlanV1`; natural route и safety gates остаются deterministic code paths, а не конфигурируемым permission bypass.

Fast chat и Open WebUI auxiliary запросы не получают искусственный тяжёлый plan: `planning_mode=skipped_fast_path`, `plan=null`. Для repository, analysis, docs, browser и media действий создаётся bounded `PlanV1` с goal, одним минимальным subtask, tools, acceptance criteria, verification, constraints и context budget. Planner не читает repository и не запускает tools.

Scoped repository requests вида `Project: ...; analyze/understand/explain this repository; do not modify files` являются read-only `local_code`; те же educational verbs без repository scope остаются chat. Global mutation/read-only conflict никогда не снимает permission ceiling.

### 3. Deterministic Router

Natural route выбирается в таком порядке:

1. Open WebUI auxiliary prompt;
2. repository action: review/high/critical → Codex boundary, остальные read/write tasks → Qwen Code;
3. voice/vision/image attachment or explicit media action;
4. documentation/Context7;
5. browser/public URL;
6. strong analysis/architecture;
7. fast chat.

Repository action имеет приоритет над incidental words `documentation`, `image`, `browser` и URL. Поэтому `Fix documentation build`, `Implement create image endpoint` и `Fix browser test using https://example.com` остаются coding tasks. Educational questions без repository action остаются chat; policy/security question не расходует Codex.

После natural route Router последовательно применяет override, project validity, permission/network rules, capability status и failure limit. Override может сузить/выбрать route, но не обходит последующие gates:

- `/local` направляет repository work в Qwen, но high/critical-risk write не получает approval через override и блокируется (`permission.high_risk_local_override_denied`);
- `/codex` применяется только к programming/repository/architecture intent; `/codex hello` отклоняется и остаётся fast chat;
- `/voice`, `/vision`, `/image`, `/browser` выбирают соответствующую capability;
- browser принимает только HTTP(S) public targets; literal private/local targets блокируются Router, а adapter повторно проверяет DNS, redirects и HTTP(S) subrequests;
- Codex cloud требует отдельного scoped approval. Обычный chat ingress такого approval не выдаёт, поэтому Codex route создаёт local handoff bundle и возвращает typed `409`, а не запускает cloud CLI.

## Routes и текущее выполнение

| Route | Normal executor | Текущее правило |
|---|---|---|
| `auxiliary` | `fast_ollama` | Локальный fast model, plan skipped |
| `fast_chat` | `fast_ollama` | Локальный fast model, plan skipped |
| `strong_chat` | `strong_ollama` | Локальная reasoning model |
| `local_code` | `qwen_code` | Resolved worktree; immutable MCP-free profile copied to writable ignored QWEN_HOME; `--bare`; read-only uses Qwen `plan`, write uses `yolo`; максимум две local стратегии |
| `codex` | `codex_cli` после scoped approval | Через обычный ingress approval отсутствует, поэтому фактический executor — `codex_bundle` |
| `codex_bundle` | `codex_bundle` | Локальный bounded redacted Markdown handoff; это не выполненная задача |
| `docs` | `qwen_code` | Qwen `plan` с Context7-only immutable profile; always neutral `run/docs-workspace`, включая request с explicit project; typed error при failure |
| `browser` | `playwright` | Только public HTTP(S), DNS/redirect/subrequest checks |
| `image` | `comfyui` | Local on-demand capability; GPU serialized |
| `voice` | `whisper` | Bounded inline `input_audio` декодируется и проксируется в standalone `/v1/audio/transcriptions`; ошибки typed |
| `vision` | `degraded_response` | Vision executor не подключён; attachment/override получает явную unavailable response |

Unavailable optional capability не превращается в success и не делает core gateway non-ready. Решение становится `degraded` или `blocked`, executor — `degraded_response`, а gateway до SSE headers возвращает OpenAI-style error с 4xx/5xx, route и request id.

## Диагностика `/v1/route`

`GET /v1/route?text=...` возвращает сериализованный `RouteDecisionV1` и сохраняет legacy-compatible `project` как пустую строку при отсутствии project. Поля включают:

- route, executor, logical model/profile;
- ordered `reason_codes` и `blocking_reason_codes`;
- action, complexity, risk и execution mode;
- requested override и его disposition;
- decision/permission disposition;
- capability, status и observation timestamp;
- resolved project, required logical locks, bounded fallback и `max_attempts`;
- `policy_version=2026-07-14.1`.

Endpoint не возвращает prompt body, attachment bytes, credentials или command lines. Он всё ещё показывает resolved project для compatibility и предназначен только для текущего trusted single-user ingress; общий auth относится к этапу 010.

## Failure и fallback

Local coding выполняет максимум две явные Qwen attempts. После первой failure вторая attempt получает bounded error summary и обязательство применить другую concrete hypothesis с повторной инспекцией и проверкой. После второй failure:

1. рекурсия и третья local attempt запрещены;
2. создаётся ровно один idempotent Codex bundle на task id;
3. bundle сохраняет original goal, project/worktree, constraints, acceptance criteria, verification plan, bounded/redacted errors, command summaries, modified files и artifact refs;
4. task получает фактический executor `codex_bundle`, `fallback_used=true` и state `ready`;
5. gateway возвращает typed `502 failure.local_attempt_limit`, а не ложный assistant success.

Handoff writer использует create-exclusive semantics и не перезаписывает существующий task bundle. Bounded redaction скрывает распространённые token/authorization/password patterns, но не является полноценным DLP; transfer в cloud всё равно требует отдельного approval.

## Persistence и фактический executor

`TaskStateV1` snapshot сохраняет исходный `RouteDecisionV1`, optional `PlanV1`, фактические executor/model/profile, `fallback_used` и structured `attempt_history`. Каждая attempt содержит index, outcome, bounded reasons/command summaries/error, modified files, artifact refs и timestamps. Decision и desired route не подменяют фактическое выполнение: если Qwen исчерпан и создан bundle, state executor равен `codex_bundle`.

Это всё ещё snapshot journal. Append-only task events, crash reconciliation и durable cross-process leases не реализованы.

## Concurrency

Canonical real worktree path получает один in-process lock. Qwen и Codex над одним worktree сериализуются; Qwen и Codex над разными worktrees могут войти независимо. Дополнительно:

- все Qwen attempts сериализуются `AGENT_LOCK` и shared `GPU_LOCK`;
- все Codex executions сериализуются `CODEX_LOCK`;
- image использует `IMAGE_LOCK` и shared `GPU_LOCK`;
- locks действуют только внутри одного gateway process.

`required_locks` в decision является прозрачной декларацией. Межпроцессные leases, read/write worktree modes и crash recovery остаются будущим hardening.

## Context budget

Planner задаёт policy bounds, а не физическое окно модели:

- strong/general: input 24 000, reserve до 6 000;
- repository/docs/browser executable agent Plan: input maximum 6 000, reserved output 4 000; Qwen profile generation ceiling 4096;
- attachment bytes: до 25 000 000;
- tool output: до 20 000 characters;
- compression: `provenance_preserving`.

Текущий executor ещё не реализует полный Context Engine или repository RAG. Он уже передаёт multiline goal, constraints, acceptance и verification без lossy rewrite и fail-closed блокирует oversized executable Plan до Qwen. Dumping большого repository в prompt запрещён.

## Versioned configuration

[config/routing.json](../config/routing.json) — отдельный committed, non-secret, strict and frozen policy. Loader принимает только schema `1.0`, отклоняет неизвестные поля, duplicate/unbounded marker lists и invalid thresholds, затем кеширует immutable `RoutingPolicy`. Этот policy намеренно не использует `.env` precedence: изменение routing semantics — reviewable Git change с новой policy version, а не скрытый runtime override.

`llm_signal` зарезервирован как bounded future policy envelope, но сейчас выключен и не имеет control-path implementation. Его включение без отдельной реализации, timeout/failure policy и regression evidence недопустимо.

## Evaluation

Fixed corpus в [test_routing_eval.py](../tests/test_routing_eval.py) содержит 117 русско- и англоязычных cases: fast/strong, repository read/write/analyze, review-only и review+fix, daily coding verbs/tools, security/high complexity, docs, browser, voice/image attachments, image, auxiliary, invalid project, overrides, scoped/global negation, word boundaries и collision prompts.

- Fixed-corpus result: `117/117`, accuracy `100%`.
- Targeted Stage 002 selection (`routing_config`, `routing_eval`, `execution_policy`, `routing`, `gateway_contracts`): `122 passed`; full suite: `189 passed`.
- Эта цифра относится только к versioned regression corpus, а не к произвольным будущим prompts.
- Warm pure routing CI ceiling: p95 `<25 ms` на 200 in-process fast cases. Real HTTP `/v1/route`, n=200: p50 1.198 ms, p95 1.538 ms, max 1.819 ms на текущей workstation/revision.
- `FOUNDATION_OK`, `DOCTOR_OK`, final `SMOKE_TEST_OK` (including 189 tests and real gateway→Qwen edit), Open WebUI fast `LOCAL_UI_OK`, read-only README-heading, live Context7 FastAPI lifespan и external `Example Domain` browser E2E green.

## Известные ограничения

1. Marker policy лучше прежнего unordered classifier, но остаётся bounded rules engine, а не semantic proof.
2. Planner видит последнюю user message; conversational project/action carry-over не реализован.
3. Capability snapshot кешируется на 5 секунд и использует enable flags, CLI/package/install checks и короткие TCP listener probes для fast/strong/voice; это ещё не полный versioned health registry или semantic probe с freshness SLA. Warm latency не описывает cold probe cost.
4. Chat voice bridge поддерживает bounded inline base64 `input_audio`, но не произвольные remote audio URLs; vision executor отсутствует.
5. Codex scoped approval ledger отсутствует, поэтому автоматический cloud execution закрыт fail-safe bundle behavior.
6. Browser adapter проверяет initial/redirect/HTTP(S)-subresource targets, но WebSocket/service-worker paths, общий outbound proxy/audit и полное DNS pinning/rebinding hardening не реализованы.
7. Worktree allowlist, symlink/junction containment и OS sandbox остаются этапом 005/010.
8. Route decision и attempt history сохраняются snapshot-ом, но не append-only event log.
