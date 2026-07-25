# Routing corpus v1

`routing_v1.jsonl` is Locestra's public deterministic routing regression corpus. It contains 117 synthetic English and Russian prompts with expected outcomes from the normalizer, planner, and policy-aware router.

The corpus contains no chat history, credentials, runtime logs, repository contents, or user-specific paths. `{project}` is a portable placeholder that EvalKit replaces with a temporary empty directory by default.

The dataset is released under [CC0 1.0](DATA_LICENSE.md), independently from
the repository's AGPL-3.0 code license, so it can be reused in other evaluation
and routing projects.

## Coverage

| Dimension | Values |
|---|---|
| Languages | English, Russian |
| Routes | fast/strong chat, local code, Codex escalation, docs, browser, image, auxiliary |
| Boundaries | read-only vs write, risk, blocked decisions, explicit overrides |
| Robustness | keyword collisions, negation, educational questions, repository intent |

Each line is one self-contained JSON object:

```json
{
  "schema_version": "1.0",
  "id": "en-read-singular",
  "language": "en",
  "category": "repository_task",
  "prompt": "Read file and list test",
  "expected": {
    "route": "local_code",
    "execution_mode": "read_only",
    "risk": "medium",
    "decision_status": "ready"
  }
}
```

## Adding a case

1. Add one unique, synthetic prompt to `routing_v1.jsonl`.
2. Use `{project}` instead of a real path when repository context is required.
3. Do not copy a private conversation, file path, credential, or log entry into the corpus.
4. Run `uv run python -m evals.routing --fail-under-exact 1`.
5. Run `uv run pytest`.

Accuracy in this corpus is a regression signal, not an estimate of performance on arbitrary real-world prompts.
