# Locestra routing evaluation

- Dataset: `routing_v1`
- Routing policy: `2026-07-14.1`
- Cases: **117**
- Route accuracy: **100.00%**
- Exact outcome match: **100.00%**
- Route macro-F1: **1.0000**
- Pipeline latency: **p50 0.333 ms / p95 0.544 ms**

Exact outcome match requires route, execution mode, risk, and decision status to all match.

## Language breakdown

| Language | Cases | Route accuracy | Exact match |
|---|---:|---:|---:|
| `en` | 98 | 100.00% | 100.00% |
| `ru` | 19 | 100.00% | 100.00% |

## Category breakdown

| Category | Cases | Route accuracy | Exact match |
|---|---:|---:|---:|
| `analysis` | 4 | 100.00% | 100.00% |
| `auxiliary` | 1 | 100.00% | 100.00% |
| `browser` | 2 | 100.00% | 100.00% |
| `collision_handling` | 8 | 100.00% | 100.00% |
| `conversation` | 23 | 100.00% | 100.00% |
| `documentation` | 2 | 100.00% | 100.00% |
| `image` | 2 | 100.00% | 100.00% |
| `permission_boundary` | 8 | 100.00% | 100.00% |
| `repository_task` | 40 | 100.00% | 100.00% |
| `review_and_escalation` | 19 | 100.00% | 100.00% |
| `route_override` | 8 | 100.00% | 100.00% |

## Route confusion matrix

Rows are expected routes; columns are predicted routes.

| Expected \ predicted | `auxiliary` | `browser` | `codex` | `docs` | `fast_chat` | `image` | `local_code` | `strong_chat` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `auxiliary` | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `browser` | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| `codex` | 0 | 0 | 24 | 0 | 0 | 0 | 0 | 0 |
| `docs` | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| `fast_chat` | 0 | 0 | 0 | 0 | 24 | 0 | 0 | 0 |
| `image` | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 |
| `local_code` | 0 | 0 | 0 | 0 | 0 | 0 | 55 | 0 |
| `strong_chat` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 |

## Failures

No mismatches in this run.

## Interpretation

This is a versioned deterministic regression benchmark, not a claim of general routing accuracy. It exercises the real Locestra normalization, planning, and routing path without starting models, calling external services, or reading private user data.

Latency covers only the local deterministic pipeline and varies by machine.
