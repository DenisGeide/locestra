# Workflows Knowledge

- Dataset status: implemented Stage 004 operator workflows; gateway automation ещё не заявлена.

| Workflow | Status | Canonical boundary | Source |
|---|---|---|---|
| Inspect knowledge storage | implemented | `uv run python -m services.knowledge.cli status` | `services/knowledge/cli.py`, [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#cli) |
| Preview/import one source | implemented | `import --project ... --source ... --approved --dry-run`, затем тот же scoped вызов без `--dry-run` | [Archive Import Plan](../docs/ARCHIVE_IMPORT_PLAN.md#operational-checklist-для-нового-архива) |
| Preview/index repository | implemented | `index --project ... --approved --dry-run`, затем publish | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#repository-indexing) |
| Retrieve bounded evidence | implemented | `retrieve --project ... --query ... --token-budget ...` | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract) |
| Exact text fallback | implemented | `rg-search` только по свежей approved repository map | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#retrieval-contract) |
| Build local Context Envelope | implemented CLI/class boundary | `context` сохраняет goal/constraints/files/errors/verification/tool results и добавляет bounded evidence | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#context-envelope-v1) |
| Propose long-term memory | implemented two-step boundary | `propose-memory ... --confirm PROPOSE-MEMORY`, затем отдельный `services.memory.cli confirm` | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#archive-и-memory-lifecycle) |
| Delete one source | implemented preview-first boundary | `purge-source` preview, затем exact source-ID confirmation; `compact` retry при deferred physical purge | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#delete-rebuild-и-физические-ограничения) |
| Automatic gateway/Qwen context injection | planned Stage 005 consumer | Не выполняется Stage 004 автоматически. | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#назначение-и-строгие-границы) |

Каждый workflow обязан использовать explicit cwd/scope, bounded input/output и объективно проверенный result. Ошибка не должна превращаться в false success.
