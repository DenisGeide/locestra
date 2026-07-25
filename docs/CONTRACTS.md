# Versioned contracts v1

- Статус: core v1 boundary remains compatible; Stages 005–006 add separate
  versioned Coding Engine and MCP registry/tool contracts rather than silently
  changing core route semantics.
- Machine source: [services/contracts/v1.py](../services/contracts/v1.py); Pydantic JSON Schema генерируется из моделей и не дублируется вручную.
- Версия: `1.0`; все internal models запрещают неизвестные поля.
- Совместимость: внешний OpenAI [ChatRequest](../services/gateway/app.py) сохраняет `extra=allow`, затем преобразуется в строгий internal contract.

## Общие правила

- Все timestamps timezone-aware и нормализуются в UTC.
- Строки, списки, schemas, paths и counts имеют верхние границы.
- Inline `data:` URL, bytes и binary content запрещены; используется reference + metadata.
- `schema_version` сохраняется при persistence/exchange.
- Backward-compatible addition остаётся в v1; изменение смысла/required field требует нового major contract и adapter.
- Schema validation не заменяет workspace, permission, existence, symlink или data-classification policy.

## NormalizedRequestV1

| Поле | Тип/правило |
|---|---|
| `request_id` | bounded stable identifier |
| `user_message` | non-empty, максимум 262 144 символа |
| `attachments` | до 32 `AttachmentRefV1`, только metadata/reference |
| `source` | `open_webui`, `api`, `telegram`, `n8n`, `internal` |
| `project_hint` | optional reference; существование проверяет boundary layer |
| `explicit_route` | optional compatibility projection применимого override |
| `created_at` | timezone-aware UTC |
| `correlation_id` | stable identifier для retries/adapters/response |
| `routing_override` | optional `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser` preference |
| `override_conflict` | разные ведущие controls обнаружены; downstream policy блокирует |
| `project_resolution` | source `explicit/default/none` и status `resolved/invalid/missing` |

Gateway создаёт этот contract после `/v1` ingress. Он намеренно не угадывает Open WebUI/Telegram source без authenticated metadata: текущий `/v1` request записывается как `api`. Inline image/audio получают positional references, но base64 не копируется в contract. Explicit invalid project не получает default; leading override удаляется из `user_message` до execution.

## PlanV1

| Поле | Правило |
|---|---|
| `goal` | non-empty bounded goal |
| `subtasks` | 1–128 bounded шагов |
| `tools` | до 64 stable capability names |
| `acceptance_criteria` | минимум один objective criterion |
| `risk` | `low/medium/high/critical` |
| `approvals` | scoped requirements; пустой список допустим |
| `verification_plan` | минимум одна проверка |
| `context_budget` | input/output/attachment/tool-output limits и compression policy |
| `request_id` | optional связь с normalized request |
| `action`, `complexity` | optional typed deterministic planning result |
| `constraints` | bounded unique explicit constraints |

Stage 002 deterministic Planner создаёт этот contract для repository, analysis, docs, browser и media actions; fast chat/auxiliary используют `plan=null`. Plan не является permission grant и не запускает tools. Repository/docs execution воспроизводит `goal`, `constraints`, `acceptance_criteria` и `verification_plan` точно, включая multiline; current agent budget — input 6000, reserved output 4000, а Qwen profile ограничивает generation 4096. Oversized executable Plan блокируется до executor.

## RouteDecisionV1

| Поле | Правило |
|---|---|
| `request_id` | связь с normalized request |
| `route` | auxiliary/fast/strong/local code/Codex/bundle/docs/browser/image/voice/vision |
| `executor` | enum реального adapter; валидируется совместимость с route |
| `model`, `profile` | optional logical model/profile identifiers |
| `reason_codes` | непустые machine-readable codes |
| `risk` | effective risk label |
| `fallback` | optional typed route/executor/reasons |
| `project` | optional resolved reference |
| `required_locks` | bounded logical resource keys без raw path |
| `policy_version` | routing semantics version |
| `action`, `complexity`, `execution_mode` | typed intent и none/read-only/write mode |
| `requested_route`, `override_disposition` | requested preference и none/applied/rejected result |
| `decision_status` | ready/degraded/blocked |
| `permission_disposition` | allowed/approval-required/denied |
| `capability`, `capability_status`, `capability_checked_at` | injected availability observation |
| `blocking_reason_codes`, `max_attempts` | explicit gates и bounded execution ceiling |

Gateway строит decision через pure Normalizer → Planner → Router и возвращает его через `/v1/route`, сохраняя legacy-compatible `project=""` при отсутствии project. Policy/override/project/network/capability/approval diagnostics versioned. `route` описывает intent, а `executor` — выбранное выполнение: например Codex без scoped approval использует `codex_bundle`, не cloud CLI.

## ToolSpecV1

Содержит `name/version`, bounded `input_schema/output_schema`, typed health probe, permissions, risk, timeout, retry, availability и locality. JSON Schema payload проверяется на serializability/size и inline binary. Реальный общий Tool Registry появится на этапах 006–007; сейчас contract предотвращает несовместимые adapters.

## TaskStateV1

| Поле | Правило |
|---|---|
| `task_id`, `request_id` | immutable identifiers |
| `status` | pending/ready/running/blocked/complete/failed/cancelled |
| `attempts` | 0–100; terminal execution требует попытку |
| `executor` | optional typed executor |
| `project`, `worktree` | scoped references, не permission grant |
| `artifacts` | до 128 `ArtifactMetadataV1` |
| `artifact_refs` | bounded references на prepared/external artifacts, включая Codex handoff bundle |
| `modified_files` | bounded unique references |
| `unresolved_errors` | bounded summaries; failed требует минимум одну |
| `next_action` | concrete bounded action или null |
| `created_at`, `updated_at` | UTC; updated не раньше created |
| `route`, `route_decision`, `plan` | desired route, полный decision и optional bounded plan |
| `model`, `profile`, `fallback_used` | фактические execution facts |
| `attempt_history` | до 100 structured `ExecutionAttemptV1` records |

`ExecutionAttemptV1` содержит index, actual executor/model, running/complete/failed/cancelled outcome, bounded reason/command/error evidence, modified files, artifact refs и UTC timestamps. Terminal attempt требует `finished_at`; failed требует error summary.

SQLite сохраняет `state_json` v1 и legacy projections. Raw prompt/result не копируются в state JSON. Legacy rows остаются с null state и читаются как legacy, без фиктивного backfill. `INSERT ... ON CONFLICT DO UPDATE` сохраняет первоначальный `created_at`. После Qwen fallback desired decision остаётся доступен, но actual state executor становится `codex_bundle` и `fallback_used=true`.

## ArtifactMetadataV1

Содержит `artifact_id`, type, store/reference path, SHA-256, producer, source request, created time, непустую provenance chain и retention policy. `TTL` требует expiry; другие policy запрещают лишний expiry. Content находится вне request/task JSON.

## Health contracts

[services/health.py](../services/health.py) определяет `CapabilityHealthV1` и `HealthReportV1`:

- `live` относится к процессу;
- `ready` зависит только от required capabilities;
- canonical `status` равен `ok`, `degraded` или `unavailable`;
- required `unavailable/disabled/on_demand` блокирует readiness;
- optional `degraded/unavailable` не блокирует readiness, но делает report degraded;
- optional `disabled/on_demand` является нейтральным ожидаемым состоянием.

Gateway required capabilities: task SQLite, fast model и strong model. Voice/Qwen/Codex/browser/ComfyUI/Telegram — отдельные optional observations. ComfyUI получает neutral `on_demand` только при наличии portable runtime, main module и required SDXL Turbo checkpoint. Legacy `/health.status` продолжает отражать core readiness для start/doctor; canonical report находится в `health`.

## Contract verification

Tests проверяют valid/invalid/extra fields, timezone, route-executor compatibility, override/project semantics, bounded public errors, exact Plan preservation/budget failure, state/attempt invariants, inline payload exclusion, JSON roundtrip/schema const, capability degradation, ComfyUI readiness, SQLite additive migration и сохранение `created_at`. Fixed Stage 002 corpus содержит 117 cases и проходит `117/117`; focused routing/config/eval/execution/gateway selection — `122 passed`, full suite — `189 passed`. Отдельные tests покрывают two failures → one handoff и actual fallback persistence. Raw SSE получает `complete` только при terminal `[DONE]`; pre-stream failure возвращает typed HTTP error, а mid-stream exception/clean truncated EOF/cancellation не становятся false complete. `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK`, Open WebUI fast `LOCAL_UI_OK` и read-only README-heading E2E закрывают Stage 002 runtime gate.

## Stage 005 coding contracts

`services/coding/` owns a separate strict request/state/result family. It binds
the exact repository, goal, constraints, acceptance, verifier argv, risk and
data classification, rule scopes, allowed/forbidden mutation scopes, explicit
permissions, attempts, worktree identity, artifacts, independent review, and
optional local commit. Push and deploy are always false at this boundary.

The coding state machine and append-only events do not overwrite core
`TaskStateV1`. Gateway adapters project the final coding result back into the
OpenAI-compatible response and core journal. A ready handoff, failed review,
timeout, cancellation, or incomplete evidence cannot become `completed`.

See [Coding Engine](CODING_ENGINE.md).

## Stage 006 MCP contracts

`config/mcp-registry.json` is the canonical schema/policy source for server
identity, version, transport, consumers, minimal tool schemas, permissions,
locality/egress, timeouts/retry, locks, lifecycle, audit, and enabled/degraded
state. Generated consumer settings are derived views.

The launcher accepts only canonical bounded JSON-RPC envelopes, filters
discovery to the evaluated allowlist, verifies upstream schema hashes, rejects
extra/secret-shaped input, and logs metadata rather than payloads. Optional MCP
health is capability-specific and cannot change core readiness.

See [MCP Hub](MCP_HUB.md).
