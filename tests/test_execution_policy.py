import asyncio
import hashlib
import threading

import pytest

from services import common
from services.contracts import MemoryContextItemV1
from services.gateway import app as gateway
from services.gateway.app import ChatRequest
from services.orchestration.handoff import ensure_codex_handoff
from services.orchestration.planner import plan_request
from services.orchestration.router import assumed_capabilities, route_request


def local_execution_contract(tmp_path):
    prompt = f"Project: {tmp_path}; create result.txt; do not modify README.md"
    normalized = gateway.normalize_request(
        ChatRequest(messages=[{"role": "user", "content": prompt}]),
        request_id="execution-policy",
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
    return prompt, planning.plan, decision


def reset_runtime_locks(monkeypatch):
    monkeypatch.setattr(gateway, "AGENT_LOCK", asyncio.Lock())
    monkeypatch.setattr(gateway, "CODEX_LOCK", asyncio.Lock())
    monkeypatch.setattr(gateway, "GPU_LOCK", asyncio.Lock())
    monkeypatch.setattr(gateway, "WORKTREE_LOCKS", {})


def test_first_local_success_has_one_visible_attempt_and_no_handoff(tmp_path, monkeypatch):
    prompt, plan, decision = local_execution_contract(tmp_path)
    reset_runtime_locks(monkeypatch)
    calls = []
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(gateway, "collect_modified_files", lambda project: ["result.txt"])
    monkeypatch.setattr(
        gateway,
        "run_qwen_agent",
        lambda *args: calls.append(args) or "The requested repository task completed with verified concrete evidence.",
    )

    result, bundle, failures = asyncio.run(
        gateway.execute_local_coding(
            task_id="one-success",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )
    )

    assert result is not None
    assert bundle is None
    assert failures == []
    assert len(calls) == 1
    assert [entry[0][2] for entry in writes] == ["running", "complete"]


def test_first_failure_changes_strategy_and_second_attempt_can_succeed(tmp_path, monkeypatch):
    prompt, plan, decision = local_execution_contract(tmp_path)
    reset_runtime_locks(monkeypatch)
    calls = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "collect_modified_files", lambda project: [])

    def worker(*args):
        calls.append(args[0])
        if len(calls) == 1:
            raise RuntimeError("test command returned exit code 1")
        return "Second strategy inspected the failure and completed the verified repository task."

    monkeypatch.setattr(gateway, "run_qwen_agent", worker)
    result, bundle, failures = asyncio.run(
        gateway.execute_local_coding(
            task_id="retry-success",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )
    )

    assert result is not None
    assert bundle is None
    assert len(calls) == 2
    assert "Previous bounded attempt failed" in calls[1]
    assert len(failures) == 1


def test_two_local_failures_create_exactly_one_redacted_context_complete_handoff(tmp_path, monkeypatch):
    prompt, plan, decision = local_execution_contract(tmp_path)
    inbox = tmp_path / "inbox"
    reset_runtime_locks(monkeypatch)
    calls = []
    bundle_calls = []
    real_create = gateway.create_codex_bundle
    monkeypatch.setattr(gateway, "INBOX_DIR", inbox)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "collect_modified_files", lambda project: ["result.txt"])

    def worker(*args):
        calls.append(args)
        raise RuntimeError("Authorization: Bearer super-secret-token-value; tests failed")

    def create_once(*args, **kwargs):
        bundle_calls.append((args, kwargs))
        return real_create(*args, **kwargs)

    monkeypatch.setattr(gateway, "run_qwen_agent", worker)
    monkeypatch.setattr(gateway, "create_codex_bundle", create_once)
    result, bundle, failures = asyncio.run(
        gateway.execute_local_coding(
            task_id="two-failures",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )
    )

    assert result is None
    assert bundle is not None and bundle.exists()
    assert len(calls) == 2
    assert len(bundle_calls) == 1
    content = bundle.read_text(encoding="utf-8")
    assert plan.goal in content
    assert str(tmp_path) in content
    assert "do not modify README.md" in content
    assert plan.acceptance_criteria[0] in content
    assert "result.txt" in content
    assert "qwen_code attempt 1 of 2" in content
    assert "tests failed" in content
    assert "super-secret-token-value" not in content
    assert "[REDACTED]" in content
    assert len(failures) == 2


def test_memory_assisted_local_failure_never_copies_executor_output_to_codex(
    tmp_path, monkeypatch
):
    prompt, plan, decision = local_execution_contract(tmp_path)
    marker = "MEMORY_CONTENT_MUST_NOT_CROSS_BOUNDARY"
    plan = plan.model_copy(
        update={
            "memory_record_refs": ["memory-local-1"],
            "memory_context": [
                MemoryContextItemV1(
                    record_id="memory-local-1",
                    record_type="project_knowledge",
                    subject="project.private_hint",
                    content=marker,
                    source_refs=["project://README.md"],
                    score=0.9,
                    why="bounded fixture match",
                )
            ],
        }
    )
    reset_runtime_locks(monkeypatch)
    monkeypatch.setattr(gateway, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "collect_modified_files", lambda project: [])
    monkeypatch.setattr(
        gateway,
        "run_qwen_agent",
        lambda *args: (_ for _ in ()).throw(RuntimeError(marker)),
    )

    result, bundle, failures = asyncio.run(
        gateway.execute_local_coding(
            task_id="memory-assisted-failure",
            prompt=prompt,
            project=str(tmp_path),
            decision=decision,
            plan=plan,
        )
    )

    assert result is None
    assert bundle is not None
    serialized = bundle.read_text(encoding="utf-8")
    assert marker not in serialized
    assert marker not in "\n".join(failures)
    assert "raw executor output withheld" in serialized


def test_handoff_create_is_idempotent_and_preserves_artifact_refs(tmp_path):
    _, plan, decision = local_execution_contract(tmp_path)
    kwargs = {
        "inbox_dir": tmp_path / "inbox",
        "task_id": "idempotent",
        "plan": plan,
        "decision": decision,
        "project": str(tmp_path),
        "worktree": str(tmp_path),
        "errors": ["pytest failed"],
        "modified_files": ["services/app.py"],
        "command_summaries": ["pytest -q"],
        "artifact_refs": ["outputs/pytest.log"],
    }
    first = ensure_codex_handoff(**kwargs)
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    modified = first.stat().st_mtime_ns
    second = ensure_codex_handoff(**kwargs)

    assert first == second
    assert hashlib.sha256(second.read_bytes()).hexdigest() == digest
    assert second.stat().st_mtime_ns == modified
    assert "outputs/pytest.log" in second.read_text(encoding="utf-8")


def test_task_state_persists_two_attempts_route_decision_and_actual_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path / "data")
    common.DATA_DIR.mkdir()
    prompt, plan, decision = local_execution_contract(tmp_path)
    task_id = "state-two-attempts"
    for index in (1, 2):
        common.save_task(
            task_id,
            "local_code",
            "running",
            prompt,
            str(tmp_path),
            route_decision=decision,
            plan=plan,
            actual_executor="qwen_code",
            actual_model="local-strong",
            command_summaries=[f"attempt {index}"],
        )
        common.save_task(
            task_id,
            "local_code",
            "failed",
            prompt,
            str(tmp_path),
            "failed",
            route_decision=decision,
            plan=plan,
            actual_executor="qwen_code",
            actual_model="local-strong",
            error_summary=f"failure {index}",
        )
    common.save_task(
        task_id,
        "codex_bundle",
        "ready",
        prompt,
        str(tmp_path),
        route_decision=decision,
        plan=plan,
        actual_executor="codex_bundle",
        fallback_used=True,
        artifact_refs=["inbox/state-two-attempts-codex.md"],
    )

    state = common.load_task_state(task_id)
    assert state is not None
    assert state.attempts == 2
    assert [attempt.outcome.value for attempt in state.attempt_history] == ["failed", "failed"]
    assert state.route_decision == decision
    assert state.plan == plan
    assert state.executor == "codex_bundle"
    assert state.artifact_refs == ["inbox/state-two-attempts-codex.md"]
    assert state.model == "local-strong"
    assert state.fallback_used is True
    assert state.status == "ready"


def test_same_worktree_serializes_qwen_and_codex(tmp_path, monkeypatch):
    reset_runtime_locks(monkeypatch)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    qwen_entered = threading.Event()
    codex_entered = threading.Event()
    release_qwen = threading.Event()

    def qwen(*args):
        qwen_entered.set()
        assert release_qwen.wait(2)
        return "Qwen completed a concrete bounded repository operation successfully."

    def codex(*args):
        codex_entered.set()
        return "Codex completed a concrete bounded repository operation successfully."

    monkeypatch.setattr(gateway, "run_qwen_agent", qwen)
    monkeypatch.setattr(gateway, "run_codex_agent", codex)

    async def scenario():
        first = asyncio.create_task(
            gateway.execute_agent("q", "local_code", "fix tests", str(tmp_path), False)
        )
        while not qwen_entered.is_set():
            await asyncio.sleep(0.005)
        second = asyncio.create_task(
            gateway.execute_agent("c", "codex", "review code", str(tmp_path), True)
        )
        await asyncio.sleep(0.05)
        assert not codex_entered.is_set()
        release_qwen.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert codex_entered.is_set()


def test_different_worktrees_allow_qwen_and_codex_to_enter_independently(tmp_path, monkeypatch):
    first_project = tmp_path / "one"
    second_project = tmp_path / "two"
    first_project.mkdir()
    second_project.mkdir()
    reset_runtime_locks(monkeypatch)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    qwen_entered = threading.Event()
    codex_entered = threading.Event()
    release = threading.Event()

    def qwen(*args):
        qwen_entered.set()
        assert release.wait(2)
        return "Qwen completed a concrete bounded repository operation successfully."

    def codex(*args):
        codex_entered.set()
        assert release.wait(2)
        return "Codex completed a concrete bounded repository operation successfully."

    monkeypatch.setattr(gateway, "run_qwen_agent", qwen)
    monkeypatch.setattr(gateway, "run_codex_agent", codex)

    async def scenario():
        tasks = [
            asyncio.create_task(gateway.execute_agent("q2", "local_code", "fix tests", str(first_project), False)),
            asyncio.create_task(gateway.execute_agent("c2", "codex", "review code", str(second_project), True)),
        ]
        for _ in range(100):
            if qwen_entered.is_set() and codex_entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert qwen_entered.is_set() and codex_entered.is_set()
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_direct_codex_failure_closes_the_running_attempt(tmp_path, monkeypatch):
    reset_runtime_locks(monkeypatch)
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(gateway, "collect_modified_files", lambda project: [])
    monkeypatch.setattr(
        gateway,
        "run_codex_agent",
        lambda *args: (_ for _ in ()).throw(RuntimeError("codex worker failed")),
    )

    with pytest.raises(RuntimeError, match="codex worker failed"):
        asyncio.run(
            gateway.execute_agent(
                "codex-failure",
                "codex",
                "review repository",
                str(tmp_path),
                True,
                mode="review",
            )
        )

    assert [entry[0][2] for entry in writes] == ["running", "failed"]
    assert writes[-1][1]["actual_executor"] == "codex_cli"
    assert "codex worker failed" in writes[-1][1]["error_summary"]


def test_cancellation_while_waiting_for_worktree_lock_is_terminal(tmp_path, monkeypatch):
    reset_runtime_locks(monkeypatch)
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def scenario():
        lock = gateway.get_worktree_lock(str(tmp_path))
        await lock.acquire()
        task = asyncio.create_task(
            gateway.execute_agent(
                "queued-cancel",
                "local_code",
                "fix tests",
                str(tmp_path),
                False,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()

    asyncio.run(scenario())
    assert [entry[0][2] for entry in writes] == ["running", "cancelled"]


def test_local_retry_policy_journals_cancellation_before_worktree_acquisition(tmp_path, monkeypatch):
    prompt, plan, decision = local_execution_contract(tmp_path)
    reset_runtime_locks(monkeypatch)
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def scenario():
        lock = gateway.get_worktree_lock(str(tmp_path))
        await lock.acquire()
        task = asyncio.create_task(
            gateway.execute_local_coding(
                task_id="local-queued-cancel",
                prompt=prompt,
                project=str(tmp_path),
                decision=decision,
                plan=plan,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()

    asyncio.run(scenario())
    assert [entry[0][2] for entry in writes] == ["cancelled"]
    assert writes[0][1]["reason_codes"] == ["executor.cancelled_while_queued"]
