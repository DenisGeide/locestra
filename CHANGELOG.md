# Changelog

This changelog describes public, evidence-backed milestones. It intentionally
omits private workstation paths, credentials, account state, local-only commit
identifiers, and internal implementation prompts.

## 2026-07-25 — Stages 005–006

### Published: hardened Coding Engine (Stage 005)

- Added versioned coding request, state, attempt, review, artifact, worktree,
  and resumable handoff contracts.
- Routed production `local_code` work through an isolated Coding Engine rather
  than a disposable legacy retry path.
- Added owned external Git worktrees, source and rule drift detection, scoped
  write boundaries, Docker-isolated Qwen execution, bounded verification,
  independent review, optional local commits, and fail-closed Codex escalation
  for explicitly approved public data.
- Added deterministic recovery, cancellation, timeout, descendant-process
  cleanup, and source-repository preservation checks.
- Acceptance evidence: 20 mandatory live Qwen/Codex/Playwright and lifecycle
  cases passed; the historical full gate reported 744 passed and 11 skipped;
  lifecycle doctor and production smoke gates were green.

### Published: managed MCP Hub (Stage 006)

- Added one canonical, project-scoped MCP registry with exact versions,
  permissions, locality, egress, lifecycle, timeout, retry, circuit, and audit
  metadata.
- Added managed Context7 `3.2.3`, Playwright MCP `0.0.78`, and a bounded local
  diagnostics integration.
- Generated consumer-specific Qwen views while keeping coding profiles MCP-free
  and leaving global Qwen/Codex configuration untouched.
- Added lazy owned process lifecycle, failure isolation, schema validation,
  payload-free auditing, cancellation, readiness, graceful stop, and orphan
  detection.
- Acceptance evidence: the exact-final full regression reported 855 passed and
  12 skipped; live Context7, Playwright title, local-diagnostics, doctor, and
  production smoke checks were green with no owned MCP processes left behind.

### Published earlier: initial Locestra EvalKit and CI

- Added a versioned 117-case English/Russian deterministic routing corpus,
  exact policy-outcome checks, metrics, filters, and reproducible local runner.
- Added cross-platform CI and a Windows reference-host workflow.
- This is early Stage 012 groundwork only. Full capability evaluation,
  resource baselines, and regression gates for Stages 007–011 remain planned.

### Planning material

- Added a sequenced Stage 021 operator prompt bundle for future Frontier Agent
  Runtime work. Its presence documents intended implementation tasks and does
  not claim that those capabilities are implemented or verified.
- This is long-horizon planning material. It must not be executed as a release
  stage until Stages 007–020 and its stated dependencies are implemented and
  audited.

### Publication checks

- Portable Coding Engine and MCP Hub gate: `216 passed, 1 skipped`.
- Public routing/gateway contracts: `95 passed, 1 skipped`.
- Deterministic routing EvalKit: `117/117` exact.
- Foundation, Python compile, critical Ruff, PowerShell parse, lock checks,
  owner-only Windows ACL regression, npm audit, secret scan, and public-data
  audit passed.

### Release hardening

- Made Codex cloud execution opt-in on a clean install. Local workflows no
  longer require Codex CLI or login; enabling the feature restores strict
  fail-closed checks.
- Added fail-closed base and alias digest checks for the strong Ollama profile.
- Protected gateway and voice `/v1/*` calls with the same generated bearer;
  heavyweight voice model loading is authenticated as well.
- Documented that gateway and voice are Docker-bridge-reachable host listeners,
  while Open WebUI and n8n stay loopback-published. Windows Firewall and router
  port forwarding remain operator-controlled boundaries.
- Added an audited MCP SDK/Hono transport compatibility gate for the temporary
  patched major-version override.
