# Coding Engine

Status: Stage 005 verified complete and published. Contract schema `1.0`;
coding policy schema `1.0`, policy `2026-07-15.4`. This page documents the
public architecture and acceptance boundary, not a promise that every language,
build system, or cloud workflow is supported.

## Purpose

The Coding Engine turns an approved repository task into a bounded, inspectable
workflow:

`resolve → inspect rules → plan → isolate → build context → execute → verify → review → optional local commit`

It strengthens the existing Qwen Code and optional Codex CLI integrations. It
does not replace the gateway, router, IDE, or model server.

## Production path

The `local_code` route constructs a strict coding request and invokes the
Coding Engine. The request records:

- the exact repository, goal, constraints, acceptance criteria, and risk;
- read-only or write mode and explicit modification/commit/cloud permissions;
- rule scopes, allowed mutation scopes, and forbidden mutation scopes;
- shell-free verifier commands, timeouts, model/executor attempts, and
  resulting artifacts.

Ordinary ingress disables cloud execution and local commits. Push and deploy
are invariantly disabled by this stage. Cloud use requires explicit approval
and public data classification; otherwise the engine keeps work local and can
produce a resumable handoff.

## Repository and worktree safety

Every coding task resolves a canonical Git repository and snapshots its HEAD,
dirty state, relevant metadata, applicable `AGENTS.md` rules, and content
fingerprints. Write and read-only agent tasks run in an owned linked worktree
outside the source checkout.

The engine verifies ownership and identity before every sensitive transition.
It rejects traversal, repository escapes, unexpected rule drift, protected Git
metadata changes, ignored task artifacts, out-of-scope changes, and ambiguous
worktree ownership. Cleanup removes only an exact, registered, clean completed
worktree through Git. Dirty or unproven paths are preserved for investigation.

The source branch, remotes, credentials, and pre-existing user changes are not
used as a task scratch space.

## Local Qwen execution

Qwen Code runs in an exact, digest-pinned container profile with:

- a read-only root filesystem and unprivileged user;
- dropped capabilities, `no-new-privileges`, and bounded process count;
- only the owned worktree mounted, read-only or writable according to task
  mode;
- an internal network and a narrow reverse proxy to the local model API;
- no host home, Docker socket, credentials, global Qwen profile, or MCP tools.

The proxy allows only the model operations needed by the agent. The executor
cannot use model-management endpoints or a generic forward proxy.

## Context, verification, and review

The engine builds a bounded Knowledge Context Envelope from the isolated
worktree. Retrieved repository content remains untrusted data and never expands
permissions.

Verifier commands are selected from explicit, supported recipes and run in
separate pinned containers. Install, publish, deploy, Git mutation, and
unqualified shell commands are rejected. A write task needs current-attempt
non-Git behavioral evidence in addition to structural checks.

Completion requires an independent approved review and no unresolved errors.
An approved local commit is optional and occurs only after verification and
review. It never implies push.

The local semantic reviewer discovers Ollama at runtime. The portable default
derives a SHA-256 identity from one stable, regular, non-reparse local
executable and revalidates that identity against the loopback listener. Hosts
that require a pre-established binary trust anchor should set both
`LOCESTRA_OLLAMA_EXECUTABLE` and
`LOCESTRA_OLLAMA_EXECUTABLE_SHA256` through the runtime environment. Those
host-specific values are never committed. Doctor applies the same automatic
or strict-pinned mode and fails closed on an identity mismatch.

Playwright UI verification uses a bounded fixture, trusted executables, exact
origin policy, and captured evidence. It is not a general web sandbox.

## Codex boundary and resumable handoff

Codex is an optional cloud executor/reviewer, not a silent fallback. It receives
only a strict task-derived contract when cloud execution is explicitly approved
for public data. Read-only review uses a read-only sandbox; writable public
fixture repair uses workspace-write with network disabled and external feature
surfaces explicitly turned off.

After bounded local failures, the engine can create versioned JSON and Markdown
handoff artifacts. Resume revalidates repository, worktree, rules, artifacts,
status, diff, and ownership. A ready handoff is not a successful task.

## Durable state and artifacts

Coding state is stored separately from the general task journal. The database
keeps compare-and-swap current state, append-only events, attempts, artifacts,
review evidence, and a durable mirror of worktree ownership. Operational owner
records remain in ignored runtime storage.

Artifacts are content-addressed and provenance-bound. Logs and API responses
are bounded and redacted; generated state, worktrees, logs, and artifacts
remain ignored by Git.

## Failure behavior

- Timeout and cancellation terminate the owned process tree.
- Verification or review failure starts only a bounded correction attempt.
- Exhausted local attempts produce a typed failure or resumable handoff.
- Source/rule/metadata drift blocks completion.
- Cleanup uncertainty preserves evidence rather than deleting by path guess.
- Crash recovery reconciles durable task and worktree records without claiming
  false success.

## Acceptance evidence

The accepted Stage 005 revision passed:

- 20 mandatory live Qwen, Codex, Playwright, and deterministic lifecycle cases;
- a historical full regression of `744 passed, 11 skipped`;
- stop/start and `DOCTOR_OK`;
- production gateway and Open WebUI coding smoke workflows;
- source HEAD/status/remote preservation and exact owned-worktree cleanup;
- secret scanning and independent review with no unresolved P0/P1/P2 findings.

These are historical acceptance results, not a guarantee of identical timings
or model quality on every host.

## Known limitations

- The platform targets one trusted local operator, not hostile multi-tenancy.
- Verifier ecosystems without an evaluated recipe fail closed.
- Docker, Git, the host filesystem, and the configured local model remain trust
  dependencies.
- Abrupt host or daemon failure may leave stale evidence that doctor/recovery
  must inspect.
- Codex data governance is intentionally narrower than a complete enterprise
  approval ledger.
- The engine coordinates coding work; system-wide GPU scheduling remains a
  later platform concern.

See [Permissions](PERMISSIONS.md), [Security Model](SECURITY_MODEL.md),
[Context Strategy](CONTEXT_STRATEGY.md), and [MCP Hub](MCP_HUB.md).
