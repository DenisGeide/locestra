# Текущее состояние платформы

- Snapshot: 2026-07-16 public release, Stages 000–004 complete; Stage 004 regression, foundation, doctor, smoke and scoped live-index gates are green.
- Владелец: владелец платформы.
- Public Git baseline is a fresh privacy-sanitized `main` root snapshot. Previous local Git metadata, machine paths, and experimental history are intentionally not included.
- Назначение: честно отделять проверенное состояние от config, планов и недоступных функций.
- Источники: read-only host/API audit, текущий repository/config и результаты stage checks.
- Изменение: после runtime/config/model/version/gate изменения с датой и evidence.

## Verified

| Факт | Evidence, 2026-07-14 |
|---|---|
| Reference host | Windows 11 with a high-memory NVIDIA GPU. Exact OS build, driver, CPU, and device inventory are intentionally omitted from the public snapshot. |
| Git baseline | Fresh public `main` root snapshot containing the verified Stage 000–004 source tree. |
| Open WebUI | `http://127.0.0.1:3737/health` → 200; API version 0.10.2; container healthy; Docker port опубликован только на `127.0.0.1`. |
| Gateway | `/health/live` live; `/health/ready` ready; `/health` legacy status `ok`, canonical v1 status `ok`, SQLite/fast/strong required и готовы; все `/v1/*` требуют runtime-generated bearer credential, `/v1/models` публикует `local-agent-auto` только после authentication. |
| Gateway semantics | Non-stream/stream, `[DONE]`, tool calls, request correlation, typed pre-/mid-stream failures и absence of false completion покрыты tests/smoke. Fresh Open WebUI fast request вернул `LOCAL_UI_OK`; read-only repository request вернул заголовок README без мутаций. |
| Planner/Router | Policy `2026-07-14.1`, pure Normalizer/Planner/Router, six leading overrides, project/permission/capability/failure/context gates и 117-case RU/EN corpus реализованы; fixed result `117/117`. Real HTTP `/v1/route`, n=200: p50 1.198 ms, p95 1.538 ms, max 1.819 ms. |
| Core contracts | `NormalizedRequestV1`, `PlanV1`, `RouteDecisionV1`, `ExecutionAttemptV1`, `ToolSpecV1`, `TaskStateV1`, `ArtifactMetadataV1` проходят positive/negative/roundtrip tests; runtime использует ingress, plan, decision, attempt и task state contracts. |
| Configuration | Platform settings: defaults → `config/platform.json` → `.env` → process env. Routing: отдельный immutable strict `config/routing.json` без env/secrets; tests подтверждают schema/version/bounds/duplicate and unknown-field rejection; LLM signal disabled. |
| Task journal migration | Schema v3 сохраняет 190 migration-boundary rows без rewrite как `legacy_payload=1`; post-smoke база содержит ещё 5 bounded/privacy-filtered rows с `legacy_payload=0`. TaskState сохраняет decision, plan, actual executor/model/profile, fallback и structured attempts. |
| Controlled Memory Engine | Typed record schema `1.0`; user/project/task scope, multi-source provenance, strict archive-reference metadata, candidate/confirmed/conflicted/stale/rejected/superseded/deleted lifecycle, CRUD/search/export/purge, TTL/commit/hash/mtime invalidation, payload-free audit и read-only bounded Planner retrieval реализованы. Реальные архивы не импортировались; `memory_records=0`. |
| Memory storage security | `data/`, live DB, WAL/SHM, backup directory, verified v0 snapshot, gateway key и test export имеют protected current-user-only DACL. Explicit `Everyone` ACE удаляется regression-тестом; ACL failure останавливает операцию. |
| Live migration | Verified v0 snapshot `memory.v0.20260714T203955304517Z.5a16aba7.bak.sqlite3` (438272 bytes, integrity ok, FK=0) создан до additive migrations 1–3. Live DB: application ID 1279347021, schema v3, integrity ok, FK=0, WAL. |
| Lifecycle ownership | Повторный реальный gate после hardening: stop освободил 8787/8788/11435/UI/n8n, не изменил PID strong Ollama 11434; orphan workers/owner files отсутствовали; restart создал по одному matching owner/listener PID. Root identity требует token/path boundary, sibling-prefix collision покрыт negative test; unowned fast listener fail-closed. |
| Open WebUI network path | Из Open WebUI container выполнен простой gateway SSE request: HTTP 200, `fast_chat`, request ID, 10 JSON events и `[DONE]`; соответствующий TaskState завершён. |
| Voice | `http://127.0.0.1:8788/health` → 200; faster-whisper `large-v3-turbo`, CPU/int8, loaded. |
| n8n | `http://127.0.0.1:5678/healthz` и readiness → 200; version 2.30.4; Docker port опубликован только на `127.0.0.1`. |
| Ollama | Обе инстанции version 0.22.1; `11434` и `11435` отвечают. |
| Fast profile | `local-fast` → `qwen3.5:4b`, 4.7B Q4_K_M, context 8192, `size_vram=0`, CPU/RAM resident на 11435. |
| Strong profile | `local-strong` → `qwen3.6:35b`, 36.0B Q4_K_M, context 32768, 23.75 GB VRAM, GPU resident на 11434. |
| Toolchain | Project Python 3.12.13; uv 0.10.11; Node 24.14.0; npm 11.9.0; Git 2.52.0; Docker 29.4.2/Desktop 4.72.0; Qwen Code 0.19.10; Codex CLI 0.144.1. |
| Codex boundary | Codex CLI was available during reference validation. Account state and credentials are not part of the public snapshot; cloud execution remains approval-gated. |
| Qwen execution profiles | Immutable `config/qwen-code` (без MCP) и `config/qwen-docs` (Context7 only) копируются в ignored writable `run/qwen-homes/{qwen-code,qwen-docs}`. Code uses `--bare`; docs uses only allowed Context7 и always neutral workspace. |
| Qwen coding E2E | Gateway направил многострочную русскую задачу Qwen Code; агент изменил disposable Git workspace, точные bytes и отсутствие commit проверены. |
| Context7 E2E | Live docs request retrieved current FastAPI `lifespan` documentation through the Context7-only Qwen profile in neutral workspace. |
| Playwright | Chromium health fixture вернул `PLAYWRIGHT_OK`; external browser E2E открыл `https://example.com` и подтвердил `Example Domain`. |
| ComfyUI installation | Portable Python, `ComfyUI/main.py` и SDXL Turbo checkpoint 6.94 GB существуют в `modules/`; doctor подтвердил пути. Порт 8388 не слушает в idle, что соответствует on-demand lifecycle. |
| Stage 001 checks | Foundation validator `FOUNDATION_OK`; `uv run pytest` → 110 passed; lifecycle restart, doctor, smoke и Open WebUI container→gateway stream были green на Stage 001 revision. |
| Stage 002 checks | Full pytest → `189 passed`; focused routing/config/eval/execution/gateway selection → `122 passed`; fixed corpus `117/117`; validator → `FOUNDATION_OK`; doctor → `DOCTOR_OK`; final smoke after runtime-home fix → `189 passed` + `SMOKE_TEST_OK`; Open WebUI fast/read-only repository, live Context7 и external browser E2E green. |
| Stage 003 checks | Остановленная система: full pytest → `292 passed`. Финальный smoke повторил `292 passed`, gateway semantic/stream/tool/route matrix, Playwright, Whisper load и gateway→Qwen edit → `SMOKE_TEST_OK`. Focused migration/task/store → `41 passed`; adversarial privacy → `75 passed`, 0 bypasses; integration boundary → `87 passed`. Validator → `FOUNDATION_OK`; doctor → `DOCTOR_OK`. |
| Stage 004 checks | Full pytest → `352 passed, 1 skipped`; focused Knowledge suite → `60 passed, 1 skipped`; validator → `FOUNDATION_OK`; doctor → `DOCTOR_OK`; final smoke повторил full suite и Knowledge lifecycle → `SMOKE_TEST_OK`. Approved live index ограничен exact project/owner: 143 tracked paths, 128 indexed и 15 policy/privacy blocked; 129 active sources/1,384 active fragments, Repository Map и retrieval подтвердили fresh provenance; facts/conflicts = 0. |

## Реализовано и проверено на Stage 004

- Отдельный `services/knowledge/` реализует schema `1.0`, checksummed SQLite schema v1, source/generation registry, FTS5 fragments, explicit facts/conflicts, Repository Map v1 и payload-free audit. Approved live index создан в ignored `data/knowledge.sqlite3` и остаётся полностью воспроизводимым derived dataset.
- `config/knowledge.json` фиксирует policy `2026-07-15.2`: manual source allowlist, Git-tracked repository scope, blocked secret/runtime/model/data paths, syntax-aware secret/reference distinction и bounded file/total/fragment/Git limits.
- Manual Markdown/TXT и explicit conversation JSON/HTML adapters имеют consent + dry-run, deterministic bounded parsing, exact provenance и secret/path checks. ChatGPT/Fantik/notes archives не предоставлены и не импортированы.
- Repository index использует bounded Git tracked inventory, sanitized Git metadata/history без patches, hash/mtime/object-based incremental reuse, rename/removal handling и atomic generation activation с mutation-epoch race protection.
- Retrieval использует FTS5, live hash/mtime/tracked/policy/parser freshness validation, exact owner/project scope, source-type filter, token budget, stale/conflict flags и local-only/untrusted provenance. `rg-search` ограничен approved paths свежей map.
- Context Envelope v1 сохраняет goal/constraints/modified files/errors/verification/fresh tool results, compact repository summary и bounded evidence. Это CLI/class boundary; gateway/Qwen automatic consumer integration ещё не выполнена.
- Knowledge fact может попасть в Controlled Memory только как scoped `candidate` после literal `PROPOSE-MEMORY`; active memory требует отдельного Memory CLI confirmation. Source reclassification/change/removal invalidates связанную memory provenance.
- Exact-source preview/apply purge координирует Memory hard purge, Knowledge derived delete, WAL checkpoint и `VACUUM`; forensic erase внешних/OS/SSD copies не обещается.
- Documentation deliverables: [Knowledge Engine](KNOWLEDGE_ENGINE.md), [Archive Import Plan](ARCHIVE_IMPORT_PLAN.md) и source-backed `knowledge/*.md` inventory. Counts и green gates выше записаны по фактическим запускам, а не выведены из наличия implementation.

`.env`, token values, cookies, keys и browser profiles не выводились и не читались audit-инструментами. Designated config loader/Telegram process используют runtime secret channel; health сообщает только `disabled/configured`, не значение. Не-secret baseline берётся из committed config/public template и наблюдаемых API.

## Завершено на Stage 002–003

- Normalizer → bounded deterministic Planner → Router с policy `2026-07-14.1`; LLM classifier отсутствует.
- `/v1/route` публикует action/complexity/risk/mode, reasons/blocking, override disposition, capability observation, permission, locks/fallback и max attempts.
- Codex без scoped approval fail-closed создаёт local bounded/redacted bundle и typed 409; cloud CLI не запускается обычным ingress.
- Две explicit Qwen failures создают ровно один idempotent handoff с goal/project/worktree/constraints/acceptance/verification/errors/commands/files/artifacts; task state хранит actual fallback.
- Browser Router и adapter блокируют local/private literal/DNS/redirect/subrequest targets.
- Chat `input_audio` bounded по routing policy, декодируется и проксируется в existing Whisper transcription API; vision остаётся unavailable.
- Scoped repository `analyze/understand/explain` с project hint и запретом изменений идёт в read-only `local_code`; общий educational prompt остаётся chat.
- `/local` не обходит high-risk write approval: route блокируется с `permission.high_risk_local_override_denied`.
- Exact executable Plan сохраняет multiline goal/constraints/acceptance/verification; budget 6000 input / 4000 reserve / 4096 model output, oversized plan fail-closed до Qwen.
- SQLite schema v3 применяет checksummed additive migrations, WAL/FK, online backup/verification и explicit restore; legacy task rows не импортируются в memory.
- Controlled memory поддерживает versioned CRUD, provenance dedup, explicit conflict resolution, retention/invalidation, scoped export/delete/purge и намеренно CLI-only management boundary; HTTP memory mutation API не опубликован.
- Planner получает максимум 6 relevant confirmed records в пределах 1500 chars/20% budget; lexical prefilter применяется до bounded recency cap, fast path не конструирует MemoryStore, а degraded memory не меняет route и не блокирует execution.
- Только local-code Qwen получает untrusted memory content с provenance; docs/Context7 memory не получает. Codex/Codex bundle сохраняют только opaque record IDs. Локальные response diagnostics объясняют selection (score/why/sources/disclosure) без повторной выдачи content; Codex handoff не получает эти metadata или value.
- Central privacy layer отклоняет secret-like durable memory, encoded/UNC/NTFS-alias sources и raw archive content; 190 исторических rows остаются явно quarantined до scoped purge.

Stage milestone status is recorded in the public [Roadmap](ROADMAP.md). Historical private commit identifiers are intentionally not part of the public snapshot.

## Discovered, но не проверено end-to-end текущим audit

- Codex имеет отдельные `exec` и read-only `review` paths и model `gpt-5.6-sol`/high, но обычный ingress теперь не запускает их без scoped approval; approval ledger ещё отсутствует.
- 190 legacy task rows всё ещё содержат pre-Stage-003 prompt/result и требуют отдельного retention decision; Open WebUI/n8n/inbox/temp histories не входят в Memory Engine.
- n8n workflow и Telegram text/voice/photo adapters присутствуют в репозитории.
- Реальная ComfyUI generation текущим stage audit не запускалась; наличие runtime/checkpoint не является semantic image E2E.
- Gateway chat→Whisper audio не прогонялся live; подтверждены standalone health/model и chat bridge tests.
- Start/stop/restart в текущей OS session проверен; recovery после перезагрузки ОС, concurrent lifecycle calls и crash mid-task не проверены.

## Unavailable или не подтверждено

| Capability | Состояние |
|---|---|
| ComfyUI/image generation | Runtime/checkpoint установлены и doctor проходит; сервис on-demand остановлен в idle. Реальная генерация в текущем stage audit не запускалась, поэтому semantic status `installed/not tested`. |
| Telegram | Canonical health сообщает `disabled` без раскрытия credential; отдельного adapter health, polling/delivery и actor allowlist нет. |
| Codex execution quality | CLI/login доступны, но cloud task execution/review не выполнялись; current ingress намеренно выдаёт bundle до появления scoped approval. |
| Automatic Knowledge context injection | Scoped live index, Repository Map v1, FTS5/rg retrieval и Context Envelope проверены. Gateway/Qwen пока не вызывают envelope автоматически; vector backend намеренно deferred до measured eval. |
| Independent reviewer/approval ledger | Нет общего runtime gate; специализированный Codex review не заменяет policy engine. |

## Известные gaps

1. Open WebUI и n8n опубликованы только на loopback, а gateway `/v1/*` защищён runtime-generated bearer credential. Однако gateway/voice processes всё ещё bind `0.0.0.0`, gateway credential общий для trusted local clients и не даёт per-actor authorization, а authorized caller при отсутствии workspace allowlist всё ещё может запустить Qwen write-mode `yolo` в произвольном existing workspace.
2. Workspace allowlist, symlink/junction escape protection и immutable platform policy для executor отсутствуют.
3. Codex cloud transfer через current ingress fail-closed, но полноценные data classification, scoped approval ledger и transfer provenance ещё не реализованы.
4. Telegram actor allowlist отсутствует. Browser external E2E и public-target/DNS/redirect/HTTP(S)-subrequest checks green, но WebSocket/service-worker coverage, общий outbound proxy/audit и полное DNS pinning/rebinding hardening отсутствуют.
5. 190 quarantined task rows, `%TEMP%\local-agent-*.txt`, `inbox/*-codex.md`, migration backup и histories других приложений могут содержать pre-filter payload. Memory CLI не является общим TTL/delete API для Open WebUI/n8n/inbox/temp.
6. Docker volumes `local_agent_open_webui_data` и `local_agent_n8n_data` persistent; общий privacy export/delete/backup contract не определён.
7. Некоторые upstream dependencies используют mutable tags `main`, `latest` или `@latest` и исполняются с правами пользователя.
8. Gateway `/v1/*` имеет bearer authentication, но voice API не имеет эквивалентной auth boundary; общие request-size/rate limits не реализованы, voice upload читается целиком.
9. In-memory locks не координируют несколько gateway processes.
10. TaskState является snapshot: append-only transitions, crash reconciliation и durable progress/cancel API отсутствуют. Subprocess timeout теперь завершает process tree, но это не заменяет crash recovery.
11. Process ownership не имеет межпроцессной блокировки одновременных start/stop; whole-platform start не транзакционный.
12. Knowledge management пока CLI-only и использует default single-user owner namespace. Gateway не выводит knowledge owner из authenticated actor и не инжектирует Context Envelope в Qwen/Codex; это обязательная boundary работы Stage 005/010.
13. Стандартные локальные linked Git worktrees поддерживаются через bounded metadata validation; custom/bare external metadata, alternates и config includes fail-closed. Map freshness использует tracked inventory/Git diff и точечное чтение changed sources; exact `rg` boundary повторно валидирует все approved inputs. Tokenizer-specific accounting/LSP/vector reranker отсутствуют.

Полная threat-модель и целевые controls: [SECURITY_MODEL.md](SECURITY_MODEL.md). План устранения: [ROADMAP.md](ROADMAP.md).

## Planned

Этапы 001–004 завершены. Следующий этап — Stage 005 hardened Coding Engine и consumer wiring Knowledge Context Envelope; этапы 006–012 остаются planned.
