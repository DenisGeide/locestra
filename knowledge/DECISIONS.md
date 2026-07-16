# Decisions Knowledge

- Dataset status: source-backed architecture decisions; extracted knowledge candidates не являются подтверждёнными decisions сами по себе.

| Status | Decision | Rationale/source |
|---|---|---|
| verified | Memory Engine и Knowledge Engine имеют отдельные contracts/storage/lifecycles. | [Memory Strategy](../docs/MEMORY_STRATEGY.md#разделение-слоёв), [Target Architecture](../docs/TARGET_ARCHITECTURE.md#storage-separation) |
| verified | Repository/archive content не становится active long-term memory автоматически. | [Memory Constitution](../constitution/MEMORY.md), [Archive Import Plan](../docs/ARCHIVE_IMPORT_PLAN.md#facts-decisions-и-memory-boundary) |
| implemented | Stage 004 использует отдельную SQLite database с FTS5 и replaceable retrieval contract. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#назначение-и-строгие-границы) |
| implemented | Vector index deferred до measured quality/latency need; MVP использует Git metadata, repository map, FTS5 и scoped rg. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract) |
| implemented | Repository indexing читает только Git-tracked inventory, а manual import — только explicit allowlist внутри canonical project. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#scope-и-identity), `config/knowledge.json` |
| implemented | All retrieved repository/archive text, map и rg results маркируются untrusted/local-only. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#security-и-privacy-controls) |
| implemented | Generation activation atomic и защищена base generation/mutation epoch; purge не может быть отменён конкурентным stale build. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#source-и-generation-model) |
| implemented | Memory promotion требует exact fact, literal `PROPOSE-MEMORY` и отдельного Memory confirmation. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#archive-и-memory-lifecycle) |
| implemented limitation | Только стандартный local linked-worktree layout разрешён; custom/bare external metadata, includes и alternates fail-closed. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#scope-и-identity) |

Новая строка добавляется только вместе с stable source, status и, если решение runtime-sensitive, verification evidence.
