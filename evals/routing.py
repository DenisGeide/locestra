from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.gateway.app import ChatRequest, normalize_request
from services.orchestration.config import get_routing_policy
from services.orchestration.planner import plan_request
from services.orchestration.router import assumed_capabilities, route_request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals" / "datasets" / "routing_v1.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "evalkit"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedOutcome(_StrictModel):
    route: str = Field(min_length=1, max_length=64)
    execution_mode: str = Field(min_length=1, max_length=64)
    risk: str = Field(min_length=1, max_length=64)
    decision_status: str = Field(min_length=1, max_length=64)


class RoutingCase(_StrictModel):
    schema_version: Literal["1.0"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    language: Literal["en", "ru"]
    category: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    prompt: str = Field(min_length=1, max_length=2_000)
    expected: ExpectedOutcome


class ActualOutcome(_StrictModel):
    route: str
    execution_mode: str
    risk: str
    decision_status: str


class CaseResult(_StrictModel):
    id: str
    language: str
    category: str
    expected: ExpectedOutcome
    actual: ActualOutcome
    route_match: bool
    exact_match: bool
    latency_ms: float = Field(ge=0)


def load_dataset(path: Path = DEFAULT_DATASET) -> list[RoutingCase]:
    """Load and strictly validate the versioned JSONL routing corpus."""

    cases: list[RoutingCase] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                case = RoutingCase.model_validate_json(line)
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid routing case") from exc
            if case.id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate case id {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    if not cases:
        raise ValueError(f"{path}: dataset is empty")
    return cases


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _macro_f1(expected: list[str], predicted: list[str]) -> float:
    labels = sorted(set(expected) | set(predicted))
    scores: list[float] = []
    for label in labels:
        true_positive = sum(e == label and p == label for e, p in zip(expected, predicted))
        false_positive = sum(e != label and p == label for e, p in zip(expected, predicted))
        false_negative = sum(e == label and p != label for e, p in zip(expected, predicted))
        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        precision = true_positive / precision_denominator if precision_denominator else 0.0
        recall = true_positive / recall_denominator if recall_denominator else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def _confusion_matrix(expected: list[str], predicted: list[str]) -> dict[str, object]:
    labels = sorted(set(expected) | set(predicted))
    counts = Counter(zip(expected, predicted))
    return {
        "labels": labels,
        "matrix": [[counts[(expected_label, predicted_label)] for predicted_label in labels] for expected_label in labels],
    }


def _breakdown(results: Iterable[CaseResult], key: Literal["language", "category"]) -> dict[str, object]:
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        grouped.setdefault(getattr(result, key), []).append(result)
    return {
        name: {
            "cases": len(items),
            "route_accuracy": sum(item.route_match for item in items) / len(items),
            "exact_match_rate": sum(item.exact_match for item in items) / len(items),
        }
        for name, items in sorted(grouped.items())
    }


def evaluate_cases(cases: list[RoutingCase], project: Path) -> list[CaseResult]:
    """Run the real normalizer, deterministic planner, and router for every case."""

    results: list[CaseResult] = []
    capabilities = assumed_capabilities()
    for case in cases:
        prompt = case.prompt.format(project=project.resolve())
        request = ChatRequest(messages=[{"role": "user", "content": prompt}])
        started = time.perf_counter_ns()
        normalized = normalize_request(request, request_id=f"eval-{case.id}")
        planning = plan_request(normalized)
        decision = route_request(
            normalized,
            planning,
            capabilities=capabilities,
            fast_model="local-fast",
            strong_model="local-strong",
            agent_model="local-strong",
            codex_model="codex",
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        actual = ActualOutcome(
            route=decision.route.value,
            execution_mode=decision.execution_mode.value,
            risk=decision.risk.value,
            decision_status=decision.decision_status.value,
        )
        results.append(
            CaseResult(
                id=case.id,
                language=case.language,
                category=case.category,
                expected=case.expected,
                actual=actual,
                route_match=actual.route == case.expected.route,
                exact_match=actual.model_dump() == case.expected.model_dump(),
                latency_ms=round(elapsed_ms, 6),
            )
        )
    return results


def build_report(
    cases: list[RoutingCase],
    results: list[CaseResult],
    dataset_path: Path,
) -> dict[str, object]:
    expected_routes = [result.expected.route for result in results]
    actual_routes = [result.actual.route for result in results]
    latencies = [result.latency_ms for result in results]
    exact_matches = sum(result.exact_match for result in results)
    route_matches = sum(result.route_match for result in results)
    failures = [
        {
            "id": result.id,
            "expected": result.expected.model_dump(),
            "actual": result.actual.model_dump(),
        }
        for result in results
        if not result.exact_match
    ]
    return {
        "schema_version": "1.0",
        "benchmark": "locestra-routing",
        "dataset_version": dataset_path.stem,
        "dataset_schema_version": cases[0].schema_version,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "policy_version": get_routing_policy().policy_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Deterministic normalizer, planner, and router regression corpus",
        "summary": {
            "cases": len(results),
            "route_matches": route_matches,
            "exact_matches": exact_matches,
            "route_accuracy": route_matches / len(results),
            "exact_match_rate": exact_matches / len(results),
            "route_macro_f1": _macro_f1(expected_routes, actual_routes),
            "latency_ms": {
                "p50": round(_percentile(latencies, 0.50), 6),
                "p95": round(_percentile(latencies, 0.95), 6),
                "max": round(max(latencies), 6),
            },
        },
        "by_language": _breakdown(results, "language"),
        "by_category": _breakdown(results, "category"),
        "route_confusion_matrix": _confusion_matrix(expected_routes, actual_routes),
        "failures": failures,
        "limitations": [
            "Accuracy is measured only on this fixed regression corpus.",
            "Latency is machine-dependent and excludes model or tool execution.",
            "This benchmark does not evaluate RAG, generated-answer quality, or external services.",
        ],
    }


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_markdown(report: dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    latency = summary["latency_ms"]
    assert isinstance(latency, dict)
    lines = [
        "# Locestra routing evaluation",
        "",
        f"- Dataset: `{report['dataset_version']}`",
        f"- Routing policy: `{report['policy_version']}`",
        f"- Cases: **{summary['cases']}**",
        f"- Route accuracy: **{_percent(float(summary['route_accuracy']))}**",
        f"- Exact outcome match: **{_percent(float(summary['exact_match_rate']))}**",
        f"- Route macro-F1: **{float(summary['route_macro_f1']):.4f}**",
        f"- Pipeline latency: **p50 {float(latency['p50']):.3f} ms / p95 {float(latency['p95']):.3f} ms**",
        "",
        "Exact outcome match requires route, execution mode, risk, and decision status to all match.",
        "",
        "## Language breakdown",
        "",
        "| Language | Cases | Route accuracy | Exact match |",
        "|---|---:|---:|---:|",
    ]
    by_language = report["by_language"]
    assert isinstance(by_language, dict)
    for name, raw_values in by_language.items():
        values = raw_values
        assert isinstance(values, dict)
        lines.append(
            f"| `{name}` | {values['cases']} | {_percent(float(values['route_accuracy']))} | "
            f"{_percent(float(values['exact_match_rate']))} |"
        )
    lines.extend(
        [
            "",
            "## Category breakdown",
            "",
            "| Category | Cases | Route accuracy | Exact match |",
            "|---|---:|---:|---:|",
        ]
    )
    by_category = report["by_category"]
    assert isinstance(by_category, dict)
    for name, raw_values in by_category.items():
        values = raw_values
        assert isinstance(values, dict)
        lines.append(
            f"| `{name}` | {values['cases']} | {_percent(float(values['route_accuracy']))} | "
            f"{_percent(float(values['exact_match_rate']))} |"
        )
    confusion = report["route_confusion_matrix"]
    assert isinstance(confusion, dict)
    labels = confusion["labels"]
    matrix = confusion["matrix"]
    assert isinstance(labels, list)
    assert isinstance(matrix, list)
    lines.extend(
        [
            "",
            "## Route confusion matrix",
            "",
            "Rows are expected routes; columns are predicted routes.",
            "",
            "| Expected \\ predicted | " + " | ".join(f"`{label}`" for label in labels) + " |",
            "|---|" + "|".join("---:" for _ in labels) + "|",
        ]
    )
    for expected_label, row in zip(labels, matrix):
        assert isinstance(row, list)
        lines.append(f"| `{expected_label}` | " + " | ".join(str(value) for value in row) + " |")
    failures = report["failures"]
    assert isinstance(failures, list)
    lines.extend(["", "## Failures", ""])
    if not failures:
        lines.append("No mismatches in this run.")
    else:
        lines.append("| Case | Expected | Actual |")
        lines.append("|---|---|---|")
        for failure in failures:
            assert isinstance(failure, dict)
            expected = json.dumps(failure["expected"], ensure_ascii=False, separators=(",", ":"))
            actual = json.dumps(failure["actual"], ensure_ascii=False, separators=(",", ":"))
            lines.append(f"| `{failure['id']}` | `{expected}` | `{actual}` |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This is a versioned deterministic regression benchmark, not a claim of general routing accuracy. "
            "It exercises the real Locestra normalization, planning, and routing path without starting models, "
            "calling external services, or reading private user data.",
            "",
            "Latency covers only the local deterministic pipeline and varies by machine.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_version = str(report["dataset_version"])
    json_path = output_dir / f"{dataset_version}.json"
    markdown_path = output_dir / f"{dataset_version}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _filtered_cases(
    cases: list[RoutingCase],
    *,
    language: str | None,
    category: str | None,
) -> list[RoutingCase]:
    selected = [
        case
        for case in cases
        if (language is None or case.language == language)
        and (category is None or case.category == category)
    ]
    if not selected:
        raise ValueError("no cases match the requested filters")
    return selected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Locestra's deterministic routing regression benchmark.",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--project", type=Path, help="Existing safe fixture project; a temporary one is used by default.")
    parser.add_argument("--language", choices=("en", "ru"), help="Evaluate only one language.")
    parser.add_argument("--category", help="Evaluate only one dataset category.")
    parser.add_argument(
        "--fail-under-exact",
        type=float,
        default=0.0,
        metavar="RATE",
        help="Exit non-zero when the exact outcome match rate is below RATE (0..1).",
    )
    parser.add_argument("--no-write", action="store_true", help="Print results without writing report files.")
    args = parser.parse_args(argv)
    if not 0.0 <= args.fail_under_exact <= 1.0:
        parser.error("--fail-under-exact must be between 0 and 1")
    if args.project is not None and not args.project.is_dir():
        parser.error("--project must point to an existing directory")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dataset_path = args.dataset.resolve()
    cases = _filtered_cases(
        load_dataset(dataset_path),
        language=args.language,
        category=args.category,
    )

    if args.project is not None:
        results = evaluate_cases(cases, args.project)
    else:
        with tempfile.TemporaryDirectory(prefix="locestra-eval-") as temporary_project:
            results = evaluate_cases(cases, Path(temporary_project))

    report = build_report(cases, results, dataset_path)
    summary = report["summary"]
    assert isinstance(summary, dict)
    latency = summary["latency_ms"]
    assert isinstance(latency, dict)
    print("Locestra EvalKit · deterministic routing")
    print(
        f"{summary['cases']} cases · exact {_percent(float(summary['exact_match_rate']))} · "
        f"route accuracy {_percent(float(summary['route_accuracy']))} · "
        f"macro-F1 {float(summary['route_macro_f1']):.4f}"
    )
    print(f"pipeline latency p50 {float(latency['p50']):.3f} ms · p95 {float(latency['p95']):.3f} ms")
    if not args.no_write:
        json_path, markdown_path = write_report(report, args.output_dir)
        print(f"JSON: {json_path}")
        print(f"Markdown: {markdown_path}")
    return 0 if float(summary["exact_match_rate"]) >= args.fail_under_exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
