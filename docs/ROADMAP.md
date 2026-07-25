# Roadmap and Stage Gates

Status: Stages 000–006 are verified complete and published. Stage 007 is next.
The presence of a file, endpoint, package, or prototype never closes a gate
without objective tests and end-to-end evidence.

| Stage | Outcome/gate | Public status |
|---|---|---|
| 000 | Constitution, charter, permissions, manifest, validator | Complete |
| 001 | Current/target architecture, contracts, health, lifecycle | Complete |
| 002 | Deterministic planner/router, overrides, failure policy, routing eval | Complete |
| 003 | Typed Memory Engine with provenance, privacy, retention, and migrations | Complete |
| 004 | Scoped Knowledge/Archive Engine, repository map, retrieval, invalidation | Complete |
| 005 | Real Qwen/Codex coding workflow, worktree safety, verification, reviewer, handoff | Complete: 20 mandatory live cases; historical full suite `744 passed, 11 skipped`; doctor and smoke green |
| 006 | Managed MCP registry, lifecycle, policy, failure isolation, real consumers | Complete: Context7, loopback Playwright fixture, and local diagnostics; exact-final full suite `855 passed, 12 skipped`; doctor and smoke green |
| 007 | Unified Tool/Application Registry and policy-gated adapters | Planned next |
| 008 | Durable voice jobs, long transcription, artifacts, summary/provenance | Planned |
| 009 | Vision/image workflows, artifacts, privacy, GPU coordination | Planned |
| 010 | Durable interfaces, API/Telegram/n8n, idempotency, actor/auth boundaries | Planned |
| 011 | Evidence → isolated experiment → approval → apply/rollback | Planned |
| 012 | Versioned cross-capability evals, baselines, resource metrics, regression gates | Partially seeded: routing EvalKit/CI exists; broader stage planned |

## Next transition: Stage 007

The next gate should unify native tools, MCP capabilities, applications, and
resource/policy metadata without weakening the Stage 005 Coding Engine or
turning Stage 006 into a catch-all MCP proxy.

Completion requires:

- one canonical capability/application registry;
- executor-specific views generated and consistency-tested;
- explicit permissions, locality, egress, resource class, and health;
- routing that respects disabled/degraded states;
- real consumers and end-to-end workflows;
- no duplicate filesystem/shell/Git authority;
- lifecycle, doctor, smoke, security, and regression evidence.

## Later-stage honesty

Existing faster-whisper, ComfyUI, Telegram, and n8n code is useful foundation,
but it does not complete Stages 008–010. Likewise, the public routing EvalKit is
valuable Stage 012 groundwork, not proof that all capability evaluation and
resource baselines exist.

See [Current State](CURRENT_STATE.md), [Coding Engine](CODING_ENGINE.md), and
[MCP Hub](MCP_HUB.md).
