# Locestra EvalKit

Locestra EvalKit makes routing behavior reproducible and reviewable. It runs the same deterministic normalization, planning, and routing code used by the gateway, then compares each decision with a versioned expected outcome.

It does not start Ollama, contact Codex, open a browser, use a GPU, or execute the selected route. A complete run is local and normally finishes in seconds.

EvalKit evaluates the policy with a deterministic assumed-available capability
set. It is therefore a routing-policy regression suite, not a live health check
and not a degraded-resource or failover benchmark.

## Quick start

From the repository root:

```powershell
uv sync --group dev
uv run python -m evals.routing --fail-under-exact 1
```

The command prints a compact summary and writes:

```text
reports/evalkit/routing_v1.json
reports/evalkit/routing_v1.md
```

Use `--no-write` when you only need the terminal result:

```powershell
uv run python -m evals.routing --no-write --fail-under-exact 1
```

No configured models or running Locestra services are required.

## Useful filters

Run only the Russian cases:

```powershell
uv run python -m evals.routing --language ru
```

Run one behavior category:

```powershell
uv run python -m evals.routing --category permission_boundary
```

Write reports to another directory:

```powershell
uv run python -m evals.routing --output-dir .artifacts/evalkit
```

To exercise project resolution against a specific safe fixture, pass an existing directory:

```powershell
uv run python -m evals.routing --project C:\path\to\empty-fixture
```

Without `--project`, EvalKit creates and removes an empty temporary directory automatically. The path is never included in reports.

## Dataset

The public corpus lives at [`evals/datasets/routing_v1.jsonl`](../evals/datasets/routing_v1.jsonl). Its 117 synthetic cases cover:

- English and Russian prompts;
- fast and strong chat;
- local repository work and cloud escalation boundaries;
- documentation, browser, and image routes;
- read-only and write intent;
- risk and blocked decision states;
- route overrides, negation, and keyword collisions.

See the [dataset card](../evals/datasets/README.md) for its schema and contribution rules.

## Metrics

- **Route accuracy** — fraction of cases assigned to the expected route.
- **Route macro-F1** — unweighted mean F1 across route labels, so small route classes remain visible.
- **Exact outcome match** — route, execution mode, risk, and decision status all match.
- **Confusion matrix** — expected route by predicted route.
- **Language/category breakdowns** — route and exact-match rates for each slice.
- **p50/p95 latency** — local normalization, planning, and routing time.

Latency is useful for regressions on the same environment. It is not a cross-machine benchmark and excludes model inference, network calls, and tool execution.

## Scope and limitations

The benchmark answers a narrow question: does the current deterministic routing policy still produce the outcomes encoded in this reviewed regression set?

It does **not** measure:

- general accuracy on unknown production prompts;
- generated-answer quality;
- RAG retrieval or grounding;
- model inference quality;
- browser, voice, image, or coding-agent execution;
- real-world safety beyond the represented policy cases.

New evaluation suites should be added only when the corresponding product capability is implemented and can be measured honestly.

## Privacy and reproducibility

The committed dataset is synthetic. Reports include case IDs and expected/actual labels, but not rendered prompts, environment variables, runtime logs, or temporary paths. The JSON report records the dataset SHA-256 and routing policy version so a result can be tied to the evaluated inputs.

CI runs the complete corpus with a 100% exact-match regression gate. This threshold applies only to the fixed corpus; it is not a claim of 100% real-world routing accuracy.
