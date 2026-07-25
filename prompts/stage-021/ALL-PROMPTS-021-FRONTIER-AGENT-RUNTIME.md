# Stage 021 — Frontier Agent Runtime: полный пакет операторских промтов

> Статус документа: инструкции для будущей реализации.
>
> Наличие этого файла в репозитории **не означает**, что перечисленные функции уже реализованы, интегрированы или проверены. Фактический статус каждой возможности определяется только исходным кодом, воспроизводимыми проверками, артефактами evaluation и актуальными источниками истины проекта.
>
> Это долгосрочный пакет. Не запускай Stage 021 до реализации и аудита Stages 007–020 и всех зависимостей, явно указанных ниже.

Этот пакет разбивает Stage 021 на семнадцать последовательно выполняемых задач `021-00`–`021-16`. Он предназначен для запуска оператором по одному промту за раз внутри существующего проекта `${PROJECT_ROOT}`.

Не запускай все промты одним длинным заданием. Заверши текущий промт, проверь его gate, сохрани объективный отчёт и только затем переходи к следующему.

## Карта подэтапов

| Исходный подэтап | Промты |
|---|---|
| Подготовка и контроль изменений | `021-00` |
| 021-A — Current Capability Audit | `021-01`–`021-02` |
| 021-B — Agentic Model Lab | `021-03`–`021-05` |
| 021-C — Adaptive Deliberation Engine | `021-06`–`021-07` |
| 021-D — Frontier Coding Loop | `021-08`–`021-09` |
| 021-E — Universal Tool and Computer Runtime | `021-10`–`021-12` |
| 021-F — Multi-User Product Readiness | `021-13`–`021-14` |
| 021-G — Frontier Evaluation and Release Gate | `021-15`–`021-16` |

## Общий контракт выполнения

Следующие правила обязательны для каждого промта в этом файле.

### Проект и исходное состояние

- Работай только внутри существующего `${PROJECT_ROOT}`.
- Не начинай проект заново, не создавай конкурирующую платформу и не переписывай рабочую систему без объективной причины.
- Перед изменениями прочитай применимые `AGENTS.md`, `SYSTEM_MANIFEST.md`, `README.md`, `constitution/*`, связанные `docs/*`, `prompts/*`, contracts, schemas и tests.
- Перед каждым подэтапом запиши текущий Git HEAD, branch, `git status` и ownership незакоммиченного diff.
- Не изменяй, не форматируй и не удаляй чужие изменения.
- Если Coding Engine уже определяет branch/worktree policy, используй её. Не проводи опасные тесты в пользовательском основном worktree.
- Не используй destructive Git-команды, не переписывай историю и не выполняй push.
- Не создавай отдельный commit после каждого промта. Итоговый commit разрешён только в `021-16`, если существующая политика проекта прямо не требует другой безопасной схемы.

### Доказательность

- Не считай наличие файла, process, model tag, endpoint или HTTP 200 доказательством работоспособности.
- Для каждого компонента различай статусы: `documented`, `discovered`, `implemented`, `integrated`, `verified`, `degraded`, `blocked`, `not present`.
- Не выдумывай версии, benchmark numbers, context size, quantization, latency, TTFT, tokens/s, VRAM/RAM или success rate.
- Любая метрика должна иметь команду запуска, timestamp, конфигурацию, число runs и сохранённый машинно-читаемый результат.
- Любое заявление `verified` должно ссылаться на objective check или воспроизводимый E2E.
- Не скрывай failing, skipped, unavailable и inconclusive checks.
- Не заявляй, что система равна Codex, Claude Code, AGI или способна выполнить любую задачу.

### Безопасность и приватность

- Не меняй Constitution автоматически и не ослабляй Permissions.
- Если новая функция противоречит Constitution: останови только конфликтующую часть, зафиксируй конфликт, предложи отдельное изменение и жди явного approval.
- Не отправляй private code, файлы, память, аудио, изображения или документы в cloud без разрешённой политики и явного подходящего маршрута.
- Explicit route пользователя не обходит permissions, resource limits, cloud policy или approval.
- Не читай passwords, cookies, browser profiles, login/payment fields или не относящиеся к задаче пользовательские данные.
- Не выполняй push, deploy, publish, merge, force-push, массовые отправки, новые внешние recipients или production actions без отдельного явного разрешения.
- Не удаляй пользовательские проекты, модели, оригинальные media или n8n data.
- Не выполняй training, LoRA, изменение весов или «снятие цензуры».
- Не сохраняй сырой private reasoning или scratchpad. Разрешено хранить только решения, evidence, краткое описание отклонённых подходов, unresolved errors и provenance.
- Все fixtures, demo projects и evaluation inputs должны быть синтетическими и безопасными для публикации.
- Secrets передаются только через runtime environment или существующий secret store и никогда не попадают в tracked config, логи или отчёты.

### Совместимость

Нельзя ломать:

- Open WebUI;
- Gateway и `/v1` OpenAI-compatible boundary;
- `local-fast` и существующий `local-strong`;
- Qwen Code и Codex integration;
- routing, task state, memory и knowledge;
- voice endpoint, ComfyUI, Playwright и Context7;
- n8n data;
- start/stop/doctor/smoke;
- Git history, model files и пользовательские проекты;
- существующий Windows desktop workflow.

Если нужна migration, обязательны backward compatibility, backup, dry-run, rollback и objective verification.

### Архитектурные правила

- Переиспользуй существующие services, contracts, Tool Registry, MCP Hub, task state, memory, knowledge, worktree manager, GPU coordinator и evaluation harness.
- Не создавай пустые директории, фиктивные adapters, классы «на будущее» или второй источник истины.
- Каждый новый runtime-модуль обязан иметь реального consumer, versioned contract и тест.
- Ни один consumer не должен быть жёстко привязан к конкретному model ID.
- Не добавляй MCP, дублирующий встроенный filesystem, shell или Git без доказанной пользы.
- Не держи несколько тяжёлых моделей параллельно в одной GPU без измеренной необходимости.
- Предпочитай объективную проверку результату self-review модели.
- Создавай `services/deliberation`, `services/model_lab`, `services/frontier_runtime`, `services/windows_control`, `services/user_profiles`, соответствующие config/tests/evals или аналогичные модули только тогда, когда существующая структура не предоставляет подходящего места и у нового модуля сразу есть consumer и test.

### Отчёт каждого промта

В конце каждого промта сообщи:

1. исходный и итоговый HEAD;
2. исходный и итоговый `git status`;
3. какие файлы изучены;
4. какие файлы изменены;
5. какие реальные команды и calls выполнены;
6. какие tests прошли, не прошли или были пропущены;
7. какие метрики измерены;
8. какие capabilities остались `degraded`, `blocked` или `not present`;
9. выполнен ли gate текущего промта;
10. можно ли переходить к следующему промту.

Если безопасное продолжение требует внешнего действия, сначала выполни всё остальное, затем запроси у пользователя ровно одно конкретное действие.

---

## Промт 021-00 — Baseline, ownership и управление выполнением

Ты работаешь как Principal Platform Auditor внутри существующего `${PROJECT_ROOT}`.

Выполни только подготовительный read-only аудит Stage 021. Не реализуй функции, не устанавливай зависимости и модели, не изменяй tracked files, не создавай commit и не выполняй push.

### Цель

Получить доказанный baseline, отделить существующую реализацию от документации и определить безопасное место выполнения Stage 021.

### Обязательные действия

1. Зафиксируй Git HEAD, branch, worktrees, remotes без credentials и полный `git status`.
2. Определи ownership каждого существующего modified/untracked файла. Не считай изменения своими без доказательства.
3. Прочитай:
   - `AGENTS.md` и вложенную иерархию;
   - `SYSTEM_MANIFEST.md`;
   - `README.md`;
   - `constitution/*`;
   - связанные `docs/*`;
   - `prompts/*` и существующие пакеты предыдущих этапов;
   - services, scripts, schemas, adapters, tests и evals;
   - model profiles, routing, task state, worktree contracts;
   - memory, knowledge, MCP Hub и Tool Registry.
4. Фактически проверь, какие gates `000–020` подтверждены кодом и tests. Отдели:
   - документированное завершение;
   - реальную реализацию;
   - последнюю успешную проверку;
   - устаревшую или отсутствующую evidence.
5. Инвентаризируй текущие entrypoints:
   - Open WebUI;
   - Gateway;
   - CLI;
   - Telegram/API;
   - Qwen Code;
   - Codex;
   - model backend;
   - voice;
   - vision/images;
   - ComfyUI;
   - browser;
   - Windows integrations.
6. Определи существующую branch/worktree policy Coding Engine. Если Stage 021 должен выполняться отдельно, предложи безопасные branch/worktree names, но не создавай их при неясном ownership.
7. Составь dependency map `021-A → 021-G` и список hard blockers.

### Gate 021-00

Gate зелёный только если:

- исходное состояние воспроизводимо;
- чужой diff не затронут;
- известен реальный статус `000–020`;
- определены применимые Constitution/Permissions;
- определён безопасный execution worktree;
- нет неразрешённой неоднозначности, способной привести к потере данных.

Если gate не зелёный, остановись после точного отчёта. Не переходи к `021-01`.

---

## Промт 021-01 — Evidence-based Current Capability Audit

Ты работаешь как Principal AI Platform Auditor внутри существующего `${PROJECT_ROOT}`.

Зависимость: `021-00` имеет зелёный gate. Выполни только первую часть `021-A`. Не начинай реализацию новых runtime-компонентов.

### Цель

Создать честный фактический аудит текущего Frontier Agent Runtime.

### Обязательные исследования

Проверь реальными локальными probes:

- установленные модели и доступную metadata;
- model aliases и всех consumers;
- backend и его фактическую версию;
- quantization, заявленный и реально проверенный context;
- tool calling, chat template и reasoning parser;
- vision;
- фактическое GPU/CPU/RAM поведение;
- model load time, TTFT, prefill/decode speed, если их можно корректно измерить;
- Qwen Code и Codex availability без изменения глобального user profile;
- sandbox type;
- worktree lifecycle, ownership, cleanup и recovery;
- browser tools;
- MCP discovery, health и consumers;
- memory, retrieval и skills;
- Windows control;
- voice и реальную транскрибацию fixture;
- image generation и реальный artifact;
- task recovery, cancellation и restart;
- reviewer и verifier;
- evaluation coverage;
- multi-user isolation;
- известные failures.

Не запускай тяжёлые model comparisons и опасное Windows/application control. Используй bounded probes.

### Deliverable

Создай или актуализируй `docs/FRONTIER_AGENT_AUDIT.md`.

Для каждой способности зафиксируй:

- component/capability;
- status: `documented|discovered|implemented|integrated|verified|degraded|blocked|not present`;
- consumer;
- runtime/backend;
- locality и data boundary;
- permissions;
- evidence command/call;
- evidence timestamp;
- artifact/log reference без sensitive payload;
- last known failure;
- limitation;
- следующий objective check.

Не копируй старые показатели без повторной проверки. Неизвестные значения обозначай `unknown`, а не нулём.

### Обязательные проверки

- schema/format lint документа;
- ссылки ведут на существующие public-safe файлы;
- в документе нет secrets, machine-specific absolute paths, private project names или необработанных payload;
- выборочно воспроизведены минимум по одному health/functional check для основных boundaries.

### Gate 021-01

Gate зелёный, если все перечисленные категории получили честный статус и ни одна capability не названа `verified` без objective evidence.

---

## Промт 021-02 — Frontier Gap Analysis и Target Architecture

Ты работаешь как Principal Systems Architect и Security Architect внутри `${PROJECT_ROOT}`.

Зависимость: `021-01` зелёный. Выполни вторую часть `021-A`.

### Цель

На основании фактического аудита определить измеримый разрыв с системами класса современных coding agents и спроектировать целевую архитектуру без создания второй платформы.

### Deliverables

Создай или актуализируй:

- `docs/FRONTIER_GAP_ANALYSIS.md`;
- `docs/FRONTIER_TARGET_ARCHITECTURE.md`.

### Gap analysis

Для каждой категории укажи: реализовано, частично, отсутствует, дублируется, ненадёжно, не проверено или невозможно полностью повторить локально:

1. Model quality.
2. Agentic model behavior и provenance post-training.
3. Tool calling.
4. Terminal execution.
5. Repository understanding.
6. Long-context handling.
7. Context compression.
8. Skills.
9. Planner.
10. Router.
11. Adaptive reasoning.
12. Multiple attempts.
13. Critic.
14. Reviewer.
15. Objective verifier.
16. Error recovery.
17. Durable execution.
18. Sandbox.
19. Browser.
20. Windows control.
21. MCP.
22. Memory.
23. Personalization.
24. Voice.
25. Vision.
26. Images.
27. Multi-user security.
28. Product UX.
29. Evaluation.
30. Observability.

Для каждого gap укажи severity, user impact, existing reusable component, proposed change, evidence gate и риск regression.

### Target architecture

Опиши текущий и целевой поток:

`UI/API → Request Normalizer → Intent/Risk/Complexity → Adaptive Planner → Context Builder → Executor Selection → Deliberation → Execution → Objective Verification → Result/Evidence/Artifacts`.

Обязательно зафиксируй:

- trust и resource boundaries;
- Windows ↔ WSL ↔ Docker;
- local ↔ cloud boundary;
- model boundary;
- user-data boundary;
- application-control boundary;
- GPU coordination;
- task lifecycle;
- context lifecycle и compression;
- artifact/provenance lifecycle;
- approval points;
- failure isolation;
- cancellation/restart recovery;
- backward compatibility, backup, dry-run и rollback.

Отдельно отметь: Stage 021 не обучает модели и не меняет веса. Agentic post-training является проверяемым свойством выбранных готовых моделей, а не разрешением на LoRA/training.

### Gate 021-A

Проведи независимый read-only review документов. Gate зелёный только если:

- аудит и gap analysis не противоречат фактическому состоянию;
- reuse decisions исключают дублирование;
- trust/data/resource boundaries явны;
- migrations имеют rollback;
- blocked функции названы честно;
- ни одно изменение Constitution не применено.

Не переходи к `021-03`, пока gate не зелёный.

---

## Промт 021-03 — Model Registry и стабильные aliases

Ты работаешь как Model Platform Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-A` зелёный. Начни `021-B`, адаптируясь к существующим config/contracts.

### Цель

Отвязать consumers от конкретных model IDs и создать один versioned Model Registry.

### Обязательные aliases

- `local-fast`;
- `local-general`;
- `local-code`;
- `local-deep`;
- `local-vision`;
- `local-embedding`;
- `local-reranker`;
- `cloud-code`;
- `cloud-review`;
- `speech`;
- `image-generation`.

Сохрани backward compatibility с существующим `local-strong`: либо оставь его стабильным alias, либо создай проверенную migration mapping. Не удаляй его и не ломай текущие consumers.

### Model record

Для каждой модели schema должна поддерживать:

- `id`;
- `provider`;
- `backend`;
- `revision`;
- `quantization`;
- `parameter_count`;
- `active_parameter_count`;
- `context_window`;
- `tested_context`;
- `modalities`;
- `tool_calling`;
- `reasoning_mode`;
- `memory_vram`;
- `memory_ram`;
- `load_time`;
- `ttft`;
- `decode_speed`;
- `prefill_speed`;
- `agent_success_rate`;
- `coding_success_rate`;
- `tool_call_validity`;
- `false_completion_rate`;
- `known_failures`;
- `license`;
- `provenance`;
- `config_hash`;
- `status`.

Недоступные данные должны быть `null`/`unknown` и иметь provenance reason. Не выдумывай metrics.

### Реализация

- Найди существующий canonical config и расширь его; не создавай параллельный registry.
- Добавь versioned schema и migration/dry-run, если формат меняется.
- Переведи consumers на alias resolution.
- Добавь config hash, revision history и audit-safe alias history.
- Сохрани текущие defaults, пока benchmark не завершён.
- Не скачивай и не переключай модели в этом промте.

### Tests

- valid/invalid schema;
- duplicate IDs и aliases;
- dangling target;
- unsupported backend/modality;
- compatibility `local-strong`;
- consumer resolution без hard-coded IDs;
- redaction и отсутствие secrets;
- config migration dry-run и rollback.

### Gate 021-03

Registry является единственным источником истины, старые routes продолжают работать, а primary aliases ещё не менялись без benchmark.

---

## Промт 021-04 — Qualification моделей и agentic evaluation corpus

Ты работаешь как Model Evaluation Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-03` зелёный.

### Цель

Отобрать baseline и максимум 2–3 обоснованных кандидата и подготовить минимум 30 воспроизводимых agentic cases.

### Candidate preflight

Сначала фактически проверь уже установленные модели. Для каждого потенциального кандидата используй актуальную официальную model card или локальную metadata и проверь:

- точное имя, revision и source;
- license;
- размер weights и свободное место;
- ожидаемую VRAM/RAM;
- доступный quant;
- backend compatibility;
- CUDA/driver compatibility;
- chat template;
- tool parser;
- reasoning parser;
- vision requirements;
- context claim и реалистичный tested context;
- expected load/unload behavior.

Если соответствующие официальные releases действительно существуют и поддерживаются текущим backend, рассмотри кандидатов:

- `Qwen3.6-35B-A3B`;
- актуальную agentic coding-модель Qwen;
- North Mini Code либо актуальный сопоставимый agentic coding candidate;
- Devstral Small либо актуальный Devstral;
- другие официальные open-weight coding/agentic модели, помещающиеся в доступное железо;
- текущую baseline model.

Не принимай ни одно название, revision или benchmark из старой документации на веру. Отсутствующая официальная model card означает, что кандидат не допускается к download/evaluation.

Не устанавливай десятки моделей. Набор: текущий baseline плюс максимум 2–3 кандидата.

Если нужная большая модель отсутствует, заверши disk/VRAM/license/backend review и запроси ровно одно конкретное разрешение на точный download. Не загружай большие weights автоматически.

### Evaluation corpus

Переиспользуй Stage 012 harness. Создай или расширь минимум 30 public-safe fixture cases, включая:

- repository discovery и чтение;
- поиск правильного файла;
- read-only inspection;
- маленький bug fix;
- UI fix по screenshot fixture;
- добавление теста;
- repair failing test;
- Docker issue;
- multi-file change;
- valid и invalid tool call;
- recovery после ошибочной команды;
- safe stop при недостаточном context;
- Codex handoff;
- browser QA;
- запрет false completion при failing tests;
- соблюдение `AGENTS.md`;
- длинную сессию;
- работу после context compression;
- prompt-injection fixture;
- no-push policy;
- cancellation;
- timeout;
- model/tool unavailable;
- лишний diff;
- смену стратегии;
- objective verifier;
- reviewer rejection;
- artifact/provenance;
- cloud privacy boundary.

У каждого case должны быть deterministic setup/cleanup, acceptance criteria, allowed paths/tools, timeout, objective scorer и ожидаемый failure mode.

### Tests

- eval manifest schema;
- fixture isolation;
- deterministic reset;
- no private inputs;
- scorer не использует self-claim модели как success;
- timeout/cancel;
- public-safe artifact paths.

### Gate 021-04

Кандидаты имеют проверенный preflight, evaluation corpus содержит не менее 30 независимых cases, а ни один benchmark result ещё не выдуман.

---

## Промт 021-05 — Model benchmark, alias selection и GPU lifecycle

Ты работаешь как Model Evaluation и GPU Runtime Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-04` зелёный и все разрешённые кандидаты доступны.

### Цель

Сравнить модели на реальных задачах и назначить aliases только на основании evidence.

### Execution policy

- Запускай тяжёлые модели последовательно.
- Перед каждым run фиксируй backend/version, model revision, quant, context, parser, temperature/seed, GPU/CPU/RAM state.
- Не вытесняй unexpectedly текущую strong coding model.
- Не запускай ComfyUI параллельно без GPU coordinator.
- Используй warm и cold measurements отдельно.
- Повтори достаточное число runs для оценки нестабильности.

### Метрики

Измеряй:

- objective success;
- pass@1;
- retry success;
- attempts;
- tool-call validity;
- false completion;
- user intervention proxy;
- total latency и TTFT;
- load, prefill, decode;
- GPU/VRAM/RAM;
- unnecessary diff;
- violated rules;
- strategy change;
- handoff quality.

Не публикуй WER-like, success-rate или speed без состава набора и числа runs.

### Alias selection

- `local-fast`: самая быстрая модель, проходящая routing/chat/tool gates.
- `local-code`: лучшая на coding и tool suites.
- `local-deep`: лучшая на planning/review/reasoning suites.
- `local-vision`: лучшая реально проверенная vision model.
- Остальные aliases назначай по соответствующей objective suite.

Одна модель может занимать несколько профилей. Не переключай primary alias после одного удачного run.

### Runtime

Реализуй или укрепи:

- health и readiness;
- safe load/unload;
- timeout/cancel;
- fallback;
- rollback;
- canary;
- alias history;
- model unavailable behavior;
- GPU locks и resource classes;
- recovery после failed load;
- compatibility с Open WebUI/Gateway/Qwen Code.

### Tests

- switching success/failure;
- load timeout;
- failed canary rollback;
- alias history;
- concurrent GPU contention;
- ComfyUI coordination;
- stale lock cleanup;
- fallback не меняет privacy/cloud policy;
- baseline route regression.

### Gate 021-B

Gate зелёный, если aliases основаны на сохранённых benchmark artifacts, switching/fallback/rollback проверены, несколько тяжёлых моделей не конкурируют без policy, а существующие model routes не сломаны.

---

## Промт 021-06 — FAST/SMART/DEEP/AUTO contracts и routing

Ты работаешь как Adaptive Reasoning Runtime Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-B` зелёный. Начни `021-C`.

### Цель

Реализовать versioned deliberation contract и безопасный выбор режима без показа сырого scratchpad.

### Режимы

`FAST`:

`один model pass → минимальная объективная проверка → ответ`.

Используется для короткого chat, перевода, форматирования, простого объяснения и deterministic tool call.

`SMART`:

`первичное решение → critic/review → при необходимости revision → ответ`.

Используется для средней coding-задачи, документа, planning, нескольких tools и небольшого debugging.

`DEEP`:

`Explorer → structured plan → 2–4 независимых кандидата/гипотезы → Critic → tool verification → independent reviewer → Synthesizer`.

Используется для architecture, security, concurrency, migration, большого изменения, сложного debugging и задач после failed local cycle.

`AUTO` выбирает режим по complexity, risk, ambiguity, subsystem count, tools, context, failure history, cost of error, explicit route и текущим resources.

### Contract

Реализуй versioned schema минимум с полями:

```json
{
  "mode": "fast|smart|deep",
  "goal": "",
  "constraints": [],
  "candidate_count": 1,
  "verification_plan": [],
  "tool_budget": {},
  "model_budget": {},
  "time_budget": {},
  "stop_conditions": [],
  "escalation_policy": {},
  "output_policy": {}
}
```

Добавь request/task/user IDs только согласно существующим contracts и privacy boundary.

### Explicit routes

Поддержи `/fast`, `/smart`, `/deep`, `/local`, `/codex`, `/review`, `/voice`, `/vision`, `/image`, `/browser`, `/computer`.

Explicit route не обходит risk classification, user permissions, approval, locality, cloud egress или resource limits.

### Integration

- Переиспользуй Gateway Request Normalizer и router.
- Не создавай отдельный несовместимый endpoint.
- Сохрани Open WebUI и `/v1`.
- Отдели user-visible status от private deliberation state.
- Добавь deterministic classifier fallback, если модель недоступна.

### Tests

- schema versions;
- explicit route parsing;
- AUTO mode boundaries;
- risk/complexity ambiguity;
- budget calculation;
- unavailable model/tool;
- privacy/cloud denial;
- user profile restrictions;
- existing route regressions;
- Open WebUI → Gateway → mode selection.

### Gate 021-06

Все четыре режима выбираются предсказуемо, explicit routes безопасны, existing routing сохранён, raw scratchpad не возвращается клиенту.

---

## Промт 021-07 — Candidate, Critic, Verifier, Synthesizer и recovery

Ты работаешь как Deliberation, Verification и Reliability Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-06` зелёный.

### Цель

Создать реально работающий evidence-driven deliberation loop.

### Внутренние роли

Минимум:

- Candidate Generator;
- Critic;
- Verifier;
- Synthesizer.

Переиспользуй существующих specialized agents только если их consumers, contracts и tests фактически существуют.

### Best-of-N

- Используй только для обоснованных SMART/DEEP задач.
- Создавай 2–4 независимых кандидата без общего scratchpad.
- Не запускай пять тяжёлых models параллельно на одной GPU.
- Выполняй sequentially или ограниченно parallel только независимые CPU tasks.
- Ранжируй по заранее заданным objective criteria.
- Проверяй факты и claims tools/evidence.

### Critic

Ищет:

- пропущенные требования;
- неверные предположения;
- Constitution/Permissions violations;
- отсутствие evidence;
- false completion;
- security risk;
- слишком широкий diff;
- неправильный tool;
- missing failure mode;
- конфликт с актуальными файлами.

Critic не исправляет результат скрытно. Любое исправление проходит traceable revision cycle.

### Verifier

Для кода использует tests, lint, typecheck, build, static analysis, Git diff, Playwright и screenshots.

Для фактов использует разрешённые актуальные источники, локальную knowledge base и provenance.

Для изображений проверяет artifact, dimensions, metadata, workflow result и visual inspection.

Для Windows проверяет observable UI/application state, screenshot, сохранённую копию и undo/copy evidence.

### Synthesizer

Получает goal, constraints, candidates, critic findings, verifier evidence и unresolved limitations. Возвращает только user-facing result, краткое решение, evidence, checks, limitations, changed files, artifacts и следующий шаг.

### Boundaries и recovery

Добавь:

- max model passes;
- max tool calls;
- max attempts;
- max wall time;
- max context;
- cancel/timeout;
- loop detection;
- repeated-output detection;
- strategy change после двух сходных failures;
- Codex escalation;
- safe stop;
- restart recovery;
- private scratchpad redaction и отсутствие persistence.

### Tests

- candidate independence;
- objective ranking;
- critic findings и revision trace;
- verifier rejects unsupported success;
- synthesis не раскрывает scratchpad;
- timeout/cancel;
- loop/repeated output;
- changed strategy;
- Codex escalation policy;
- restart recovery;
- audit logs не содержат private reasoning/secrets.

### Gate 021-C

FAST/SMART/DEEP/AUTO работают E2E, Critic и Verifier используют evidence, false success отклоняется, а raw reasoning отсутствует в API, logs, memory и artifacts.

---

## Промт 021-08 — Frontier Coding Loop core

Ты работаешь как Staff Coding-Agent Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-C` зелёный. Начни `021-D`.

### Цель

Укрепить существующий Coding Engine до надёжного durable loop, не создавая второй coding agent.

### Workflow

Реализуй через существующие contracts:

1. Resolve repository.
2. Read applicable `AGENTS.md` hierarchy.
3. Inspect.
4. Build bounded repository map.
5. Define acceptance criteria.
6. Select executor.
7. Create isolated worktree.
8. Create failing test when appropriate.
9. Implement minimal diff.
10. Run targeted checks.
11. Diagnose failure.
12. Retry с изменённой стратегией.
13. Run broader checks.
14. Review diff.
15. Browser QA when applicable.
16. Independent review.
17. Local commit only if policy allows.
18. Report result/evidence.

### Read-only guarantee

Запросы «объясни», «прочитай», «найди», «проведи review», «покажи архитектуру» не должны:

- менять файлы;
- устанавливать dependencies;
- запускать mutating scripts;
- создавать worktree без необходимости;
- создавать commit.

Проверь enforcement на уровне policy/executor, а не только prompt.

### Local route

Предпочитай local-code для простых fixes, boilerplate, tests, docs, small UI, понятного bug, знакомого project и low risk.

Codex route пока только подготавливается; сам handoff завершается в `021-09`.

### Надёжность

- canonical repository/path resolution;
- path-scope enforcement;
- worktree ownership и cleanup;
- same-worktree serialization;
- independent-worktree concurrency;
- durable task state;
- cancellation/restart;
- no push;
- bounded logs/artifacts;
- secret redaction;
- context compression.

### Tests

- read-only no mutation;
- invalid project path;
- small bug;
- failing test repair;
- multi-file minimal diff;
- Docker fixture;
- cancellation;
- restart;
- locking/serialization;
- independent worktrees;
- secret prevention;
- no push;
- local commit policy;
- context compression.

### Gate 021-08

Local coding loop проходит focused fixtures, read-only технически enforced, worktrees не остаются orphaned, existing Qwen Code/Gateway contracts сохранены.

---

## Промт 021-09 — Codex handoff, independent review и Coding E2E

Ты работаешь как Hybrid Coding Runtime и Review Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-08` зелёный.

### Local vs Codex routing

Codex рекомендуется для architecture, security, migration, concurrency, большого diff, unknown failure, сложного нового проекта, final review, двух неудачных local cycles и critical production-adjacent change.

Cloud route требует разрешённой user/cloud policy. Private files не отправляются автоматически.

### Handoff bundle

Versioned bundle должен содержать:

- goal;
- repository;
- branch/worktree;
- applicable `AGENTS.md`;
- constraints;
- acceptance criteria;
- route decision;
- inspected files;
- modified files;
- diff summary;
- commands;
- failures;
- bounded logs excerpt;
- artifacts;
- attempts;
- rejected approaches summary;
- unresolved questions;
- verification plan.

Не включай secrets, raw reasoning, лишние files или неразрешённые private data. Codex не должен повторять discovery с нуля, если bounded context уже собран.

При недоступности Codex создай durable inbox/status и продолжи безопасные local parts. Не маскируй quota/auth/unavailable как success.

### Review independence

Reviewer получает task, requirements, relevant contracts, diff, tests, artifacts и evidence. Он не получает полный внутренний разговор Implementer.

Reviewer ищет incomplete implementation, regression, security, missing tests, unnecessary changes, convention violations и false success.

### Обязательные 20 Coding E2E

1. Read-only inspection.
2. Small bug.
3. UI fix from screenshot.
4. Multi-file feature.
5. Failing test repair.
6. Docker issue.
7. Invalid explicit project path.
8. Two local failures → Codex handoff.
9. Codex unavailable → durable inbox.
10. Cancellation.
11. Restart recovery.
12. Same-worktree serialization.
13. Independent worktrees.
14. Secret leak prevention.
15. No push.
16. Local commit policy.
17. Browser QA.
18. Context compression during long task.
19. DEEP versus direct.
20. False-success detection.

Используй fixture repositories и fake/mock cloud boundary там, где live Codex не разрешён. Отдельно пометь mock contract test и настоящий bounded live call.

### Gate 021-D

Critical coding E2E зелёные, reviewer независим, Codex handoff содержит достаточный bounded context, unavailable route durable, а no-push/private-cloud policy технически enforced.

---

## Промт 021-10 — Universal Tool Runtime и MCP governance

Ты работаешь как Tool Runtime и MCP Integration Architect внутри `${PROJECT_ROOT}`.

Зависимость: `021-D` зелёный. Начни `021-E`.

### Цель

Дать planner/deliberation единый безопасный способ выбирать существующие tools без дублирования Tool Registry или MCP Hub.

### Tool priority

Для каждой capability предпочитай:

1. официальный API;
2. официальный scripting API;
3. plugin/extension;
4. CLI;
5. MCP;
6. Windows UI Automation;
7. accessibility tree;
8. vision-assisted control;
9. coordinate click только как experimental last resort.

### Интеграция

- Используй canonical Tool Registry.
- Свяжи capability, consumer, input/output schema, locality, data egress, permissions, risk, resource class, timeout, cancel, retry, locks, audit, redaction, health и degraded state.
- Подключи к planner и verifier существующие browser, voice, vision, image, documents, memory, diagnostics и automation capabilities.
- Сохрани Telegram и n8n как consumers общих task/tool contracts; не создавай для них отдельный путь исполнения и не выполняй live external send в fixtures.
- Не создавай новый proxy для всех tools.
- Ошибка optional tool не должна ломать chat/coding platform.

### MCP

Все MCP проходят через Tool Registry, permissions, health, timeout, bounded retry, cancellation, audit, redaction и resource locks.

Фактически проверь существующие Context7 и Playwright. GitHub включай только при конкретном workflow и безопасной auth policy. Не добавляй filesystem/shell/Git MCP, если executor умеет это нативно.

### Tests

- registry/schema consistency;
- duplicate capability;
- route → tool selection;
- MCP discovery/call;
- broken MCP isolation;
- timeout/cancel/retry;
- disabled/degraded propagation;
- audit redaction;
- resource locks;
- voice/vision/image/browser explicit routes;
- no permission bypass.

### Gate 021-10

Planner реально вызывает нужные capabilities через существующий registry, MCP не дублирует native tools, failure isolation и privacy boundaries проверены.

---

## Промт 021-11 — Secure Windows Control boundary

Ты работаешь как Windows Automation и Security Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-10` зелёный.

### Цель

Создать или объективно классифицировать безопасную границу Windows Control.

### Сначала аудит

Фактически проверь результаты Stage 014 и существующие Windows adapters. Если dependencies не реализованы, не создавай фиктивную working integration. Подготовь минимальный реальный adapter только в существующей архитектуре или оставь capability `BLOCKED/NOT_CONFIGURED` с точной причиной.

### Минимальный contract

- `list_applications`;
- `list_windows`;
- `launch_application`;
- `focus_window`;
- `inspect_ui_tree`;
- `find_element`;
- `invoke_element`;
- `set_text`;
- `send_approved_shortcut`;
- `open_file`;
- `save_as_copy`;
- `take_screenshot`;
- `wait_for_state`;
- `cancel_action`;
- `get_action_status`.

Не реализуй методы, которые нельзя реально проверить.

### Security boundary

- localhost only;
- authentication и short-lived session token;
- application allowlist;
- action/shortcut allowlist;
- visible activity indicator;
- emergency stop;
- no password fields;
- no login/payment dialogs;
- no unrestricted keyboard injection;
- no hidden control unrelated apps;
- no destructive overwrite;
- save-as-copy и backup;
- screenshots и observable state;
- structured audit без sensitive payload;
- timeout/cancel;
- rollback/undo where possible.

### Verification

Используй отдельное безопасное Windows fixture application. Не управляй пользовательскими рабочими приложениями или файлами.

Tests:

- auth/session expiry;
- allowlist deny;
- password/payment deny;
- emergency stop;
- timeout/cancel;
- visible action status;
- save-as-copy;
- original unchanged;
- screenshot/state evidence;
- audit redaction;
- Tool Registry integration;
- restart/orphan cleanup.

### Gate 021-11

Windows Control получает `verified` только после реального fixture E2E и emergency-stop test. Иначе остаётся честно `degraded`, `blocked` или `not configured`.

---

## Промт 021-12 — ComfyUI и creative application workflows

Ты работаешь как Image/Creative Application Integration Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-11` завершён с честным status.

### ComfyUI

Переиспользуй существующий adapter и реализуй/проверь:

- versioned workflow;
- checkpoint/custom-node validation;
- GPU lock;
- submit/status/progress/cancel;
- timeout/recovery;
- result validation;
- seed/parameters/workflow hash;
- artifact/provenance;
- no overwrite оригинала.

### Photoshop

Не считай запуск приложения интеграцией. Working E2E требует:

1. открыть fixture;
2. создать рабочую копию;
3. выполнить контролируемое изменение через официальный script/plugin/API или проверенную UI Automation;
4. сохранить копию;
5. проверить artifact;
6. передать копию в ComfyUI;
7. выполнить versioned workflow;
8. получить и проверить result;
9. сохранить provenance;
10. доказать, что оригинал не изменён.

Если подходящий adapter отсутствует, зафиксируй required adapter и `BLOCKED`, не имитируй success.

### DaVinci Resolve

Только отдельный test project:

- fixture media;
- test timeline;
- одно безопасное изменение;
- test render;
- artifact validation;
- пользовательские projects неизменны.

### Blender

Предпочитай официальный Python API:

- test scene;
- создать/изменить объект;
- сохранить копию;
- render preview;
- validate artifact;
- пользовательская scene неизменна.

### Общие tests

- application unavailable;
- adapter unavailable;
- approval denied;
- original hash unchanged;
- artifact/provenance;
- cancellation/restart;
- GPU contention;
- invalid workflow/node/checkpoint;
- Tool Registry permissions;
- no hidden UI automation.

Не выполняй real application control без требуемого approval. Полностью проверяй остальные безопасные local/mocked contracts.

### Gate 021-E

ComfyUI имеет реальный bounded E2E. Windows/Photoshop/DaVinci/Blender названы working только при полном E2E; все остальные имеют точный degraded/blocked status. Existing voice, vision, image, browser и coding workflows не сломаны.

---

## Промт 021-13 — Multi-user identity, permissions и isolation

Ты работаешь как Multi-Tenant Security Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-E` завершён. Начни `021-F`.

### Цель

Сделать owner/trusted/guest/demo profiles технически изолированными, а не только описанными в prompt.

### Profiles

Создай или расширь существующий versioned user-permission contract для:

- owner;
- trusted user;
- guest;
- demo.

Для каждого задай:

- allowed workspaces;
- memory scope;
- tool permissions;
- cloud permissions;
- model budget;
- Windows actions;
- external actions;
- session retention;
- artifact access.

Не создавай параллельную identity system, если Gateway/Open WebUI уже предоставляет проверяемый user identity.

### Isolation

- отдельные conversations;
- task states;
- memory scopes;
- artifacts;
- worktrees;
- audit records;
- no cross-user retrieval;
- guest без owner profile;
- no implicit Desktop/Documents;
- no owner Codex account без permission;
- no model alias/Constitution changes;
- no push/deploy;
- no unrestricted Windows control.

### Tests

- owner/trusted/guest/demo matrix;
- cross-user memory;
- cross-user artifacts;
- task/worktree/audit isolation;
- out-of-scope path;
- cloud denial;
- Codex denial;
- Windows denial;
- external action denial;
- retention/delete;
- restart/session expiry;
- prompt injection attempts to change profile;
- permission override;
- guest cannot enumerate owner resources.

### Gate 021-13

Gate зелёный только при технически доказанном отсутствии cross-user leakage и implicit owner access.

---

## Промт 021-14 — Capability UX, truthful status и Demo mode

Ты работаешь как Product Integration и Reliability Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-13` зелёный.

### Onboarding

Приглашённый пользователь должен:

1. открыть один URL;
2. создать или выбрать guest profile;
3. увидеть разрешённые capabilities;
4. дать задачу обычным языком;
5. получить status;
6. получить result и evidence;
7. не изучать n8n, Gateway, Whisper или MCP.

Не публикуй сервис в Internet автоматически. Используй существующую безопасную access/auth boundary.

### Capability UX

Показывай понятные действия:

- написать или исправить код;
- проанализировать repository;
- расшифровать audio;
- сделать summary;
- проанализировать image;
- создать image;
- проверить site;
- выполнить разрешённую Windows task;
- передать сложную coding task Codex.

Внутренние названия доступны только в diagnostics.

### Status UX

Canonical task state должен отображать:

- принято;
- планируется;
- выполняется;
- ожидает разрешения;
- проверяется;
- завершено;
- завершено частично;
- не выполнено;
- отменено.

Не показывай synthetic timer или ложный progress. Status строится из реального task state.

### Demo mode

Создай изолированные public-safe scenarios:

1. исправление fixture project;
2. browser QA;
3. voice → transcript → summary;
4. image generation;
5. Windows fixture application;
6. direct vs DEEP comparison;
7. local task + Codex review contract.

Demo не использует owner data, private projects, real recipients или production actions.

### Сквозные пользовательские journeys

Проверь через один интерфейс три сценария.

`Исправь кнопку на этом скриншоте`:

1. принять screenshot;
2. определить разрешённый project;
3. выбрать vision;
4. локализовать проблему;
5. выбрать SMART или DEEP;
6. выбрать local-code или разрешённый Codex route;
7. создать worktree;
8. внести минимальный fix;
9. запустить fixture project;
10. проверить Playwright;
11. провести independent review;
12. вернуть result, evidence и artifacts.

`Создай сложную систему управления клиентами`:

1. определить high complexity;
2. собрать недостающие requirements;
3. построить plan;
4. подготовить bounded Codex bundle;
5. вызвать Codex только при разрешённом cloud route;
6. сохранить durable task state;
7. проверить result;
8. продолжить доступные local parts при quota/unavailable.

`Открой тестовое изображение в Photoshop, обработай копию через ComfyUI и верни результат`:

1. проверить реальный Photoshop adapter;
2. запросить required approval;
3. сохранить original;
4. создать working copy;
5. выполнить observable verified action;
6. передать copy в ComfyUI;
7. получить и проверить artifact;
8. вернуть result и provenance.

Если Photoshop integration не проверена, система сообщает отсутствующую capability, необходимый adapter, автоматически доступную часть и ровно одно требуемое действие пользователя.

### Tests

- one-URL onboarding;
- capability filtering by profile;
- truthful task transitions;
- partial/failed/cancelled states;
- unavailable tool/model/Codex;
- demo reset;
- demo data isolation;
- no external send;
- no owner retrieval.

### Gate 021-F

Guest/demo workflow работает на fixtures, пользователь видит capabilities и evidence, а isolation/status не основаны на декларациях.

---

## Промт 021-15 — Comparative Frontier Evaluation

Ты работаешь как Evaluation, Statistics и AI Reliability Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-F` зелёный. Начни `021-G`.

### Цель

Объективно сравнить архитектурные конфигурации и измерить фактический разрыв между local runtime и разрешённым cloud executor.

### Конфигурации

Минимум:

- direct `local-fast`;
- direct `local-strong`;
- direct `local-code`;
- SMART;
- DEEP;
- local agent без reviewer;
- local agent с reviewer;
- AUTO router;
- Codex;
- local implementation + Codex review.

Live cloud runs выполняй только при разрешённой policy. Иначе отдели contract/mock result от `not run`.

### Категории

- chat;
- planning;
- coding;
- debugging;
- architecture;
- tool use;
- browser;
- Windows;
- memory;
- context;
- voice;
- vision;
- images;
- multi-user isolation;
- safety;
- recovery.

### Метрики качества

- task success;
- pass@1;
- retry success;
- objective checks;
- route accuracy;
- tool-call validity;
- false completion;
- reviewer rejection;
- user intervention;
- acceptance-criteria coverage;
- unnecessary changes;
- rollback frequency.

### Метрики производительности

- total latency;
- TTFT;
- planning/model/tool/queue time;
- load time;
- GPU/VRAM/RAM;
- attempts;
- context size;
- compression events.

### Метрики надёжности

- restart recovery;
- cancellation;
- timeout;
- orphan processes;
- stale locks;
- duplicate external actions;
- artifact corruption;
- task loss.

### Метрики безопасности

- secret leak;
- cross-user leak;
- out-of-scope access;
- unauthorized push/deploy;
- unsafe Windows action;
- private cloud egress;
- permission bypass.

### Методика

- До runs зафиксируй versioned case set, thresholds, scoring и tie-break rules.
- Не подстраивай threshold после просмотра результата.
- Используй одинаковые fixtures и acceptance criteria.
- Разделяй deterministic objective checks и human/reviewer rubric.
- Сохраняй raw machine metrics без private prompt/reasoning.
- Повторяй stochastic cases.
- Для medium/high complexity выполни A/B:
  - Direct;
  - SMART;
  - DEEP;
  - Codex.
- Покажи, где multi-pass помогает, где только увеличивает latency, где local model плохо оценивает варианты, где verifier исправляет ситуацию и где нужен Codex.

### Deliverables

- versioned manifests в существующем `evals` layout;
- machine-readable results;
- `docs/FRONTIER_EVALUATION.md`;
- measured baseline для doctor;
- reproducibility commands;
- limitations и unavailable configurations.

### Gate 021-15

Gate зелёный, если результаты воспроизводимы, ни одна metric не выдумана, direct/SMART/DEEP/Codex разделены корректно и разрыв описан без маркетинговых заявлений.

---

## Промт 021-16 — Final release gate, operations, security и commit

Ты работаешь как Product Reliability Lead, Security Reviewer и Release Engineer внутри `${PROJECT_ROOT}`.

Зависимость: `021-15` зелёный. Это единственный промт, которому разрешено создать итоговый local commit при выполнении всех условий. Push запрещён.

### 1. Полная проверка ownership

- Запиши исходный HEAD/status.
- Сопоставь весь diff со Stage 021.
- Не включай чужие изменения.
- Проведи независимый read-only review полного diff.
- Исправь findings и повтори relevant tests.

### 2. Обязательные unit tests

- schemas;
- routing;
- mode selection;
- budgets;
- candidate ranking;
- Critic;
- Verifier;
- Synthesizer;
- Model Registry;
- model switching;
- permissions;
- profile isolation;
- task state;
- loop detection;
- retry;
- timeout;
- cancellation.

### 3. Обязательные integration tests

- Open WebUI → Gateway → mode selection;
- Gateway → local model;
- Gateway → Qwen Code;
- Gateway → Codex handoff;
- Deliberation → tools;
- Reviewer → evidence;
- Tool Registry → MCP;
- Tool Registry → Windows Control;
- GPU coordinator → model/ComfyUI;
- user profile → scoped memory;
- user profile → scoped artifacts.

### 4. Обязательные Frontier E2E

1. Fast chat.
2. Smart analysis.
3. Deep architecture question.
4. Small local coding fix.
5. Complex coding → Codex.
6. Screenshot → local fix → Playwright.
7. Voice → transcript → summary.
8. Sketch → ComfyUI.
9. Windows test app.
10. Photoshop test copy — только при проверенной integration.
11. Blender test scene — только при проверенной integration.
12. Guest isolation.
13. Restart during long task.
14. Cancellation.
15. Tool unavailable.
16. Codex unavailable.
17. Model unavailable.
18. GPU contention.
19. Context compression.
20. False-success rejection.

Skipped conditional application E2E не считать passed. Они должны оставаться `NOT_CONFIGURED/BLOCKED`.

### 5. Обязательные security tests

- secret input;
- prompt injection in repository;
- prompt injection in skill;
- prompt injection in document;
- cross-user memory;
- out-of-scope path;
- arbitrary shell injection;
- unsafe Windows action;
- push attempt;
- deploy attempt;
- cloud egress;
- new recipient;
- payment dialog;
- mass send;
- permission override.

### 6. Обязательные performance/reliability tests

- fast/smart/deep latency;
- model load;
- context prefill;
- Q4/Q5 comparison только для реально доступных candidates;
- memory/GPU usage;
- ComfyUI contention;
- sequential multi-pass;
- repeated long session;
- queue behavior;
- restart recovery;
- stale locks;
- orphan processes;
- duplicate external-action protection;
- artifact/task durability.

### 7. Doctor и smoke

Расширь существующие `scripts/doctor.ps1` и `scripts/smoke-test.ps1`, не создавая параллельную диагностику.

Doctor показывает:

- model profiles;
- active backend;
- model health;
- tool calling;
- Codex;
- Qwen Code;
- deliberation engine;
- reviewer;
- MCP;
- Windows Control;
- GPU coordinator;
- user isolation;
- frontier eval baseline.

Используй только статусы `PASS`, `WARN`, `FAIL`, `DEGRADED`, `NOT_CONFIGURED`.

Smoke должен иметь быстрый режим. Полные model comparisons и Windows application tests не запускаются при каждом старте.

Выполни финальный цикл существующими `scripts/stop.ps1` → `scripts/start.ps1` → `scripts/doctor.ps1` → quick `scripts/smoke-test.ps1` → full smoke. Используй реальные параметры, определённые самими scripts/документацией; не выдумывай flags. Не скрывай duration и failures.

### 8. Документация и источники истины

Аккуратно обнови только по фактической evidence:

- `SYSTEM_MANIFEST.md`;
- `docs/CURRENT_STATE.md`;
- `docs/TARGET_ARCHITECTURE.md`;
- `docs/ROADMAP.md`;
- `docs/CAPABILITY-MATURITY.md`;
- `docs/OPERATIONS.md`;
- `docs/SECURITY_MODEL.md`;
- `docs/PERMISSIONS.md`;
- `docs/CONTEXT_STRATEGY.md`;
- `docs/CODEX_HANDOFF.md`;
- Model Registry;
- Tool Registry;
- evaluation manifests;
- `docs/FRONTIER_OPERATIONS.md`, если он реально нужен и не дублирует Operations.

Не меняй Constitution. Не обозначай conditional/blocked integrations как завершённые.

### 9. Secret/privacy/publication audit

- Выполни repository secret scan.
- Проверь staged diff отдельно.
- Проверь отсутствие tokens, cookies, private prompts, raw reasoning, personal paths, private project names и generated user artifacts.
- Проверь, что private data не ушли в cloud.
- Проверь cross-user isolation и audit redaction.
- Проверь отсутствие unauthorized push/deploy.

### 10. Release status

`FRONTIER_READY_LOCAL` допустим только если:

- critical coding E2E зелёные;
- DEEP объективно лучше direct на заранее выбранном наборе;
- false-success ниже заранее установленного threshold;
- нет push/deploy, secret или cross-user leaks;
- task recovery работает;
- tool failures не маскируются;
- Windows emergency stop реально проверен;
- friend/demo mode изолирован;
- aliases основаны на benchmark;
- Codex handoff работает.

`FRONTIER_READY_HYBRID` дополнительно требует:

- explicit cloud policy;
- private files не уходят автоматически;
- Codex handoff проверен live либо статус ограничен;
- quota/unavailable корректно обрабатываются;
- local continuation работает.

Если условия не выполнены, используй `READY_WITH_GAPS` или `BLOCKED`. Не используй «равно Codex», «равно Claude», «AGI» или «выполняет любую задачу».

### 11. Gate 021-G

Финальный gate зелёный только если:

- проведены честный audit и gap analysis;
- aliases отвязаны от конкретных IDs и основаны на benchmark;
- agentic candidates проверены на реальных fixtures;
- FAST/SMART/DEEP/AUTO реально работают;
- Critic и Verifier используют objective evidence;
- raw scratchpad не показывается и не сохраняется;
- coding loop и Codex handoff проходят применимые E2E;
- после двух сходных failures меняется strategy;
- MCP и Tool Registry не дублируются;
- Windows Control имеет безопасную проверенную boundary либо честный blocked status;
- creative applications не названы working без E2E;
- owner и guest изолированы;
- friend/demo mode работает на fixtures;
- direct/SMART/DEEP/Codex сравнение завершено;
- нет secret leaks и unauthorized push/deploy;
- restart recovery работает;
- existing platform не сломана;
- Manifest/docs соответствуют evidence;
- full diff проверен.

### 12. Commit gate

Создай local commit `Build frontier-grade adaptive agent runtime` только если:

- весь diff однозначно принадлежит Stage 021;
- все mandatory applicable checks зелёные;
- conditional unavailable checks честно отражены и не нарушают выбранный release status;
- secret scan чист;
- independent reviewer принял diff;
- runtime и backward compatibility не сломаны.

Иначе не создавай commit и укажи точную причину. Не push.

### 13. Финальный отчёт — строго 30 пунктов

1. Исходный HEAD и состояние worktree.
2. Какие этапы `000–020` фактически завершены.
3. Какие существующие компоненты переиспользованы.
4. Какие gaps найдены.
5. Какие модели протестированы.
6. Какие модели назначены profiles и почему.
7. Фактические quant/context/backend.
8. Результаты agentic model evaluation.
9. Как работают FAST/SMART/DEEP/AUTO.
10. Насколько SMART/DEEP лучше direct baseline.
11. Как работает Critic.
12. Как работает Verifier.
13. Как работает coding loop.
14. Как работает Codex handoff.
15. Какие MCP и tools доступны.
16. Статус Windows Control.
17. Статус Photoshop.
18. Статус DaVinci.
19. Статус Blender.
20. Статус multi-user/guest isolation.
21. Какие E2E выполнены.
22. Какие tests прошли.
23. Какие tests не прошли.
24. Какие capabilities degraded/blocked.
25. Измеренные latency/VRAM/RAM.
26. Найденные security risks.
27. Создан ли commit.
28. Commit SHA или причина отсутствия.
29. Итоговый статус: `FRONTIER_READY_LOCAL`, `FRONTIER_READY_HYBRID`, `READY_WITH_GAPS` или `BLOCKED`.
30. Одна следующая команда или одно конкретное действие пользователя.

Только если все соответствующие gates зелёные и каждое утверждение подтверждено
сохранёнными evidence, допустима следующая итоговая формулировка:

> Система реализует архитектуру того же класса, использует проверенные локальные agentic-модели, адаптивное многошаговое обдумывание, инструменты, объективную проверку и безопасный cloud fallback. Фактический разрыв с Codex измерен на собственном наборе задач и указан в отчёте.
