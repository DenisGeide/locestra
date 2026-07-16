# ADR 0007: Deterministic Planner and Router

- Статус: Accepted; Stage 002 implementation and runtime gates complete.
- Дата: 2026-07-14.
- Решение: policy `2026-07-14.1`.

## Контекст

Предыдущий gateway совмещал ingress, unordered keyword classification и execution в одном module. Route было трудно объяснить, воспроизвести и проверить на collisions; unavailable capability часто возвращала assistant text с HTTP 200; Codex enable flag мог ошибочно восприниматься как cloud approval. Требовалось сохранить OpenAI-compatible `/v1`, существующие executors и local-first behavior без перехода к fashionable multi-agent framework или одному недетерминированному classifier call.

## Решение

Вводится modular orchestration pipeline:

1. `Normalizer` создаёт `NormalizedRequestV1`, извлекает только leading standalone overrides, исключает inline attachment payload и явно различает resolved/default/invalid/missing project.
2. Bounded deterministic `Planner` создаёт typed signals и `PlanV1` только для задач, которым он нужен; fast/auxiliary path не платит за model planning.
3. Pure `Router` принимает request, plan/signals, immutable routing policy, injected capability/permission snapshots и failure history; возвращает `RouteDecisionV1` с reason codes, risk, status, fallback, locks и policy version.
4. LLM control signal выключен. Любая будущая optional model signal может только дать bounded evidence и не может выбирать permissions/executor напрямую.
5. Overrides `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser` являются preference, не permission. Conflicts и critical local override блокируются.
6. Обычный ingress не имеет scoped Codex cloud approval. Codex decision создаёт redacted local bundle и typed non-success response.
7. Local coding получает не более двух explicit strategies; затем создаётся ровно один idempotent context-complete handoff.
8. `TaskStateV1` сохраняет desired decision, plan, structured attempts и actual executor/model/fallback.
9. Browser route допускает только public HTTP(S); Node adapter повторно валидирует DNS, redirects и subrequests.
10. Read-only Qwen использует `plan`, prompts передаются CLI через stdin, а unscoped docs route получает neutral workspace вместо default repository.

Подробные runtime rules находятся в [Routing](../ROUTING.md). Committed [routing.json](../../config/routing.json) имеет отдельную schema/policy version и не принимает secret/env overrides.

## Причины

- Determinism делает regression corpus и failure analysis воспроизводимыми.
- Typed diagnostics отделяют route preference от permission, availability и actual execution.
- Fast path остаётся дешёвым и не зависит от model latency/failure.
- Explicit project resolution предотвращает silent execution в default workspace.
- Bundle-first Codex boundary сохраняет цель/evidence без несанкционированной cloud передачи.
- Сохранение existing adapters и `/v1` уменьшает migration risk; Planner/Router можно развивать независимо от будущих Memory/Knowledge/Tool Registry.

## Последствия

Положительные:

- один versioned source routing thresholds/rules;
- прозрачные RU/EN reasons, risk и execution modes;
- 117-case fixed regression corpus с `117/117`;
- bounded fallback без recursion;
- route и actual executor больше не смешиваются в task state;
- unavailable/degraded work не выдаётся за успешный assistant response.

Ограничения:

- rules engine не понимает произвольную семантику и требует corpus evolution;
- Planner использует только последнюю user message и не имеет Memory/Knowledge;
- capability snapshot пока не полноценный Tool Registry;
- Codex approval ledger отсутствует, поэтому cloud executor недоступен через обычный ingress;
- locks остаются in-process;
- voice attachment bridge ограничен bounded inline audio и existing Whisper API; vision executor не реализован.

## Отклонённые альтернативы

### Один LLM classifier

Отклонён как основной control path из-за nondeterminism, latency, availability dependency и prompt-injection risk. Optional bounded signal можно добавить позднее только как non-authoritative evidence.

### Полный multi-agent rewrite

Отклонён: он увеличил бы state/concurrency/failure surface до появления Memory, Tool Registry, reviewer и durable events, не исправив permission boundary.

### Только расширить старый `classify()`

Отклонён: невозможно честно представить project resolution, override disposition, capability observation, permission status, risk, fallback и actual attempts одним route string.

### Автоматически запускать Codex при high complexity

Отклонён: complexity не является разрешением передать private workspace в cloud. До scoped approval ledger используется local handoff bundle.

## Проверка

- Contract/config/normalization/router/failure/concurrency tests.
- Fixed 117-case RU/EN corpus: `117/117` для policy `2026-07-14.1`.
- Pure warm fast-route CI gate: p95 `<25 ms`.
- Full suite: `189 passed`; focused routing/config/eval/execution/gateway selection: `122 passed`.
- Real HTTP `/v1/route` (n=200): p50 1.198 ms, p95 1.538 ms, max 1.819 ms.
- `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK`, Open WebUI fast/read-only repository E2E, live Context7 and external browser E2E verified.
