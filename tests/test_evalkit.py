from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.routing import DEFAULT_DATASET, evaluate_cases, load_dataset, main
from services.orchestration.config import get_routing_policy


def test_public_routing_dataset_is_versioned_unique_and_private_data_free() -> None:
    cases = load_dataset()

    assert len(cases) == 117
    assert len({case.id for case in cases}) == len(cases)
    assert {case.language for case in cases} == {"en", "ru"}
    assert sum(case.language == "ru" for case in cases) == 19
    assert {"permission_boundary", "route_override", "collision_handling"} <= {
        case.category for case in cases
    }

    serialized = DEFAULT_DATASET.read_text(encoding="utf-8").casefold()
    assert "\\users\\" not in serialized
    assert "bearer " not in serialized
    assert "api_key" not in serialized


def test_dataset_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    duplicate = (
        '{"schema_version":"1.0","id":"same-id","language":"en","category":"conversation",'
        '"prompt":"Hello","expected":{"route":"fast_chat","execution_mode":"none",'
        '"risk":"low","decision_status":"ready"}}\n'
    )
    dataset = tmp_path / "duplicate.jsonl"
    dataset.write_text(duplicate + duplicate, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate case id"):
        load_dataset(dataset)


def test_committed_baseline_matches_dataset_and_policy() -> None:
    baseline_path = DEFAULT_DATASET.parents[2] / "reports" / "evalkit" / "routing_v1.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["dataset_sha256"] == hashlib.sha256(DEFAULT_DATASET.read_bytes()).hexdigest()
    assert baseline["policy_version"] == get_routing_policy().policy_version
    assert baseline["summary"]["cases"] == 117
    assert baseline["summary"]["exact_match_rate"] == 1.0


def test_evalkit_runs_the_real_pipeline_and_writes_safe_reports(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"
    exit_code = main(
        [
            "--output-dir",
            str(output_dir),
            "--fail-under-exact",
            "1",
        ]
    )

    assert exit_code == 0
    payload = json.loads((output_dir / "routing_v1.json").read_text(encoding="utf-8"))
    assert payload["summary"]["cases"] == 117
    assert payload["summary"]["route_accuracy"] == 1.0
    assert payload["summary"]["exact_match_rate"] == 1.0
    assert payload["summary"]["route_macro_f1"] == 1.0
    assert payload["failures"] == []
    assert "prompt" not in json.dumps(payload).casefold()
    assert "\\users\\" not in json.dumps(payload).casefold()

    markdown = (output_dir / "routing_v1.md").read_text(encoding="utf-8")
    assert "Route confusion matrix" in markdown
    assert "not a claim of general routing accuracy" in markdown


def test_language_filter_uses_only_requested_cases(tmp_path: Path) -> None:
    cases = [case for case in load_dataset() if case.language == "ru"]
    results = evaluate_cases(cases, tmp_path)

    assert len(results) == 19
    assert all(result.language == "ru" for result in results)
    assert all(result.exact_match for result in results)


def test_custom_dataset_report_uses_its_filename_as_version(tmp_path: Path) -> None:
    dataset = tmp_path / "custom_suite.jsonl"
    dataset.write_text(
        DEFAULT_DATASET.read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    assert main(["--dataset", str(dataset), "--output-dir", str(output_dir)]) == 0

    report = json.loads((output_dir / "custom_suite.json").read_text(encoding="utf-8"))
    assert report["dataset_version"] == "custom_suite"
