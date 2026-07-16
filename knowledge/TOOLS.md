# Tools Knowledge

- Dataset status: source-backed platform/tool observations; versions отражают только дату соответствующего manifest snapshot.

| Tool/capability | Status | Fact | Source |
|---|---|---|---|
| Project Python | verified at Stage 003 snapshot | Python 3.12.13 в project environment. | [System Manifest](../SYSTEM_MANIFEST.md#toolchain-and-integrations) |
| uv | verified at Stage 003 snapshot | Version 0.10.11; canonical Python test/CLI launcher. | [System Manifest](../SYSTEM_MANIFEST.md#toolchain-and-integrations) |
| Git | verified at Stage 003 snapshot | Version 2.52.0.windows.1. | [System Manifest](../SYSTEM_MANIFEST.md#toolchain-and-integrations) |
| SQLite FTS5 | implemented for Stage 004 | Lexical bounded fragment candidate search в отдельной knowledge database. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract), `services/knowledge/migrations.py` |
| ripgrep | discovered implementation dependency | Используется как fixed-string fallback только по approved paths свежей repository map; фактическая availability проверяется runtime command. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract), `services/knowledge/repository.py` |
| Qwen Code | verified at Stage 003 snapshot | CLI 0.19.10; current local coding executor, но Stage 004 Context Envelope ещё не подключён к gateway. | [System Manifest](../SYSTEM_MANIFEST.md#toolchain-and-integrations), [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#назначение-и-строгие-границы) |
| Codex CLI | verified login/CLI at Stage 003 snapshot | CLI 0.144.1; cloud execution остаётся approval-gated. | [System Manifest](../SYSTEM_MANIFEST.md#toolchain-and-integrations) |
| Vector database/embedding service | not installed by Stage 004 | Намеренно deferred до measured retrieval eval. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract) |

Manifest и live health имеют приоритет над этой derived сводкой. Tool availability не даёт permission на вызов.
