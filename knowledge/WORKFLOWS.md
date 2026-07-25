# Workflows Knowledge

Dataset status: implemented public workflows through Stage 006. Every workflow
uses explicit scope, bounded inputs/outputs, objective evidence, and typed
failure; “ready” or HTTP 200 alone is not success.

| Workflow | Status | Canonical boundary | Source |
|---|---|---|---|
| Inspect/index/retrieve project knowledge | Implemented | Explicit registered owner/project/source through `services.knowledge.cli` | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md) |
| Build coding context | Verified Stage 005 | Coding Engine builds a bounded Context Envelope from the isolated owned worktree | [Coding Engine](../docs/CODING_ENGINE.md) |
| Execute local repository task | Verified Stage 005 | Resolve rules → isolate worktree → Qwen → verify → independent review → optional local commit | [Coding Engine](../docs/CODING_ENGINE.md) |
| Resume coding handoff | Verified Stage 005 | Revalidate source/worktree/rules/artifacts/current diff before resuming the same task | [Codex Handoff](../docs/CODEX_HANDOFF.md) |
| Review/fix with Codex | Verified only for approved public data | Strict read-only or workspace-write cloud contract; local verification/review still required | [Coding Engine](../docs/CODING_ENGINE.md) |
| Validate/list/status MCP registry | Verified Stage 006 | `uv run python -m services.mcp_hub.cli <validate|list|status>` | [MCP Hub](../docs/MCP_HUB.md) |
| Retrieve current documentation | Verified Stage 006 | Documentation Qwen receives generated Context7-only view in a fresh neutral workspace | [MCP Hub](../docs/MCP_HUB.md) |
| Run MCP live doctor | Verified Stage 006 | Bounded Context7 call, Playwright title fixture, local diagnostics, and exact cleanup | [MCP Hub](../docs/MCP_HUB.md) |
| Stop optional MCP processes | Verified Stage 006 | Hub stops only exact proven owners and reports ambiguous/unowned evidence | [MCP Hub](../docs/MCP_HUB.md) |
| Evaluate deterministic routing | Published groundwork | `uv run python -m evals.routing --fail-under-exact 1` | [Evaluation](../docs/EVALUATION.md) |

Voice long-job, image-artifact, durable Telegram/n8n delivery, controlled
improvement, and broader evaluation workflows remain future stages.
