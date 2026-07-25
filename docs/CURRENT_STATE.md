# Current State

Snapshot: 2026-07-25 public documentation release. Stages 000–006 are verified
complete and published. Stage 007 is the next planned implementation milestone.

Current tracked content and the release diff are privacy-sanitized. Private
workstation paths, credentials, account state, internal prompts, request
identifiers, and local-only commit identifiers are not acceptance evidence and
are not included. Legacy public commits retain pre-existing author/committer
email metadata; removing it requires a separate history migration.

## Verified now

| Capability | Verified state |
|---|---|
| Governance and lifecycle | Versioned rules, permissions, contracts, health, ownership, start/stop/doctor/smoke foundations |
| Routing | Deterministic Normalizer/Planner/Router and 117-case RU/EN EvalKit |
| Memory | Typed scoped records, provenance, privacy, CRUD/retention/delete, bounded retrieval |
| Knowledge | Approved source registration, repository map, FTS5/rg retrieval, freshness, provenance, Context Envelope |
| Coding | Production `local_code` uses the hardened Stage 005 Coding Engine |
| MCP | Three evaluated Stage 006 integrations run through one canonical managed registry |

## Stage 005: hardened Coding Engine

The Coding Engine owns a strict request/state contract, canonical repository
resolution, applicable rule snapshots, external linked worktrees, task
ownership, durable attempts/events, bounded artifacts, execution, verification,
independent review, optional local commit, and resumable handoff.

Qwen Code runs inside a task container with a narrow connection to the local
model service. The source checkout is not used as scratch space. Scope, rules,
Git metadata, ignored artifacts, source drift, and worktree identity are
revalidated throughout the task. Codex is explicit, public-data-only cloud
escalation; without approval the workflow remains local.

Accepted evidence:

- mandatory live matrix: 20 passed;
- historical full regression: `744 passed, 11 skipped`;
- lifecycle stop/start and `DOCTOR_OK`;
- production gateway/Open WebUI coding smoke green;
- source repository/remote preservation and owned-worktree cleanup green;
- secret scan and independent audit with no unresolved P0/P1/P2.

Read [Coding Engine](CODING_ENGINE.md) for contracts and limitations.

## Stage 006: managed MCP Hub

The Hub uses one project-scoped registry and generates disposable
consumer-specific Qwen views. It manages lazy stdio process ownership,
discovery, readiness, call deadlines, bounded idempotent retry, cancellation,
circuit/degraded state, graceful stop, orphan detection, strict schema/input
policy, and metadata-only audit.

Included:

- Context7 `3.2.3` for current public documentation;
- Playwright MCP `0.0.78` for one Hub-owned loopback title fixture;
- local diagnostics `1.0.0` for bounded registry/health metadata.

The coding profile has no MCP. Global Qwen/Codex configuration is untouched.
A failed optional MCP does not make chat or coding unready.

Accepted evidence:

- MCP-only: `97 passed`;
- expanded focused: `164 passed, 1 skipped`;
- exact-final full regression: `855 passed, 12 skipped`;
- real bounded calls through all three included integrations;
- `DOCTOR_OK`, `SMOKE_TEST_OK`, config consistency, failure isolation,
  cancellation/timeout/retry, audit redaction, and no owned orphan processes;
- dependency/secret scans and independent audit green.

Read [MCP Hub](MCP_HUB.md) for the inclusion policy and operations.

## Existing but not yet a completed later-stage capability

| Area | Honest state |
|---|---|
| Tool/Application Registry | Stage 007 planned; Stage 006 registry covers MCP only |
| Voice | Existing faster-whisper endpoint and short-audio bridge; durable long jobs, artifacts, resume, summary, and channel return are Stage 008 |
| Image | Existing ComfyUI/on-demand foundation; policy, artifacts, GPU coordination, and semantic E2E are Stage 009 |
| Interfaces | Existing gateway/Open WebUI plus n8n/Telegram foundations; durable jobs, actor auth, idempotency, and unified delivery are Stage 010 |
| Controlled improvement | Stage 011 planned |
| Evaluation | Initial deterministic routing EvalKit/CI exists; full cross-capability Stage 012 remains planned |

## Security and operational limitations

1. Locestra targets one trusted local workstation; it is not a hostile
   multi-tenant service.
2. Open WebUI and n8n are published on loopback. Gateway and voice must listen
   on host IPv4 interfaces so Docker Desktop can reach them through
   `host.docker.internal`; both `/v1/*` boundaries require the same generated
   bearer. This is not host-only binding and does not replace a host firewall
   or full actor authorization.
3. A general workspace allowlist and complete cloud data/approval ledger remain
   future work.
4. Context7 is an external, untrusted public-doc boundary. Automatic filters
   cannot prove that arbitrary prose is public.
5. Managed MCP processes are policy- and ownership-bounded, not OS-sandboxed.
6. A sudden host/Docker failure may leave stale evidence requiring doctor or
   explicit recovery.
7. Runtime databases, logs, model files, worktrees, generated settings, and
   artifacts are local ignored state and require the operator's backup and
   retention policy.

The canonical mutable facts are in [System Manifest](../SYSTEM_MANIFEST.md);
next milestones are in [Roadmap](ROADMAP.md).
