# Tools Knowledge

Dataset status: source-backed capability inventory. Availability does not grant
permission. Exact installed versions can differ by checkout/reference host;
locked project sources and current doctor output take precedence.

| Tool/capability | Public status | Boundary | Source |
|---|---|---|---|
| Qwen Code | Verified Stage 005 executor | Primary local coding agent inside an owned task container; coding profile has no MCP | [Coding Engine](../docs/CODING_ENGINE.md) |
| Codex CLI | Verified Stage 005 optional executor/reviewer | Explicit approval plus public classification; otherwise local handoff | [Codex Handoff](../docs/CODEX_HANDOFF.md) |
| Git linked worktrees | Verified Stage 005 isolation | Exact owned task worktree outside source checkout; no path-guess cleanup | [Coding Engine](../docs/CODING_ENGINE.md) |
| Docker verifier recipes | Verified Stage 005 | Pinned, bounded supported test/build recipes; no host fallback | [Coding Engine](../docs/CODING_ENGINE.md) |
| Playwright UI verifier | Verified Stage 005 | Exact-origin UI evidence inside coding workflow | [Coding Engine](../docs/CODING_ENGINE.md) |
| Context7 MCP `3.2.3` | Verified Stage 006 | Public documentation egress only | [MCP Hub](../docs/MCP_HUB.md) |
| Playwright MCP `0.0.78` | Verified Stage 006 | Hub-owned loopback fixture title only | [MCP Hub](../docs/MCP_HUB.md) |
| Local diagnostics MCP `1.0.0` | Verified Stage 006 | Bounded registry/health metadata; no network/path/command input | [MCP Hub](../docs/MCP_HUB.md) |
| Direct browser adapter | Verified existing route | Separate public-target/DNS/redirect/subrequest policy; not MCP fallback | [Routing](../docs/ROUTING.md) |
| SQLite FTS5/ripgrep | Verified Knowledge implementation | Bounded retrieval over approved fresh repository-map paths | [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md) |
| faster-whisper | Existing foundation | Local transcription compatibility; durable Stage 008 jobs remain planned | [Current State](../docs/CURRENT_STATE.md) |
| ComfyUI | Existing foundation | Local on-demand images; hardened Stage 009 workflow remains planned | [Current State](../docs/CURRENT_STATE.md) |

Filesystem, shell, Git, and generic memory MCP servers are intentionally absent:
they would duplicate narrower native contracts. GitHub, image, document, and
messaging MCPs remain deferred until a consumer and bounded end-to-end workflow
justify them.
