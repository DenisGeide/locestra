# Roadmap и stage gates

- Статус: последовательность развития платформы.
- Владелец: владелец платформы.
- Применение: переходы между этапами 000–012.
- Источник статуса и gates: этот документ и связанное verification evidence.
- Изменение: status меняется только по фактическому gate evidence.

Нельзя перескакивать через незавершённый foundation gate. Наличие документа или skeleton не равно завершённому этапу.

| Этап | Результат/gate | Статус на 2026-07-15 |
|---|---|---|
| 000 | Конституция, Charter, Permissions, Manifest и validator согласованы; tests зелёные | Complete: `FOUNDATION_OK`, 52 tests, doctor/smoke и independent reviews зелёные |
| 001 | Current/target architecture, contracts, health и lifecycle подтверждены | Complete: 110 tests, `FOUNDATION_OK`, hardened lifecycle restart без orphan, doctor/smoke и Open WebUI→gateway streaming зелёные |
| 002 | Deterministic planner/router, overrides, failure policy и routing eval | Complete: 117/117 corpus, 189 tests, `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK`, gateway→Qwen edit, Open WebUI fast/read-only repository, live Context7 и external browser E2E green; exact commit SHA recorded after commit |
| 003 | Typed memory с provenance, privacy, CRUD/delete и migrations | Complete: 292 tests, verified migration/backup/privacy/ACL, `FOUNDATION_OK`, `DOCTOR_OK`, `SMOKE_TEST_OK` |
| 004 | Scoped knowledge/index/import/retrieval и invalidation | Complete: separate schema/source generations, allowlisted archive adapters, incremental Repository Map v1, FTS5/rg retrieval, Context Envelope and exact-source purge; full regression/foundation/doctor/smoke and approved scoped live index are green |
| 005 | Реальный Qwen/Codex coding workflow, worktree safety и reviewer | Planned |
| 006 | Управляемый MCP Hub и failure isolation | Planned |
| 007 | Unified Tool/Application Registry и policy-gated adapters | Planned |
| 008 | Voice jobs, длинная транскрипция, artifacts и provenance | Planned |
| 009 | Vision/image workflows и GPU coordination | Planned |
| 010 | Durable interfaces, API/Telegram/n8n, idempotency и auth | Planned |
| 011 | Evidence → isolated experiment → approval → apply/rollback | Planned |
| 012 | Versioned evals, baseline, resource metrics и regression gates | Planned |

## Ближайший переход

Gates 003–004 закрыты. Текущий следующий переход — Stage 005 hardened Coding Engine: автоматическое consumer wiring Context Envelope, worktree safety, retries и independent Reviewer. Полный Tool Registry остаётся Stage 007.
