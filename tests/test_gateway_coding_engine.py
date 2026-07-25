from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from services import common
from services.coding import CodingEngineError
from services.coding.contracts import (
    CodingMode,
    CodingTaskResultV1,
    CodingTaskStatus,
    DataClassification,
    ExecutorKind,
    ReviewVerdict,
)
from services.coding.git import CodingRepositoryError, applicable_agent_rules
from services.coding.process import ProcessPolicyError
from services.contracts import ExecutionMode
from services.gateway import app as gateway
from services.gateway.app import ChatRequest
from services.orchestration.planner import plan_request
from services.orchestration.router import assumed_capabilities, route_request


def _local_contract(project: Path, *, request_id: str = "gateway-coding-test"):
    if not (project / ".git").exists():
        project.mkdir(parents=True, exist_ok=True)
        (project / "README.md").write_text("Synthetic gateway fixture.\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Gateway Fixture",
                "GIT_AUTHOR_EMAIL": "gateway@example.invalid",
                "GIT_COMMITTER_NAME": "Gateway Fixture",
                "GIT_COMMITTER_EMAIL": "gateway@example.invalid",
            }
        )
        for command in (
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Gateway Fixture"],
            ["git", "config", "user.email", "gateway@example.invalid"],
            ["git", "add", "README.md"],
            ["git", "commit", "--quiet", "-m", "fixture baseline"],
        ):
            subprocess.run(command, cwd=project, env=environment, check=True)
    prompt = f"Project: {project}; create result.txt containing the exact word done"
    normalized = gateway.normalize_request(
        ChatRequest(messages=[{"role": "user", "content": prompt}]),
        request_id=request_id,
    )
    planning = plan_request(normalized)
    decision = route_request(
        normalized,
        planning,
        capabilities=assumed_capabilities(),
        fast_model="local-fast",
        strong_model="local-strong",
        agent_model="local-strong",
        codex_model="codex",
    )
    assert planning.plan is not None
    assert decision.route.value == "local_code"
    return prompt, planning.plan, decision


def _result(
    *,
    task_id: str,
    project: Path,
    status: CodingTaskStatus = CodingTaskStatus.COMPLETED,
    artifact_paths: list[str] | None = None,
    final_executor: ExecutorKind = ExecutorKind.LOCAL_QWEN,
    review_verdict: ReviewVerdict | None = None,
    review_findings_count: int = 0,
    modified_files: list[str] | None = None,
) -> CodingTaskResultV1:
    handoff = (
        str(project / "handoff.json")
        if status is CodingTaskStatus.HANDOFF_READY
        else None
    )
    return CodingTaskResultV1(
        task_id=task_id,
        status=status,
        summary=(
            "Synthetic Coding Engine completion."
            if status is CodingTaskStatus.COMPLETED
            else "Synthetic bounded engine result."
        ),
        source_repository=str(project),
        worktree_path=str(project / "owned-worktree"),
        branch="local-agent/task-gateway-test",
        commit_sha=None,
        attempts=2 if status is CodingTaskStatus.HANDOFF_READY else 1,
        modified_files=(
            modified_files
            if modified_files is not None
            else ["result.txt"]
            if status is CodingTaskStatus.COMPLETED
            else []
        ),
        verification_passed=(status is CodingTaskStatus.COMPLETED),
        review_verdict=(
            review_verdict
            if review_verdict is not None
            else ReviewVerdict.APPROVED
            if status is CodingTaskStatus.COMPLETED
            else None
        ),
        final_executor=final_executor,
        final_model=(
            "local-strong"
            if final_executor is ExecutorKind.LOCAL_QWEN
            else "gpt-5.6-sol"
        ),
        review_findings_count=review_findings_count,
        artifact_paths=artifact_paths or [],
        handoff_path=handoff,
    )


def _reset_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "AGENT_LOCK", asyncio.Lock())
    monkeypatch.setattr(gateway, "GPU_LOCK", asyncio.Lock())


def test_gateway_maps_plan_and_decision_to_fail_closed_coding_request(tmp_path: Path):
    prompt, plan, decision = _local_contract(tmp_path, request_id="mapping-task")

    request = gateway.build_coding_task_request(
        task_id="mapping-task",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert request.task_id == "mapping-task"
    assert request.request_id == decision.request_id
    assert request.goal == prompt
    assert request.repository_path == str(tmp_path.resolve(strict=True))
    assert request.mode is CodingMode.WRITE
    assert request.risk.value == decision.risk.value
    assert request.constraints == plan.constraints
    assert request.acceptance_criteria == plan.acceptance_criteria
    assert request.verification_plan == plan.verification_plan
    assert request.route_reasons == decision.reason_codes
    assert request.verification_commands == []
    assert request.rule_scope_paths == ["result.txt"]
    assert request.expected_diff_paths == ["result.txt"]
    assert request.forbidden_diff_paths == []
    assert request.permissions.modify_files is True
    assert request.permissions.local_commit is False
    assert request.permissions.cloud_execution is False
    assert request.permissions.data_classification is DataClassification.INTERNAL
    assert request.permissions.push is False
    assert request.permissions.deploy is False


def test_gateway_preserves_nested_project_scope_for_rules_and_write_boundary(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="nested-project-scope")
    source = tmp_path / "src"
    source.mkdir()
    (tmp_path / "AGENTS.md").write_text("# Root rules\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("# Source rules\n", encoding="utf-8")
    prompt = f"Project: {source}; create result.txt containing done"

    request = gateway.build_coding_task_request(
        task_id="nested-project-scope",
        prompt=prompt,
        project=str(source),
        decision=decision,
        plan=plan,
    )

    assert request.repository_path == str(tmp_path.resolve(strict=True))
    assert request.rule_scope_paths == ["src"]
    assert request.expected_diff_paths == ["src"]
    assert request.forbidden_diff_paths == []
    rules = applicable_agent_rules(
        tmp_path.resolve(strict=True),
        [*request.rule_scope_paths, *request.expected_diff_paths],
    )
    assert [item.relative_to(tmp_path).as_posix() for item in rules] == [
        "AGENTS.md",
        "src/AGENTS.md",
    ]


def test_gateway_read_only_keeps_rule_scope_without_writable_scope(tmp_path: Path):
    _, plan, decision = _local_contract(tmp_path, request_id="read-only-rule-scope")
    source = tmp_path / "src"
    source.mkdir()
    (source / "README.md").write_text("Nested fact.\n", encoding="utf-8")
    prompt = f"Project: {tmp_path}; read src/README.md; do not modify files"

    request = gateway.build_coding_task_request(
        task_id="read-only-rule-scope",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision.model_copy(
            update={
                "request_id": "read-only-rule-scope",
                "execution_mode": ExecutionMode.READ_ONLY,
            }
        ),
        plan=plan,
    )

    assert request.rule_scope_paths == ["src/README.md"]
    assert request.expected_diff_paths == []
    assert request.forbidden_diff_paths == []
    assert request.permissions.modify_files is False


def test_gateway_positive_write_scope_excludes_negated_and_escaping_paths(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="positive-path-scope")
    prompt = (
        f"Project: {tmp_path}; read docs/spec.md; create result.txt and tests/result_check.py "
        "from templates/input.txt; "
        "do not modify README.md; "
        "do not create ../outside.py; inspect https://example.com/index.html"
    )

    request = gateway.build_coding_task_request(
        task_id="positive-path-scope",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert request.rule_scope_paths == [
        "docs/spec.md",
        "result.txt",
        "tests/result_check.py",
        "templates/input.txt",
        "README.md",
    ]
    assert request.expected_diff_paths == ["result.txt", "tests/result_check.py"]
    assert request.forbidden_diff_paths == ["README.md"]
    assert request.permissions.local_commit is False
    assert request.permissions.cloud_execution is False


def test_gateway_russian_positive_target_does_not_promote_negated_reference(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="russian-path-scope")
    prompt = f"Проект: {tmp_path}; Создай result.txt; не изменяй README.md"

    request = gateway.build_coding_task_request(
        task_id="russian-path-scope",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert request.rule_scope_paths == ["result.txt", "README.md"]
    assert request.expected_diff_paths == ["result.txt"]
    assert request.forbidden_diff_paths == ["README.md"]


def test_gateway_negative_scope_cannot_collapse_positive_target_to_allow_all(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="negative-comma-scope")
    prompt = f"Project: {tmp_path}; Do not modify Dockerfile, create src/app.py"

    request = gateway.build_coding_task_request(
        task_id="negative-comma-scope",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert request.rule_scope_paths == ["Dockerfile", "src/app.py"]
    assert request.expected_diff_paths == ["src/app.py"]
    assert request.forbidden_diff_paths == ["Dockerfile"]


def test_gateway_conflicting_positive_and_forbidden_scope_fails_closed(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="conflicting-scope")
    prompt = f"Project: {tmp_path}; do not modify config.py; update config.py"

    with pytest.raises(CodingRepositoryError, match="declared coding path scope"):
        gateway.build_coding_task_request(
            task_id="conflicting-scope",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )


def test_gateway_declared_path_scope_overflow_fails_closed(tmp_path: Path):
    _, plan, decision = _local_contract(tmp_path, request_id="path-scope-overflow")
    targets = " ".join(f"generated/file-{index:03d}.py" for index in range(257))
    prompt = f"Project: {tmp_path}; create {targets}"

    with pytest.raises(CodingRepositoryError, match="policy limit"):
        gateway.build_coding_task_request(
            task_id="path-scope-overflow",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )


def test_gateway_scope_rejects_existing_link_that_resolves_outside_repository(
    tmp_path: Path,
):
    _, plan, decision = _local_contract(tmp_path, request_id="linked-path-scope")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=False)
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this Windows host")
    prompt = f"Project: {tmp_path}; create linked/escape.py"

    request = gateway.build_coding_task_request(
        task_id="linked-path-scope",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert request.rule_scope_paths == []
    assert request.expected_diff_paths == []
    assert request.forbidden_diff_paths == []


def test_gateway_discovers_only_tracked_conventional_write_verifiers(tmp_path: Path):
    prompt, plan, decision = _local_contract(tmp_path, request_id="verification-discovery")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_sample.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "add test"], cwd=tmp_path, check=True)

    request = gateway.build_coding_task_request(
        task_id="verification-discovery",
        prompt=prompt,
        project=str(tmp_path),
        decision=decision,
        plan=plan,
    )

    assert [item.argv for item in request.verification_commands] == [
        ["python", "-m", "unittest", "discover", "-s", "tests"]
    ]

    read_only = gateway.build_coding_task_request(
        task_id="mapping-read-only",
        prompt="Inspect README.md without changing files.",
        project=str(tmp_path),
        decision=decision.model_copy(
            update={
                "request_id": "mapping-read-only",
                "execution_mode": ExecutionMode.READ_ONLY,
            }
        ),
        plan=plan,
    )
    assert read_only.mode is CodingMode.READ_ONLY
    assert read_only.permissions.modify_files is False


class _FakeEngine:
    def __init__(self, result_factory) -> None:
        self.result_factory = result_factory
        self.calls: list[tuple[object, threading.Event]] = []

    def run(self, request, *, cancel_event: threading.Event):
        self.calls.append((request, cancel_event))
        return self.result_factory(request)


def test_execute_coding_engine_invokes_injected_engine_and_mirrors_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, plan, decision = _local_contract(tmp_path, request_id="engine-wrapper")
    artifact_paths = [str(tmp_path / f"artifact-{index}.txt") for index in range(130)]
    engine = _FakeEngine(
        lambda request: _result(
            task_id=request.task_id,
            project=tmp_path,
            artifact_paths=artifact_paths,
        )
    )
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "save_task",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    _reset_locks(monkeypatch)

    result = asyncio.run(
        gateway.execute_coding_engine(
            task_id="engine-wrapper",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
            engine=engine,  # type: ignore[arg-type]
        )
    )

    assert result.status is CodingTaskStatus.COMPLETED
    assert len(engine.calls) == 1
    mapped, cancel_event = engine.calls[0]
    assert mapped.task_id == "engine-wrapper"
    assert mapped.permissions.cloud_execution is False
    assert cancel_event.is_set() is False
    assert [entry[0][2] for entry in writes] == ["running", "complete"]
    assert writes[-1][1]["actual_executor"].value == "qwen_code"
    assert writes[-1][1]["modified_files"] == ["result.txt"]
    assert writes[-1][1]["artifact_refs"] == artifact_paths[-128:]
    assert writes[-1][1]["worktree_path"] == str(tmp_path / "owned-worktree")


def test_execute_coding_engine_journals_codex_review_findings_as_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, plan, decision = _local_contract(
        tmp_path, request_id="engine-codex-review"
    )
    engine = _FakeEngine(
        lambda request: _result(
            task_id=request.task_id,
            project=tmp_path,
            final_executor=ExecutorKind.CODEX_REVIEW,
            review_verdict=ReviewVerdict.REJECTED,
            review_findings_count=1,
            modified_files=[],
        )
    )
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "save_task",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    _reset_locks(monkeypatch)

    result = asyncio.run(
        gateway.execute_coding_engine(
            task_id="engine-codex-review",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
            engine=engine,  # type: ignore[arg-type]
        )
    )

    assert result.status is CodingTaskStatus.COMPLETED
    assert writes[-1][1]["actual_executor"].value == "codex_cli"
    assert writes[-1][1]["actual_model"] == "gpt-5.6-sol"
    assert writes[-1][1]["reason_codes"] == [
        "coding.review_findings_delivered"
    ]


def test_execute_coding_engine_closes_local_attempt_before_ready_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, plan, decision = _local_contract(tmp_path, request_id="engine-handoff")
    engine = _FakeEngine(
        lambda request: _result(
            task_id=request.task_id,
            project=tmp_path,
            status=CodingTaskStatus.HANDOFF_READY,
        )
    )
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "save_task",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    _reset_locks(monkeypatch)

    result = asyncio.run(
        gateway.execute_coding_engine(
            task_id="engine-handoff",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
            engine=engine,  # type: ignore[arg-type]
        )
    )

    assert result.status is CodingTaskStatus.HANDOFF_READY
    assert [entry[0][1:3] for entry in writes] == [
        ("local_code", "running"),
        ("local_code", "failed"),
        ("codex_bundle", "ready"),
    ]
    assert writes[-1][0][6] == {
        "bundle": result.handoff_path,
        "handoff": result.handoff_path,
    }
    assert writes[-1][1]["fallback_used"] is True
    assert writes[-1][1]["actual_executor"].value == "codex_bundle"
    assert writes[-1][1]["worktree_path"] == str(tmp_path / "owned-worktree")


def test_execute_coding_engine_closes_attempt_before_projecting_blocked_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, plan, decision = _local_contract(tmp_path, request_id="engine-blocked")
    engine = _FakeEngine(
        lambda request: _result(
            task_id=request.task_id,
            project=tmp_path,
            status=CodingTaskStatus.BLOCKED,
        )
    )
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "save_task",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    _reset_locks(monkeypatch)

    result = asyncio.run(
        gateway.execute_coding_engine(
            task_id="engine-blocked",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
            engine=engine,  # type: ignore[arg-type]
        )
    )

    assert result.status is CodingTaskStatus.BLOCKED
    assert [entry[0][2] for entry in writes] == ["running", "failed", "blocked"]
    assert writes[-1][1]["reason_codes"] == ["coding.blocked"]
    assert writes[-1][1]["worktree_path"] == str(tmp_path / "owned-worktree")


def test_production_local_code_route_uses_coding_engine_not_legacy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, _, _ = _local_contract(tmp_path, request_id="preview-only")
    engine = _FakeEngine(
        lambda request: _result(task_id=request.task_id, project=tmp_path)
    )

    async def forbidden_legacy_path(**kwargs):
        raise AssertionError("production local_code bypassed CodingEngine")

    monkeypatch.setattr(gateway, "coding_engine_factory", lambda: engine)
    monkeypatch.setattr(gateway, "execute_local_coding", forbidden_legacy_path)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    _reset_locks(monkeypatch)

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": prompt}]))
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert response.headers["X-Local-Agent-Route"] == "local_code"
    assert len(engine.calls) == 1
    mapped, _ = engine.calls[0]
    assert mapped.repository_path == str(tmp_path.resolve())
    assert mapped.permissions.cloud_execution is False
    content = body["choices"][0]["message"]["content"]
    assert "Synthetic Coding Engine completion." in content
    assert "Modified: result.txt" in content
    assert "Verification: passed" in content


def test_production_security_review_response_distinguishes_delivered_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, _, _ = _local_contract(tmp_path, request_id="preview-review-findings")

    async def fake_engine_path(**kwargs):
        return _result(
            task_id=str(kwargs["task_id"]),
            project=tmp_path,
            final_executor=ExecutorKind.CODEX_REVIEW,
            review_verdict=ReviewVerdict.REJECTED,
            review_findings_count=2,
            modified_files=[],
        )

    monkeypatch.setattr(gateway, "execute_coding_engine", fake_engine_path)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": prompt}]))
    )
    content = json.loads(response.body)["choices"][0]["message"]["content"]

    assert response.status_code == 200
    assert "Review: findings delivered (2; code-change verdict rejected)" in content
    assert "Review: approved" not in content


def test_production_handoff_maps_to_stable_codex_bundle_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, _, _ = _local_contract(tmp_path, request_id="preview-handoff")

    async def fake_engine_path(**kwargs):
        return _result(
            task_id=str(kwargs["task_id"]),
            project=tmp_path,
            status=CodingTaskStatus.HANDOFF_READY,
        )

    monkeypatch.setattr(gateway, "execute_coding_engine", fake_engine_path)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": prompt}]))
    )
    body = json.loads(response.body)

    assert response.status_code == 502
    assert response.headers["X-Local-Agent-Route"] == "codex_bundle"
    assert body["error"]["code"] == "failure.local_attempt_limit"
    assert str(tmp_path / "handoff.json") in body["error"]["message"]


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_code"),
    [
        (CodingTaskStatus.BLOCKED, 409, "coding.blocked"),
        (CodingTaskStatus.CANCELLED, 409, "coding.cancelled"),
        (CodingTaskStatus.FAILED, 502, "coding.failed"),
    ],
)
def test_production_terminal_engine_results_map_to_openai_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: CodingTaskStatus,
    expected_status: int,
    expected_code: str,
):
    prompt, _, _ = _local_contract(tmp_path, request_id=f"preview-{status.value}")

    async def fake_engine_path(**kwargs):
        return _result(
            task_id=str(kwargs["task_id"]),
            project=tmp_path,
            status=status,
        )

    monkeypatch.setattr(gateway, "execute_coding_engine", fake_engine_path)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": prompt}]))
    )
    body = json.loads(response.body)

    assert response.status_code == expected_status
    assert body["error"]["code"] == expected_code
    assert body["local_agent_route"] == "local_code"


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (
            CodingRepositoryError("explicit repository is not a Git worktree"),
            422,
            "coding.repository_invalid",
        ),
        (
            CodingEngineError(
                "Authorization: Bearer synthetic-super-secret-token-value"
            ),
            502,
            "coding.engine_failure",
        ),
    ],
)
def test_production_engine_exceptions_are_redacted_and_mapped_before_sse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_status: int,
    expected_code: str,
):
    prompt, _, _ = _local_contract(tmp_path, request_id="preview-error")

    async def fake_engine_path(**kwargs):
        raise failure

    monkeypatch.setattr(gateway, "execute_coding_engine", fake_engine_path)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)

    response = asyncio.run(
        gateway.chat(
            ChatRequest(
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
        )
    )
    body = json.loads(response.body)

    assert response.status_code == expected_status
    assert body["error"]["code"] == expected_code
    assert body["error"]["type"] == "local_agent_error"
    assert "synthetic-super-secret-token-value" not in response.body.decode("utf-8")


def test_gateway_run_process_does_not_inherit_parent_secrets_but_allows_explicit_local_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret-must-not-cross")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "parent-telegram-must-not-cross")
    monkeypatch.setenv("STAGE005_UNLISTED", "parent-unlisted-must-not-cross")
    script = (
        "import json,os; print(json.dumps({"
        "'openai':os.environ.get('OPENAI_API_KEY'),"
        "'telegram':os.environ.get('TELEGRAM_BOT_TOKEN'),"
        "'unlisted':os.environ.get('STAGE005_UNLISTED')}))"
    )

    inherited = json.loads(
        gateway.run_process(
            [sys.executable, "-c", script],
            str(tmp_path),
            timeout=10,
            prefer_stdout=True,
        )
    )
    explicit = json.loads(
        gateway.run_process(
            [sys.executable, "-c", script],
            str(tmp_path),
            timeout=10,
            prefer_stdout=True,
            env_overrides={"OPENAI_API_KEY": "ollama"},
        )
    )

    assert inherited == {"openai": None, "telegram": None, "unlisted": None}
    assert explicit == {"openai": "ollama", "telegram": None, "unlisted": None}
    with pytest.raises(ProcessPolicyError, match="secret-shaped"):
        gateway.run_process(
            [sys.executable, "-c", "print('must not start')"],
            str(tmp_path),
            env_overrides={"SERVICE_TOKEN": "forbidden-explicit-secret"},
        )


def test_async_gateway_cancellation_sets_engine_event_before_releasing_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prompt, plan, decision = _local_contract(tmp_path, request_id="gateway-cancel")
    started = threading.Event()
    observed_cancel = threading.Event()

    class WaitingEngine:
        def run(self, request, *, cancel_event: threading.Event):
            started.set()
            if cancel_event.wait(2):
                observed_cancel.set()
            return _result(
                task_id=request.task_id,
                project=tmp_path,
                status=CodingTaskStatus.CANCELLED,
            )

    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "save_task",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    _reset_locks(monkeypatch)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.execute_coding_engine(
                task_id="gateway-cancel",
                prompt=prompt,
                project=str(tmp_path),
                decision=decision,
                plan=plan,
                engine=WaitingEngine(),  # type: ignore[arg-type]
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert observed_cancel.is_set() is True
    assert [entry[0][2] for entry in writes] == ["running", "cancelled"]


def test_gateway_task_journal_keeps_source_and_owned_worktree_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = tmp_path / "journal"
    source = tmp_path / "source"
    worktree = tmp_path / "owned-worktree"
    source.mkdir()
    worktree.mkdir()
    monkeypatch.setattr(common, "DATA_DIR", data)

    common.save_task(
        "journal-worktree-task",
        "local_code",
        "running",
        "bounded task",
        str(source),
    )
    common.save_task(
        "journal-worktree-task",
        "local_code",
        "complete",
        "bounded task",
        str(source),
        "completed",
        worktree_path=str(worktree),
    )

    state = common.load_task_state("journal-worktree-task")
    assert state is not None
    assert state.project == str(source)
    assert state.worktree == str(worktree)
