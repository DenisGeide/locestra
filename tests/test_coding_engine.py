from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typing import Callable

from services.coding import engine as coding_engine
from services.coding import semantic_review
from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, load_coding_policy
from services.coding.context import CodingContext
from services.coding.contracts import (
    ArtifactKind,
    AttemptStatus,
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskStatus,
    CommandResultV1,
    CommandStatus,
    DataClassification,
    ExecutorKind,
    ReviewResultV1,
    ReviewVerdict,
    VerificationCommandV1,
    WorktreeRecordV1,
)
from services.coding.engine import CodingEngine, CodingEngineError
from services.coding.discovery import discover_verification_commands
from services.coding.executors import ExecutorFailure, ExecutorResult
from services.coding.git import (
    CodingRepositoryError,
    git_status_paths,
    resolve_repository,
)
from services.coding.handoff import CodingHandoffV1, HandoffManager, HandoffPolicyError
from services.coding.reviewer import DeterministicReviewer
from services.coding.semantic_review import (
    LocalSemanticReviewConfig,
    LocalSemanticReviewResult,
    SemanticAttestation,
    SemanticReviewBlocked,
    SemanticReviewEvidence,
    SemanticReviewSubject,
)
from services.coding.store import CodingTaskStore
from services.coding.worktrees import WorktreeLease, WorktreeManager
from services.knowledge.contracts import ContextEnvelopeV1, RetrievalResultV1
from tests.coding_fixtures import coding_fixture, file_snapshot


ROOT = Path(__file__).resolve().parents[1]
Action = Callable[[dict[str, object], int], ExecutorResult]


def test_semantic_command_artifact_lookup_accepts_typed_ui_evidence(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore("ui-lookup", root=tmp_path, policy=_policy())
    reference = artifacts.write_json(
        kind=ArtifactKind.UI_EVIDENCE,
        value={"status": "passed"},
        producer="coding-playwright",
    )
    state = SimpleNamespace(artifacts=[reference])

    resolved = CodingEngine._state_artifact(
        state,  # type: ignore[arg-type]
        reference.artifact_id,
        kind=(ArtifactKind.COMMAND_OUTPUT, ArtifactKind.UI_EVIDENCE),
    )

    assert resolved == reference


def test_verification_retry_message_preserves_actionable_output_tail(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore("verification-retry-tail", root=tmp_path, policy=_policy())
    command_id = "a1-verify-tail"
    output = artifacts.write_text(
        kind=ArtifactKind.COMMAND_OUTPUT,
        text=("setup noise\n" * 1_000) + "FINAL_ASSERTION: expected 4 but received 3",
        producer="synthetic-verifier",
        occurrence_id=command_id,
    )
    now = datetime.now(timezone.utc)
    result = CommandResultV1(
        command_id=command_id,
        argv=[sys.executable, "-m", "pytest", "-q"],
        cwd=str(tmp_path),
        purpose="Exercise a noisy failing verifier.",
        status=CommandStatus.FAILED,
        exit_code=1,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        output_artifact_id=output.artifact_id,
        summary="Noisy verifier failed.",
    )

    message = CodingEngine._verification_retry_message(
        [result],
        {command_id},
        artifacts,
    )

    assert len(message) <= 2_048
    assert "bounded verifier middle omitted" in message
    assert "FINAL_ASSERTION: expected 4 but received 3" in message


def test_optional_timeout_cannot_hide_later_required_verifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    artifacts = ArtifactStore("optional-timeout", root=tmp_path, policy=_policy())
    commands = [
        VerificationCommandV1(
            argv=[sys.executable, "-m", "pytest", "tests/test_optional.py"],
            purpose="Run an optional diagnostic.",
            timeout_seconds=5,
            required=False,
        ),
        VerificationCommandV1(
            argv=[sys.executable, "-m", "pytest", "tests/test_required.py"],
            purpose="Run the mandatory regression.",
            timeout_seconds=5,
            required=True,
        ),
    ]
    request = _request(
        repository,
        task_id="optional-timeout-task",
        verification_commands=commands,
    )
    calls: list[str] = []

    class TimeoutRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self, command, *, command_id: str, **kwargs: object):
            calls.append(command_id)
            now = datetime.now(timezone.utc)
            return CommandResultV1(
                command_id=command_id,
                argv=command.argv,
                cwd=str(repository),
                purpose=command.purpose,
                status=CommandStatus.TIMED_OUT,
                exit_code=None,
                started_at=now,
                finished_at=now,
                duration_ms=1,
                summary="Optional diagnostic timed out.",
            )

    monkeypatch.setattr(coding_engine, "VerificationRunner", TimeoutRunner)
    engine = object.__new__(CodingEngine)
    engine.policy = _policy()

    results, required, references = engine._verification(
        state=SimpleNamespace(request=request),
        repository=repository,
        attempt_index=1,
        artifacts=artifacts,
        cancel_event=None,
    )

    assert calls == ["a1-verify-1"]
    assert [item.status for item in results] == [CommandStatus.TIMED_OUT]
    assert required == {"a1-verify-2", "a1-verify-3"}
    assert required.difference(item.command_id for item in results)
    assert references == []


def test_verification_retry_message_divides_evidence_budget_across_failures(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore("verification-retry-fairness", root=tmp_path, policy=_policy())
    results: list[CommandResultV1] = []
    required: set[str] = set()
    for ordinal in range(1, 4):
        command_id = f"a1-verify-{ordinal}"
        required.add(command_id)
        output = artifacts.write_text(
            kind=ArtifactKind.COMMAND_OUTPUT,
            text=(f"noise-{ordinal}\n" * 500) + f"FINAL_FAILURE_{ordinal}",
            producer="synthetic-verifier",
            occurrence_id=command_id,
        )
        now = datetime.now(timezone.utc)
        results.append(
            CommandResultV1(
                command_id=command_id,
                argv=[sys.executable, "-m", "pytest", f"tests/test_{ordinal}.py"],
                cwd=str(tmp_path),
                purpose=f"Run required verifier {ordinal}.",
                status=CommandStatus.FAILED,
                exit_code=ordinal,
                started_at=now,
                finished_at=now,
                duration_ms=1,
                output_artifact_id=output.artifact_id,
                summary=f"Required verifier {ordinal} failed.",
            )
        )

    message = CodingEngine._verification_retry_message(results, required, artifacts)

    assert len(message) <= 2_048
    for ordinal in range(1, 4):
        assert f"a1-verify-{ordinal}" in message
        assert f"FINAL_FAILURE_{ordinal}" in message


def _policy() -> CodingPolicy:
    return load_coding_policy(ROOT / "config" / "coding.json")


def _request(
    repository: Path,
    *,
    task_id: str,
    mode: CodingMode = CodingMode.WRITE,
    risk: CodingRisk = CodingRisk.LOW,
    expected_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    rule_scope_paths: list[str] | None = None,
    verify_calculator: bool = False,
    verification_commands: list[VerificationCommandV1] | None = None,
    local_commit: bool = False,
    cloud_execution: bool = False,
) -> CodingTaskRequestV1:
    permissions = CodingPermissionsV1(
        modify_files=(mode is CodingMode.WRITE),
        local_commit=local_commit,
        cloud_execution=cloud_execution,
        data_classification=(
            DataClassification.PUBLIC
            if cloud_execution
            else DataClassification.INTERNAL
        ),
    )
    commands = (
        verification_commands
        if verification_commands is not None
        else (
            [
                VerificationCommandV1(
                    argv=[
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-v",
                    ],
                    purpose="Run the synthetic calculator regression test.",
                    timeout_seconds=60,
                )
            ]
            if verify_calculator
            else []
        )
    )
    return CodingTaskRequestV1(
        task_id=task_id,
        request_id=f"request-{task_id}",
        goal=(
            "Inspect the fixture and report the documented fact."
            if mode is CodingMode.READ_ONLY
            else "Make the exact bounded fixture correction."
        ),
        repository_path=str(repository),
        mode=mode,
        risk=risk,
        constraints=["Never push, deploy, install dependencies, or alter remotes."],
        acceptance_criteria=[
            "The requested fixture result is exact and independently reviewed."
        ],
        verification_plan=["Use only the declared verifier and the engine diff check."],
        verification_commands=commands,
        permissions=permissions,
        route_reasons=["Low-risk synthetic fixture."],
        rule_scope_paths=rule_scope_paths or [],
        expected_diff_paths=expected_paths or [],
        forbidden_diff_paths=forbidden_paths or [],
        commit_message=("Fix synthetic calculator" if local_commit else None),
    )


class _FakeContextBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def build(
        self,
        *,
        request: CodingTaskRequestV1,
        repository: Path,
        artifact_store: ArtifactStore,
        modified_files: tuple[str, ...] = (),
        unresolved_errors: tuple[str, ...] = (),
    ) -> CodingContext:
        self.calls.append(
            {
                "task_id": request.task_id,
                "repository": str(repository),
                "modified_files": tuple(modified_files),
                "unresolved_errors": tuple(unresolved_errors),
            }
        )
        evidence = RetrievalResultV1(
            project_path=str(repository),
            query=request.goal,
            token_budget=512,
            estimated_tokens=0,
            fragments=[],
        )
        envelope = ContextEnvelopeV1(
            project_path=str(repository),
            goal=request.goal,
            constraints=request.constraints,
            modified_files=list(modified_files),
            unresolved_errors=list(unresolved_errors),
            verification_plan=request.verification_plan,
            repository_summary={"fixture": True},
            evidence=evidence,
            token_budget=512,
            estimated_tokens=16,
        )
        artifact = artifact_store.write_json(
            kind=ArtifactKind.CONTEXT,
            value=envelope.model_dump(mode="json"),
            producer="fake-context",
        )
        return CodingContext(
            envelope=envelope,
            artifact=artifact,
            index_result={"fixture": True, "blocked_files": 0},
        )


class _FakeExecutor:
    def __init__(self, kind: ExecutorKind, actions: list[Action]) -> None:
        self.kind = kind
        self.actions = actions
        self.calls: list[dict[str, object]] = []

    def execute(self, **kwargs: object) -> ExecutorResult:
        ordinal = len(self.calls) + 1
        self.calls.append(dict(kwargs))
        if ordinal > len(self.actions):
            raise AssertionError(
                f"unexpected {self.kind.value} fake call {ordinal}; no live executor fallback allowed"
            )
        return self.actions[ordinal - 1](dict(kwargs), ordinal)


def _success(
    kwargs: dict[str, object],
    ordinal: int,
    *,
    executor: ExecutorKind,
    summary: str,
    inspected_files: tuple[str, ...] = (),
) -> ExecutorResult:
    artifacts = kwargs["artifact_store"]
    assert isinstance(artifacts, ArtifactStore)
    output = artifacts.write_text(
        kind=ArtifactKind.COMMAND_OUTPUT,
        text=f"fake {executor.value} output {ordinal}\nresult: {summary}",
        producer="fake-executor",
        redact=True,
    )
    return ExecutorResult(
        executor=(
            ExecutorKind.CODEX_REVIEW if kwargs.get("review_only") is True else executor
        ),
        summary=summary,
        session_id=f"fake-session-{executor.value}-{ordinal}",
        inspected_files=inspected_files,
        tool_names=("read_file",),
        command_count=0,
        output_artifact=output,
        duration_ms=1,
    )


def _failure(message: str) -> Action:
    def action(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
        artifacts = kwargs["artifact_store"]
        assert isinstance(artifacts, ArtifactStore)
        output = artifacts.write_text(
            kind=ArtifactKind.COMMAND_OUTPUT,
            text=f"bounded fake failure {ordinal}: {message}",
            producer="fake-executor",
            redact=True,
        )
        raise ExecutorFailure(
            message,
            output_artifact=output,
            session_id=f"fake-failed-session-{ordinal}",
        )

    return action


def _synthetic_passed_verification(**kwargs: object):
    artifacts = kwargs["artifacts"]
    attempt_index = kwargs["attempt_index"]
    assert isinstance(artifacts, ArtifactStore)
    assert isinstance(attempt_index, int)
    command_id = f"a{attempt_index}-verify-synthetic"
    output = artifacts.write_text(
        kind=ArtifactKind.COMMAND_OUTPUT,
        text="Synthetic acceptance behavior passed.",
        producer="synthetic-verifier",
        occurrence_id=command_id,
    )
    now = datetime.now(timezone.utc)
    result = CommandResultV1(
        command_id=command_id,
        argv=[sys.executable, "-m", "unittest", "tests.test_calculator", "-v"],
        cwd=".",
        purpose="Exercise the requested calculator behavior.",
        status=CommandStatus.PASSED,
        exit_code=0,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        output_artifact_id=output.artifact_id,
        summary="Synthetic acceptance verifier passed.",
    )
    return [result], {command_id}, [output]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _FakeSemanticReviewer:
    """Construct authenticated deterministic semantic results without a live API."""

    def __init__(
        self,
        *,
        approvals: list[bool] | None = None,
        blocked_codes: list[str] | None = None,
        on_review: Callable[[SemanticReviewSubject, int], None] | None = None,
        forge_result: bool = False,
    ) -> None:
        policy = _policy()
        self.config = LocalSemanticReviewConfig(
            model=policy.local_semantic_model,
            expected_executable_path="C:/Program Files/Ollama/ollama.exe",
            expected_executable_sha256="3" * 64,
            expected_model_digest=policy.local_semantic_expected_model_digest,
            timeout_seconds=float(policy.review_timeout_seconds),
            max_artifact_payload_bytes=policy.max_artifact_bytes,
        )
        self.approvals = list(approvals or [])
        self.blocked_codes = list(blocked_codes or [])
        self.on_review = on_review
        self.forge_result = forge_result
        self.calls: list[SemanticReviewSubject] = []
        self.deadlines: list[float | None] = []

    def review(
        self,
        subject: SemanticReviewSubject,
        *,
        assert_subject_current: Callable[[SemanticReviewSubject], None],
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> LocalSemanticReviewResult:
        ordinal = len(self.calls) + 1
        self.calls.append(subject)
        self.deadlines.append(deadline)
        # The production reviewer invokes the callback at all four attestation/
        # inference boundaries. Two calls are sufficient for this deterministic
        # test double to exercise the engine's before/after invariant contract.
        for phase in range(2):
            if cancel_event is not None and cancel_event.is_set():
                raise SemanticReviewBlocked(
                    "semantic_review.cancelled",
                    "Synthetic semantic review was cancelled.",
                )
            try:
                assert_subject_current(subject)
            except SemanticReviewBlocked:
                raise
            except Exception as exc:
                raise SemanticReviewBlocked(
                    "semantic_review.subject_stale",
                    "Synthetic reviewer rejected the current subject invariant.",
                ) from exc
            if phase == 0 and self.on_review is not None:
                self.on_review(subject, ordinal)

        if self.blocked_codes:
            code = self.blocked_codes.pop(0)
            raise SemanticReviewBlocked(
                code,
                "Synthetic bounded semantic reviewer failure.",
            )

        approved = self.approvals.pop(0) if self.approvals else True
        prepared = semantic_review._prepare_subject(subject, self.config)
        evidence_refs: list[dict[str, str]] = [
            {
                "kind": "artifact",
                "ref": (
                    "artifact.knowledge."
                    f"{subject.knowledge_artifact.reference.artifact_id}"
                ),
            }
        ]
        if subject.diff_artifact is not None:
            evidence_refs.insert(
                0,
                {
                    "kind": "artifact",
                    "ref": (
                        f"artifact.diff.{subject.diff_artifact.reference.artifact_id}"
                    ),
                },
            )
        evidence_refs.extend(
            {
                "kind": "command_result",
                "ref": f"command.{item.result.command_id}",
            }
            for item in subject.command_evidence
        )
        coverage = [
            {"requirement_id": requirement_id, "evidence_refs": evidence_refs}
            for requirement_id in prepared.requirement_ids
        ]
        findings: list[dict[str, object]] = []
        if not approved:
            findings.append(
                {
                    "priority": "P1",
                    "code": "behavior.wrong",
                    "title": "The claimed result contradicts current evidence",
                    "file": "README.md",
                    "line": 1,
                    "failure_scenario": (
                        "The exact current evidence does not establish the requested result."
                    ),
                    "requirement_ids": ["goal"],
                    "evidence_refs": evidence_refs,
                }
            )
        response = _canonical_json(
            {
                "schema_version": "1.0",
                "subject_sha256": prepared.sha256,
                "verdict": "approved" if approved else "rejected",
                "coverage": coverage,
                "findings": findings,
            }
        )
        verdict, parsed_coverage, parsed_findings = (
            semantic_review._parse_semantic_response(
                response,
                prepared=prepared,
                config=self.config,
            )
        )
        attestation = SemanticAttestation(
            listener_pid=4242,
            listener_create_time_ns=1_000_000_000,
            executable_path=self.config.expected_executable_path,
            executable_sha256=self.config.expected_executable_sha256,
            model_alias=self.config.model,
            model_digest=self.config.expected_model_digest,
        )
        attestation_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "after": attestation.sha256,
                    "before": attestation.sha256,
                }
            )
        ).hexdigest()
        result = LocalSemanticReviewResult(
            subject_sha256=prepared.sha256,
            deterministic_review_id=subject.deterministic_review_id,
            verdict=verdict,
            coverage=parsed_coverage,
            findings=parsed_findings,
            evidence=SemanticReviewEvidence(
                reviewed_at=datetime.now(timezone.utc),
                subject_sha256=prepared.sha256,
                canonical_subject=prepared.canonical_bytes,
                request_sha256=hashlib.sha256(
                    semantic_review._build_api_request(prepared, self.config)
                ).hexdigest(),
                model_response=response,
                model_response_sha256=hashlib.sha256(response).hexdigest(),
                canonical_response=response,
                response_sha256=hashlib.sha256(response).hexdigest(),
                attestation_before=attestation,
                attestation_after=attestation,
                attestation_sha256=attestation_sha256,
                verdict=verdict,
            ),
        )
        if self.forge_result:
            result = LocalSemanticReviewResult(
                subject_sha256="f" * 64,
                deterministic_review_id=result.deterministic_review_id,
                verdict=result.verdict,
                coverage=result.coverage,
                findings=result.findings,
                evidence=result.evidence,
            )
        return result


def _engine(
    fixture,
    *,
    qwen: _FakeExecutor,
    codex: _FakeExecutor | None = None,
    semantic: _FakeSemanticReviewer | None = None,
) -> tuple[CodingEngine, CodingTaskStore, WorktreeManager, _FakeContextBuilder]:
    policy = _policy()
    store = CodingTaskStore(
        fixture.root / "coding-state.sqlite3",
        harden_permissions=False,
    )
    manager = WorktreeManager(
        registry_root=fixture.root / "engine-registry",
        owned_worktree_root=fixture.root / "engine-worktrees",
        policy=policy,
    )
    context = _FakeContextBuilder()
    never_codex = codex or _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
    engine = CodingEngine(
        store=store,
        worktree_manager=manager,
        context_builder=context,
        qwen_executor=qwen,
        codex_executor=never_codex,
        semantic_reviewer=semantic or _FakeSemanticReviewer(),
        policy=policy,
        artifact_root=fixture.root / "engine-artifacts",
    )
    return engine, store, manager, context


def test_secret_redaction_of_local_semantic_review_blocks_without_stale_bindings():
    local_review = ReviewResultV1(
        reviewer_id="local-semantic-sensitive-review",
        reviewer=ExecutorKind.LOCAL_SEMANTIC_REVIEW,
        verdict=ReviewVerdict.APPROVED,
        findings=[],
        checked_requirements=True,
        checked_tests=True,
        checked_diff_scope=True,
        checked_secrets=True,
        checked_constitution=True,
        subject_sha256="a" * 64,
        evidence_artifact_id="review-evidence-sensitive",
        evidence_artifact_sha256="b" * 64,
        summary="-----BEGIN " + "PRIVATE KEY-----\nsynthetic-sensitive-material",
        reviewed_at=datetime.now(timezone.utc),
    )

    redacted = CodingEngine._safe_review(local_review)

    assert redacted.reviewer is ExecutorKind.DETERMINISTIC
    assert redacted.verdict is ReviewVerdict.BLOCKED
    assert redacted.subject_sha256 is None
    assert redacted.evidence_artifact_id is None
    assert redacted.evidence_artifact_sha256 is None
    assert redacted.findings[0].code == "review.secret_detected"


def test_read_only_engine_inspects_without_project_command_or_file_change():
    with coding_fixture(run_id="engine-read-only") as fixture:
        source_before = file_snapshot(fixture.repository)

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            assert "synthetic" in (repository / "README.md").read_text(encoding="utf-8")
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Inspected README without running a project command.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="read-only-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert result.verification_passed is True
        assert result.modified_files == []
        assert state is not None
        assert state.command_results == []
        assert state.review is not None
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert state.inspected_files == ["README.md"]
        assert len(qwen.calls) == 1
        assert file_snapshot(fixture.repository) == source_before
        assert file_snapshot(Path(result.worktree_path or "")) == source_before
        assert manager.load(request.task_id).status == "complete"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_local_read_fact_requires_attested_semantic_evidence_and_persists_bindings():
    with coding_fixture(run_id="engine-semantic-read-approved") as fixture:
        semantic = _FakeSemanticReviewer()

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="README identifies this as the synthetic coding fixture.",
                inspected_files=("README.md",),
            )

        engine, store, _, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect]),
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-read-approved-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None and state.review is not None
        assert state.review.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert state.review.subject_sha256 is not None
        assert state.review.evidence_artifact_id is not None
        assert state.review.evidence_artifact_sha256 is not None
        evidence = next(
            item
            for item in state.artifacts
            if item.artifact_id == state.review.evidence_artifact_id
        )
        assert evidence.kind is ArtifactKind.REVIEW
        assert evidence.producer == "local-semantic-reviewer"
        assert evidence.sha256 == state.review.evidence_artifact_sha256
        payload = json.loads(Path(evidence.path).read_text(encoding="utf-8"))
        assert payload["subject_sha256"] == state.review.subject_sha256
        assert payload["canonical_subject_sha256"] == state.review.subject_sha256
        canonical_subject = payload["canonical_subject"]
        assert canonical_subject["attempt_index"] == 1
        assert canonical_subject["source_base_commit"] == fixture.baseline_sha
        assert canonical_subject["worktree_binding_sha256"]
        canonical_executor_output = canonical_subject["executor_output_artifact"]
        assert canonical_executor_output["payload_sha256_only"] is True
        assert "payload_utf8_exact" not in canonical_executor_output
        assert (
            canonical_executor_output["sha256"]
            == semantic.calls[0].executor_output_artifact.reference.sha256
        )
        assert len(semantic.calls) == 1
        assert semantic.calls[0].executor_output_artifact.payload.endswith(
            b"README identifies this as the synthetic coding fixture."
        )


def test_retryable_semantic_block_retries_only_reviewer_and_persists_call_audit():
    with coding_fixture(run_id="engine-semantic-reviewer-retry") as fixture:
        semantic = _FakeSemanticReviewer(
            blocked_codes=["semantic_review.response_truncated"]
        )

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Current README fact.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect])
        engine, store, _, _ = _engine(
            fixture,
            qwen=qwen,
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-reviewer-retry-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert len(qwen.calls) == 1
        assert len(semantic.calls) == 2
        assert semantic.calls[0] is semantic.calls[1]
        assert semantic.deadlines[0] is not None
        assert semantic.deadlines[0] == semantic.deadlines[1]
        assert len(state.attempts) == 1
        assert state.attempts[0].status is AttemptStatus.PASSED
        audits = [
            item
            for item in state.artifacts
            if item.producer == coding_engine._LOCAL_SEMANTIC_RETRY_AUDIT_PRODUCER
        ]
        assert len(audits) == 1
        audit = json.loads(Path(audits[0].path).read_text(encoding="utf-8"))
        assert audit == {
            "coding_attempt_index": 1,
            "max_reviewer_retries": 1,
            "producer": coding_engine._LOCAL_SEMANTIC_RETRY_AUDIT_PRODUCER,
            "review_calls": [
                {
                    "block_code": "semantic_review.response_truncated",
                    "call_index": 1,
                    "outcome": "blocked",
                    "retry_scheduled": True,
                },
                {
                    "call_index": 2,
                    "outcome": "completed",
                    "retry_scheduled": False,
                    "verdict": "approved",
                },
            ],
            "schema_version": "1.0",
            "subject_sha256": state.review.subject_sha256,
        }
        assert audits[0].artifact_id in state.attempts[0].artifact_ids


def test_two_retryable_semantic_blocks_fail_closed_without_rerunning_qwen():
    with coding_fixture(run_id="engine-semantic-reviewer-exhausted") as fixture:
        semantic = _FakeSemanticReviewer(
            blocked_codes=[
                "semantic_review.response_truncated",
                "semantic_review.coverage_invalid",
            ]
        )

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Current README fact.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect])
        engine, store, _, _ = _engine(
            fixture,
            qwen=qwen,
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-reviewer-exhausted-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.BLOCKED
        assert len(qwen.calls) == 1
        assert len(semantic.calls) == 2
        assert semantic.calls[0] is semantic.calls[1]
        assert semantic.deadlines[0] is not None
        assert semantic.deadlines[0] == semantic.deadlines[1]
        assert len(state.attempts) == 1
        assert state.attempts[0].status is AttemptStatus.FAILED
        assert any(
            item.code.startswith(coding_engine._LOCAL_SEMANTIC_NO_CODING_RETRY_PREFIX)
            for item in state.review.findings
        )
        audits = [
            item
            for item in state.artifacts
            if item.producer == coding_engine._LOCAL_SEMANTIC_RETRY_AUDIT_PRODUCER
        ]
        assert len(audits) == 1
        audit = json.loads(Path(audits[0].path).read_text(encoding="utf-8"))
        assert [item["call_index"] for item in audit["review_calls"]] == [1, 2]
        assert [item["outcome"] for item in audit["review_calls"]] == [
            "blocked",
            "blocked",
        ]
        assert audit["review_calls"][1]["retry_scheduled"] is False
        assert audits[0].artifact_id in state.attempts[0].artifact_ids


def test_authenticated_semantic_rejection_starts_new_coding_attempt_not_review_retry():
    with coding_fixture(run_id="engine-semantic-authenticated-reject") as fixture:
        semantic = _FakeSemanticReviewer(approvals=[False, True])

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary=f"Current README fact hypothesis {ordinal}.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect, inspect])
        engine, store, _, _ = _engine(
            fixture,
            qwen=qwen,
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-authenticated-reject-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert len(qwen.calls) == 2
        assert len(semantic.calls) == 2
        assert [item.attempt_index for item in semantic.calls] == [1, 2]
        assert [item.status for item in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert not any(
            item.producer == coding_engine._LOCAL_SEMANTIC_RETRY_AUDIT_PRODUCER
            for item in state.artifacts
        )


def test_wrong_local_read_fact_is_rejected_by_semantic_review_until_handoff():
    with coding_fixture(run_id="engine-semantic-read-rejected") as fixture:
        semantic = _FakeSemanticReviewer(approvals=[False, False])

        def wrong_fact(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="README says this is a production banking repository.",
                inspected_files=("README.md",),
            )

        engine, store, _, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [wrong_fact, wrong_fact]),
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-read-rejected-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert state.review.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
        assert state.review.verdict is ReviewVerdict.REJECTED
        assert any(
            item.code == "local_semantic.p1.behavior.wrong"
            for item in state.review.findings
        )
        assert len(semantic.calls) == 2
        assert all(
            b"production banking repository" in subject.executor_output_artifact.payload
            for subject in semantic.calls
        )


def test_wrong_local_write_is_rejected_despite_generic_green_test():
    with coding_fixture(run_id="engine-semantic-write-rejected") as fixture:
        semantic = _FakeSemanticReviewer(approvals=[False, False])

        def wrong_change(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            calculator = repository / "src" / "calculator.py"
            calculator.write_text(
                calculator.read_text(encoding="utf-8").replace(
                    "return left - right",
                    "return 5",
                ),
                encoding="utf-8",
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="The single generic example passes.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        engine, store, _, _ = _engine(
            fixture,
            qwen=_FakeExecutor(
                ExecutorKind.LOCAL_QWEN,
                [wrong_change, wrong_change],
            ),
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-write-rejected-task",
            expected_paths=["src/calculator.py"],
            rule_scope_paths=["src/calculator.py"],
            verify_calculator=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.REJECTED
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert any(
            item.code == "local_semantic.p1.behavior.wrong"
            for item in state.review.findings
        )
        assert semantic.calls
        assert all(subject.diff_artifact is not None for subject in semantic.calls)
        assert all(subject.command_evidence for subject in semantic.calls)


@pytest.mark.parametrize("tamper", ["forged-result", "stale-artifact"])
def test_forged_or_stale_local_semantic_evidence_blocks_closed(tamper: str):
    with coding_fixture(run_id=f"engine-semantic-{tamper}") as fixture:

        def mutate_evidence(subject: SemanticReviewSubject, _ordinal: int) -> None:
            if tamper == "stale-artifact":
                Path(subject.executor_output_artifact.reference.path).write_bytes(
                    subject.executor_output_artifact.payload + b"\nsubstituted"
                )

        semantic = _FakeSemanticReviewer(
            on_review=mutate_evidence,
            forge_result=(tamper == "forged-result"),
        )

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Current README fact.",
                inspected_files=("README.md",),
            )

        engine, store, _, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect, inspect]),
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id=f"semantic-{tamper}-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.BLOCKED
        assert state.review.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
        assert state.review.checked_requirements is False
        if tamper == "forged-result":
            assert state.review.evidence_artifact_id is None
        else:
            assert any("subject_stale" in item.code for item in state.review.findings)


def test_cancellation_during_local_semantic_review_preserves_worktree():
    with coding_fixture(run_id="engine-semantic-cancel") as fixture:
        cancelled = threading.Event()

        def cancel_review(_subject: SemanticReviewSubject, _ordinal: int) -> None:
            cancelled.set()

        semantic = _FakeSemanticReviewer(on_review=cancel_review)

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Current README fact.",
                inspected_files=("README.md",),
            )

        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect]),
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-cancel-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request, cancel_event=cancelled)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.CANCELLED
        assert state is not None
        assert state.attempts[-1].status is AttemptStatus.CANCELLED
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]


def test_cancellation_during_reviewer_only_retry_does_not_rerun_qwen():
    with coding_fixture(run_id="engine-semantic-retry-cancel") as fixture:
        cancelled = threading.Event()

        def cancel_second_review(
            _subject: SemanticReviewSubject,
            ordinal: int,
        ) -> None:
            if ordinal == 2:
                cancelled.set()

        semantic = _FakeSemanticReviewer(
            blocked_codes=["semantic_review.api_failed"],
            on_review=cancel_second_review,
        )

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Current README fact.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=qwen,
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="semantic-retry-cancel-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request, cancel_event=cancelled)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.CANCELLED
        assert state is not None
        assert len(qwen.calls) == 1
        assert len(semantic.calls) == 2
        assert len(state.attempts) == 1
        assert state.attempts[0].status is AttemptStatus.CANCELLED
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]


def test_declared_nested_rule_scope_is_rendered_relative_to_the_task_worktree():
    with coding_fixture(run_id="engine-declared-nested-rules") as fixture:
        source_root = str(fixture.repository.resolve(strict=True))

        def inspect(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            prompt = str(kwargs["prompt"])
            assert "AGENTS.md" in prompt
            assert "src/AGENTS.md" in prompt
            assert source_root not in prompt
            assert "Source rules" in (repository / "src" / "AGENTS.md").read_text(
                encoding="utf-8"
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Read the applicable worktree rule before the nested source file.",
                inspected_files=("src/AGENTS.md", "src/calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [inspect])
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="declared-nested-rules-task",
            mode=CodingMode.READ_ONLY,
            rule_scope_paths=["src/calculator.py"],
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None
        assert [item.scope for item in state.applicable_rules] == [
            "repository-root",
            "src",
        ]
        assert len(qwen.calls) == 1


def test_dirty_applicable_agents_rule_blocks_before_executor_reads_stale_bytes():
    with coding_fixture(run_id="engine-dirty-applicable-rule") as fixture:
        nested_rule = fixture.repository / "src" / "AGENTS.md"
        dirty_bytes = nested_rule.read_bytes() + b"\n- DIRTY SOURCE RULE\n"
        nested_rule.write_bytes(dirty_bytes)
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        engine, store, manager, context = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="dirty-applicable-rule-task",
            mode=CodingMode.READ_ONLY,
            rule_scope_paths=["src/calculator.py"],
        )

        with pytest.raises(
            CodingEngineError,
            match="source repository has tracked or untracked changes",
        ):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is None
        assert manager.load(request.task_id) is None
        assert nested_rule.read_bytes() == dirty_bytes
        assert qwen.calls == []
        assert context.calls == []


def test_untracked_applicable_agents_rule_blocks_before_executor_runs():
    with coding_fixture(run_id="engine-untracked-applicable-rule") as fixture:
        nested_rule = fixture.repository / "web" / "AGENTS.md"
        rule_bytes = b"# Uncommitted web rules\n\n- Treat this scope as read-only.\n"
        nested_rule.write_bytes(rule_bytes)
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        engine, store, manager, context = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="untracked-applicable-rule-task",
            mode=CodingMode.READ_ONLY,
            rule_scope_paths=["web/index.html"],
        )

        with pytest.raises(
            CodingEngineError,
            match="source repository has tracked or untracked changes",
        ):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is None
        assert manager.load(request.task_id) is None
        assert nested_rule.read_bytes() == rule_bytes
        assert qwen.calls == []
        assert context.calls == []


def test_deleted_applicable_agents_rule_cannot_leave_stale_worktree_rule():
    with coding_fixture(run_id="engine-deleted-applicable-rule") as fixture:
        nested_rule = fixture.repository / "src" / "AGENTS.md"
        nested_rule.unlink()
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        engine, store, manager, context = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="deleted-applicable-rule-task",
            mode=CodingMode.READ_ONLY,
            rule_scope_paths=["src/calculator.py"],
        )

        with pytest.raises(
            CodingEngineError,
            match="source repository has tracked or untracked changes",
        ):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is None
        assert manager.load(request.task_id) is None
        assert not nested_rule.exists()
        assert qwen.calls == []
        assert context.calls == []


def test_forbidden_diff_path_blocks_review_even_without_positive_allowlist():
    with coding_fixture(run_id="engine-forbidden-diff") as fixture:
        source = resolve_repository(str(fixture.repository))
        _, _, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
        )
        request = _request(
            fixture.repository,
            task_id="forbidden-diff-task",
            forbidden_paths=["README.md"],
        )
        record = manager.create(task_id=request.task_id, repository=source)
        target = Path(record.worktree_path)
        readme = target / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nFORBIDDEN CHANGE\n",
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc)
        command = CommandResultV1(
            command_id="semantic-check",
            argv=[sys.executable, "-m", "unittest", "tests.exact_check"],
            cwd=str(target),
            purpose="Provide synthetic passed semantic evidence.",
            status=CommandStatus.PASSED,
            exit_code=0,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            summary="verification passed",
        )

        review = DeterministicReviewer(policy=_policy()).review(
            request=request,
            source_snapshot=source,
            target_repository=target,
            worktree=record,
            command_results=[command],
            required_command_ids={command.command_id},
        )

        codes = {item.code for item in review.findings}
        assert review.verdict is ReviewVerdict.REJECTED
        assert "diff.forbidden_file" in codes
        assert "diff.unexpected_file" not in codes
        assert review.checked_diff_scope is False


@pytest.mark.parametrize(
    ("destination", "forbidden_paths", "expected_codes"),
    [
        (
            "forbidden/calculator.py",
            ["forbidden"],
            {"diff.unexpected_file", "diff.forbidden_file"},
        ),
        ("outside/calculator.py", [], {"diff.unexpected_file"}),
    ],
)
def test_rename_destination_cannot_hide_behind_allowed_source_path(
    destination: str,
    forbidden_paths: list[str],
    expected_codes: set[str],
):
    with coding_fixture(run_id=f"engine-rename-{destination.split('/')[0]}") as fixture:

        def rename_allowed_source(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            source = repository / "src" / "calculator.py"
            target = repository / destination
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Renamed an allowed source into a non-allowed destination.",
                inspected_files=("src/calculator.py",),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [rename_allowed_source, rename_allowed_source],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id=f"rename-{destination.split('/')[0]}-task",
            expected_paths=["src/calculator.py"],
            forbidden_paths=forbidden_paths,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert expected_codes.issubset(
            {finding.code for finding in state.review.findings}
        )
        status_paths = git_status_paths(Path(result.worktree_path or ""))
        assert destination in status_paths
        assert "src/calculator.py" in status_paths


def test_leading_whitespace_path_is_not_normalized_into_declared_src_scope():
    with coding_fixture(run_id="engine-leading-whitespace-scope") as fixture:

        def create_deceptive_path(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / " src" / "evil.py"
            target.parent.mkdir(exist_ok=True)
            target.write_text("print('outside src')\n", encoding="utf-8")
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Created a leading-whitespace path.",
                inspected_files=("src/calculator.py",),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [create_deceptive_path, create_deceptive_path],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="leading-whitespace-scope-task",
            expected_paths=["src"],
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert "diff.unexpected_file" in {
            finding.code for finding in state.review.findings
        }
        assert git_status_paths(Path(result.worktree_path or "")) == [" src/evil.py"]


def test_discovered_read_only_nested_rule_fails_closed_then_retries_with_rule():
    with coding_fixture(run_id="engine-discovered-read-rules") as fixture:
        source_root = str(fixture.repository.resolve(strict=True))

        def discover(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Discovered a nested source target.",
                inspected_files=("src/calculator.py",),
            )

        def retry_with_rule(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            prompt = str(kwargs["prompt"])
            assert "src/AGENTS.md" in prompt
            assert source_root not in prompt
            assert "New applicable AGENTS.md rule scope" in prompt
            assert (repository / "src" / "AGENTS.md").is_file()
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Repeated the inspection after reading the nested rule.",
                inspected_files=("src/AGENTS.md", "src/calculator.py"),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [discover, retry_with_rule],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="discovered-read-rules-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None
        assert [item.status for item in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert [item.scope for item in state.applicable_rules] == [
            "repository-root",
            "src",
        ]
        assert state.command_results == []
        assert len(qwen.calls) == 2


def test_single_attempt_codex_review_cannot_pass_after_discovering_nested_rule():
    with coding_fixture(run_id="engine-single-attempt-rule-block") as fixture:

        def discover(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="- [P1] synthetic finding — src/security_runner.py:10",
                inspected_files=("src/security_runner.py",),
            )

        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [discover])
        engine, store, _, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="single-attempt-rule-block-task",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.BLOCKED
        assert state is not None
        assert state.review is None
        assert [item.status for item in state.attempts] == [AttemptStatus.FAILED]
        assert [item.scope for item in state.applicable_rules] == [
            "repository-root",
            "src",
        ]
        assert len(codex.calls) == 1


def test_modified_nested_path_discovers_rule_before_any_verifier_or_approval():
    with coding_fixture(run_id="engine-discovered-write-rules") as fixture:

        def first_fix(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right",
                    b"return left + right",
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Changed a nested file before its rule scope was known.",
            )

        def retry_with_rule(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            assert "src/AGENTS.md" in str(kwargs["prompt"])
            assert (repository / "src" / "AGENTS.md").is_file()
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Validated the existing correction under the nested rule.",
                inspected_files=(
                    "src/AGENTS.md",
                    "src/calculator.py",
                    "tests/test_calculator.py",
                ),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [first_fix, retry_with_rule],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="discovered-write-rules-task",
            verify_calculator=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None
        assert [item.status for item in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert all(item.command_id.startswith("a2-") for item in state.command_results)
        assert state.review is not None
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert [item.scope for item in state.applicable_rules] == [
            "repository-root",
            "src",
        ]
        fixture.assert_remote_unchanged()


def test_failed_executor_edit_still_records_nested_rule_for_retry():
    with coding_fixture(run_id="engine-failed-write-rule-discovery") as fixture:

        def fail_after_edit(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right",
                    b"return left + right",
                )
            )
            artifacts = kwargs["artifact_store"]
            assert isinstance(artifacts, ArtifactStore)
            output = artifacts.write_text(
                kind=ArtifactKind.COMMAND_OUTPUT,
                text="synthetic executor failure after nested edit",
                producer="fake-executor",
                redact=True,
            )
            raise ExecutorFailure(
                "synthetic failure after edit",
                output_artifact=output,
                session_id=f"fake-failed-rule-session-{ordinal}",
            )

        def retry_with_rule(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert "src/AGENTS.md" in str(kwargs["prompt"])
            assert "New applicable AGENTS.md rule scope" in str(kwargs["prompt"])
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Validated the correction after the failed executor cycle.",
                inspected_files=("src/AGENTS.md", "src/calculator.py"),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [fail_after_edit, retry_with_rule],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="failed-write-rule-discovery-task",
            verify_calculator=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert state is not None
        assert [item.status for item in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert "New applicable AGENTS.md rule scope" in (
            state.attempts[0].error_summary or ""
        )
        assert [item.scope for item in state.applicable_rules] == [
            "repository-root",
            "src",
        ]
        assert all(item.command_id.startswith("a2-") for item in state.command_results)


def test_low_risk_read_only_qwen_ignored_artifact_mutation_handoffs():
    with coding_fixture(run_id="engine-read-only-ignored-artifact") as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = fixture.git(
            [
                "--git-dir",
                str(fixture.remote),
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs",
            ],
            cwd=fixture.root,
        ).stdout

        def create_ignored_artifact(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            ignored_root = repository / "artifacts"
            ignored_root.mkdir(exist_ok=True)
            target = ignored_root / f"forbidden-read-only-{ordinal}.txt"
            target.write_bytes(f"ignored mutation {ordinal}\n".encode("ascii"))
            assert git_status_paths(repository) == []
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Read-only inspection completed successfully.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [create_ignored_artifact, create_ignored_artifact],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="read-only-ignored-artifact",
            mode=CodingMode.READ_ONLY,
        )

        with pytest.raises(CodingEngineError, match="ignored files appeared.*executor"):
            engine.run(request)
        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        worktree = Path(state.worktree.worktree_path)

        assert state.status is CodingTaskStatus.FAILED
        assert state.command_results == []
        assert state.modified_files == []
        assert state.review is None
        assert state.commit_sha is None
        assert len(qwen.calls) == 1
        assert git_status_paths(worktree) == []
        assert (worktree / "artifacts" / "forbidden-read-only-1.txt").is_file()
        assert not (worktree / "artifacts" / "forbidden-read-only-2.txt").exists()
        assert fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip() == (
            fixture.baseline_sha
        )
        assert file_snapshot(fixture.repository) == source_before
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert (
            fixture.git(
                [
                    "--git-dir",
                    str(fixture.remote),
                    "for-each-ref",
                    "--format=%(refname)%09%(objectname)",
                    "refs",
                ],
                cwd=fixture.root,
            ).stdout
            == remote_before
        )
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_diff_attribute_cannot_hide_secret_changed_bytes_from_review():
    with coding_fixture(run_id="engine-hidden-secret-attribute") as fixture:

        def hide_secret(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            (repository / ".gitattributes").write_text(
                "private.txt -diff\n",
                encoding="utf-8",
            )
            (repository / "private.txt").write_text(
                "-----BEGIN " + "PRIVATE KEY-----\nnot-for-commit\n",
                encoding="utf-8",
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Attempted to hide a secret behind a binary diff attribute.",
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [hide_secret])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="hidden-secret-attribute-task",
            expected_paths=[".gitattributes", "private.txt"],
        )

        with pytest.raises(
            CodingRepositoryError,
            match="unreviewable Git diff attribute|privacy rule",
        ):
            engine.run(request)
        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.review is None
        assert state.commit_sha is None
        assert len(qwen.calls) == 1
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_dirty_source_fails_before_state_worktree_or_executor():
    with coding_fixture(run_id="engine-dirty-source-preflight") as fixture:
        readme = fixture.repository / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nUSER DIRTY CHANGE\n",
            encoding="utf-8",
        )
        (fixture.repository / "user-untracked.txt").write_text(
            "keep this user file\n",
            encoding="utf-8",
        )
        source_before = file_snapshot(fixture.repository)
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="dirty-source-preflight-task",
        )

        with pytest.raises(CodingEngineError, match="reconcile or commit"):
            engine.run(request)

        assert store.load(request.task_id) is None
        assert manager.load(request.task_id) is None
        assert qwen.calls == []
        assert file_snapshot(fixture.repository) == source_before
        assert not any(manager.records_dir.glob("*.json"))
        fixture.assert_remote_unchanged()


@pytest.mark.parametrize(
    ("filename", "payload", "commands"),
    [
        (
            "requirements.txt",
            "requests==2.32.4\n",
            [
                VerificationCommandV1(
                    argv=["python", "-m", "pytest", "-q"],
                    purpose="Run dependency-bearing Python tests.",
                    timeout_seconds=60,
                )
            ],
        ),
        (
            "package.json",
            '{"scripts":{"test":"node test.js"},"dependencies":{"left-pad":"1.3.0"}}\n',
            [
                VerificationCommandV1(
                    argv=["npm.cmd", "test"],
                    purpose="Run dependency-bearing Node tests.",
                    timeout_seconds=60,
                )
            ],
        ),
        (
            "go.mod",
            "module example.invalid/fixture\n\ngo 1.24\n",
            [
                VerificationCommandV1(
                    argv=["git", "status", "--short"],
                    purpose="An unrelated supported command cannot verify Go.",
                    timeout_seconds=60,
                )
            ],
        ),
        ("Cargo.toml", '[package]\nname="fixture"\nversion="0.1.0"\n', []),
        ("fixture.sln", "Microsoft Visual Studio Solution File\n", []),
    ],
)
def test_unsupported_verifier_capability_fails_before_executor_or_worktree(
    filename: str,
    payload: str,
    commands: list[VerificationCommandV1],
):
    slug = Path(filename).stem.casefold()
    with coding_fixture(run_id=f"engine-capability-{slug}") as fixture:
        (fixture.repository / filename).write_text(payload, encoding="utf-8")
        fixture.git(["add", filename])
        fixture.git(["commit", "-m", f"add {filename} capability fixture"])
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id=f"capability-{slug}-task",
            verification_commands=commands,
        )

        if filename in {"go.mod", "Cargo.toml", "fixture.sln"}:
            discovered_programs = {
                Path(item.argv[0])
                .name.casefold()
                .removesuffix(".exe")
                .removesuffix(".cmd")
                for item in discover_verification_commands(fixture.repository)
            }
            assert not discovered_programs.intersection({"go", "cargo", "dotnet"})
        with pytest.raises(CodingEngineError, match="capability preflight"):
            engine.run(request)

        assert store.load(request.task_id) is None
        assert manager.load(request.task_id) is None
        assert qwen.calls == []
        fixture.assert_remote_unchanged()


@pytest.mark.parametrize(
    ("relative", "payload"),
    [
        (".env", "API_TOKEN=test-private-fixture\n"),
        (
            "src/tracked_secret.py",
            'KEY = "-----BEGIN ' + 'PRIVATE KEY-----\\nprivate-material"\n',
        ),
    ],
)
def test_public_claim_with_tracked_private_data_never_starts_codex(
    relative: str,
    payload: str,
):
    with coding_fixture(
        run_id=f"engine-public-block-{Path(relative).name.replace('.', '-')}"
    ) as fixture:
        target = fixture.repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        fixture.git(["add", "--force", relative])
        fixture.git(["commit", "-m", "add private public-preflight fixture"])
        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id=f"public-block-{Path(relative).stem.replace('_', '-')}-task",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
        )

        with pytest.raises(CodingEngineError, match="PUBLIC repository preflight"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert codex.calls == []
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


@pytest.mark.parametrize("unreachable", [False, True])
def test_public_claim_scans_secret_blobs_in_history_and_unreachable_objects(
    unreachable: bool,
):
    suffix = "unreachable" if unreachable else "history"
    with coding_fixture(run_id=f"engine-public-old-secret-{suffix}") as fixture:
        base = fixture.git(["rev-parse", "HEAD"]).stdout.strip()
        target = fixture.repository / "src" / "historical_private.py"
        target.write_text(
            'KEY = "-----BEGIN ' + 'PRIVATE KEY-----\\nhistorical-material"\n',
            encoding="utf-8",
        )
        fixture.git(["add", "src/historical_private.py"])
        fixture.git(["commit", "-m", "add historical fixture"])
        if unreachable:
            fixture.git(["reset", "--hard", base])
        else:
            target.unlink()
            fixture.git(["add", "--all"])
            fixture.git(["commit", "-m", "remove historical fixture"])

        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id=f"public-old-secret-{suffix}-task",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
        )

        with pytest.raises(CodingEngineError, match="PUBLIC repository preflight"):
            engine.run(request)

        assert codex.calls == []
        state = store.load(request.task_id)
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_public_claim_rejects_deleted_historical_blocked_directory_before_codex():
    with coding_fixture(run_id="engine-public-old-blocked-directory") as fixture:
        historical = fixture.repository / ".ssh" / "notes.txt"
        historical.parent.mkdir()
        historical.write_text(
            "This innocuous text still lived under a non-public directory.\n",
            encoding="utf-8",
        )
        fixture.git(["add", ".ssh/notes.txt"])
        fixture.git(["commit", "-m", "add historical blocked directory"])
        historical.unlink()
        historical.parent.rmdir()
        fixture.git(["add", "--all"])
        fixture.git(["commit", "-m", "remove historical blocked directory"])

        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="public-old-blocked-directory-task",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
        )

        with pytest.raises(CodingEngineError, match="PUBLIC repository preflight"):
            engine.run(request)

        assert codex.calls == []
        state = store.load(request.task_id)
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_public_claim_rejects_credential_bearing_local_git_config_before_codex():
    with coding_fixture(run_id="engine-public-config-secret") as fixture:
        source_before = file_snapshot(fixture.repository)
        fixture.git(
            [
                "remote",
                "set-url",
                "origin",
                "https://alice:"
                + "ghp_"
                + "A" * 36
                + "@example.invalid/repo.git",
            ]
        )
        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="public-config-secret-task",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
        )

        with pytest.raises(
            CodingRepositoryError,
            match="local Git remote config is unsupported",
        ):
            engine.run(request)

        assert qwen.calls == []
        assert codex.calls == []
        assert store.load(request.task_id) is None
        assert manager.load(request.task_id) is None
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_failed_writable_verifier_ignored_artifact_stops_before_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-verifier-ignored-artifact") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right",
                    b"return left + right",
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected the calculator before verification.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [fix_calculator, fix_calculator],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)

        def failing_verification_with_ignored_artifact(**kwargs: object):
            repository = Path(str(kwargs["repository"]))
            ignored_root = repository / "artifacts"
            ignored_root.mkdir(exist_ok=True)
            ignored = ignored_root / "verifier-cache.bin"
            ignored.write_bytes(b"verifier-cache")
            stable_stamp = (repository / "README.md").stat().st_mtime_ns
            os.utime(ignored, ns=(stable_stamp, stable_stamp))
            now = datetime.now(timezone.utc)
            result = CommandResultV1(
                command_id="a1-verify-1",
                argv=[sys.executable, "-m", "unittest", "tests.test_calculator", "-v"],
                cwd=str(repository),
                purpose="Synthetic failing writable verifier.",
                status=CommandStatus.FAILED,
                exit_code=1,
                started_at=now,
                finished_at=now,
                duration_ms=1,
                summary="synthetic verification failure",
            )
            return [result], {result.command_id}, []

        monkeypatch.setattr(
            engine,
            "_verification",
            failing_verification_with_ignored_artifact,
        )
        request = _request(
            fixture.repository,
            task_id="verifier-ignored-artifact-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
        )

        with pytest.raises(
            CodingEngineError,
            match="ignored files appeared.*after writable verification",
        ):
            engine.run(request)
        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        worktree = Path(state.worktree.worktree_path)

        assert state.status is CodingTaskStatus.FAILED
        assert state.command_results == []
        assert state.review is None
        assert state.commit_sha is None
        assert len(qwen.calls) == 1
        assert (worktree / "artifacts" / "verifier-cache.bin").is_file()
        assert git_status_paths(worktree) == ["src/calculator.py"]
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_source_ignored_artifact_mutation_is_detected_during_read_only_task():
    with coding_fixture(run_id="engine-source-ignored-mutation") as fixture:

        def mutate_ignored_source(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            source_artifacts = fixture.repository / "artifacts"
            source_artifacts.mkdir(exist_ok=True)
            (source_artifacts / "external-cache.txt").write_text(
                f"forbidden source mutation {ordinal}\n",
                encoding="utf-8",
                newline="\n",
            )
            assert git_status_paths(fixture.repository) == []
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Inspected the fixture without reporting a source mutation.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [mutate_ignored_source, mutate_ignored_source],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="source-ignored-mutation-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert "source.dirty_work_changed" in {
            finding.code for finding in state.review.findings
        }
        assert state.modified_files == []
        assert git_status_paths(fixture.repository) == []
        assert (fixture.repository / "artifacts" / "external-cache.txt").is_file()
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


@pytest.mark.parametrize(
    "mutation",
    [
        "remote-config",
        "tag",
        "remote-tracking-ref",
        "unrelated-local-branch",
        "owned-prefix-user-branch",
    ],
)
def test_shared_git_metadata_mutation_is_rejected_before_completion(mutation: str):
    with coding_fixture(run_id=f"engine-git-metadata-{mutation}") as fixture:
        user_branch = (
            "local-agent/task-user-owned"
            if mutation == "owned-prefix-user-branch"
            else "user-feature"
        )
        if mutation in {"unrelated-local-branch", "owned-prefix-user-branch"}:
            fixture.git(["branch", user_branch, fixture.baseline_sha])
        mutated = False

        def mutate_git_metadata(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            nonlocal mutated
            if not mutated:
                if mutation == "remote-config":
                    fixture.git(
                        [
                            "remote",
                            "set-url",
                            "origin",
                            str(fixture.root / "unexpected-remote.git"),
                        ]
                    )
                elif mutation == "tag":
                    fixture.git(["tag", "unexpected-tag", fixture.baseline_sha])
                elif mutation == "remote-tracking-ref":
                    fixture.git(
                        [
                            "update-ref",
                            "refs/remotes/origin/unexpected",
                            fixture.baseline_sha,
                        ]
                    )
                else:
                    fixture.git(["branch", "-D", user_branch])
                mutated = True
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Returned without reporting the protected Git metadata mutation.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [mutate_git_metadata, mutate_git_metadata],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id=f"git-metadata-{mutation}-task",
            mode=CodingMode.READ_ONLY,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert "source.git_metadata_changed" in {
            finding.code for finding in state.review.findings
        }
        assert state.commit_sha is None
        assert len(qwen.calls) == 2
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_high_risk_read_only_codex_review_delivers_findings_without_mutation():
    with coding_fixture(run_id="engine-codex-review-findings") as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = fixture.git(
            [
                "--git-dir",
                str(fixture.remote),
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs",
            ],
            cwd=fixture.root,
        ).stdout

        def security_review(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert kwargs.get("review_only") is True
            repository = Path(str(kwargs["repository"]))
            assert "shell=True" in (
                repository / "src" / "security_runner.py"
            ).read_text(encoding="utf-8")
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary=(
                    "- [P1] command injection — src/security_runner.py:9\n"
                    "An untrusted report_name is interpolated into a command executed "
                    "with shell=True."
                ),
                inspected_files=("src/security_runner.py",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [security_review])
        semantic = _FakeSemanticReviewer()
        engine, store, manager, _ = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
            semantic=semantic,
        )
        request = _request(
            fixture.repository,
            task_id="high-risk-read-only-review",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            rule_scope_paths=["src/security_runner.py"],
            cloud_execution=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.COMPLETED
        assert result.review_verdict is ReviewVerdict.REJECTED
        assert result.verification_passed is True
        assert result.modified_files == []
        assert result.commit_sha is None
        assert state is not None
        assert state.command_results == []
        assert state.modified_files == []
        assert state.review is not None
        assert state.review.reviewer is ExecutorKind.CODEX_REVIEW
        assert state.review.verdict is ReviewVerdict.REJECTED
        assert [item.code for item in state.review.findings] == [
            "codex.p1.command.injection"
        ]
        assert state.inspected_files == ["src/security_runner.py"]
        assert len(state.attempts) == 1
        assert state.attempts[0].executor is ExecutorKind.CODEX_REVIEW
        assert state.attempts[0].status is AttemptStatus.PASSED
        assert qwen.calls == []
        assert len(codex.calls) == 1
        assert semantic.calls == []
        assert codex.calls[0].get("review_only") is True
        assert file_snapshot(fixture.repository) == source_before
        assert file_snapshot(worktree) == source_before
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip() == (
            fixture.baseline_sha
        )
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"],
                cwd=worktree,
            ).stdout.strip()
            == "0"
        )
        assert (
            fixture.git(
                [
                    "--git-dir",
                    str(fixture.remote),
                    "for-each-ref",
                    "--format=%(refname)%09%(objectname)",
                    "refs",
                ],
                cwd=fixture.root,
            ).stdout
            == remote_before
        )
        assert manager.load(request.task_id).status == "complete"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_high_risk_read_only_codex_review_clean_commit_is_blocked():
    with coding_fixture(run_id="engine-codex-review-clean-commit") as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = fixture.git(
            [
                "--git-dir",
                str(fixture.remote),
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs",
            ],
            cwd=fixture.root,
        ).stdout

        def mutating_review(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert kwargs.get("review_only") is True
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "security_runner.py"
            target.write_text(
                target.read_text(encoding="utf-8")
                + "\n# forbidden read-only review mutation\n",
                encoding="utf-8",
                newline="\n",
            )
            fixture.git(["add", "src/security_runner.py"], cwd=repository)
            fixture.git(
                ["commit", "-m", "forbidden read-only reviewer commit"],
                cwd=repository,
            )
            assert git_status_paths(repository) == []
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="NO_FINDINGS",
                inspected_files=("src/security_runner.py",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [mutating_review])
        engine, store, manager, _ = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="high-risk-read-only-clean-commit",
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            rule_scope_paths=["src/security_runner.py"],
            cloud_execution=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.BLOCKED
        assert result.status is not CodingTaskStatus.COMPLETED
        assert result.review_verdict is ReviewVerdict.REJECTED
        assert result.commit_sha is None
        assert state is not None
        assert state.status is CodingTaskStatus.BLOCKED
        assert state.review is not None
        finding_codes = {item.code for item in state.review.findings}
        assert "executor.commit_forbidden" in finding_codes
        assert "codex.review_mutated_worktree" in finding_codes
        assert state.command_results == []
        assert state.modified_files == []
        assert len(state.attempts) == 1
        assert state.attempts[0].executor is ExecutorKind.CODEX_REVIEW
        assert state.attempts[0].status is AttemptStatus.FAILED
        assert qwen.calls == []
        assert len(codex.calls) == 1
        assert codex.calls[0].get("review_only") is True
        assert file_snapshot(fixture.repository) == source_before
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert (
            fixture.git(
                [
                    "--git-dir",
                    str(fixture.remote),
                    "for-each-ref",
                    "--format=%(refname)%09%(objectname)",
                    "refs",
                ],
                cwd=fixture.root,
            ).stdout
            == remote_before
        )
        assert fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip() != (
            fixture.baseline_sha
        )
        assert git_status_paths(worktree) == []
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_writable_final_codex_review_ignored_artifact_mutation_is_blocked():
    with coding_fixture(run_id="engine-final-review-ignored-mutation") as fixture:

        def codex_fix(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "security_runner.py"
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    'f"fixture-report --name {report_name}",\n        shell=True,\n',
                    '["fixture-report", "--name", report_name],\n',
                ),
                encoding="utf-8",
                newline="\n",
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="Removed shell interpretation from the report command.",
                inspected_files=("src/security_runner.py", "tests/security_check.py"),
            )

        def mutating_final_review(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            assert kwargs.get("review_only") is True
            repository = Path(str(kwargs["repository"]))
            ignored = repository / "artifacts"
            ignored.mkdir(exist_ok=True)
            (ignored / "reviewer-cache.txt").write_text(
                "forbidden ignored review mutation\n",
                encoding="utf-8",
                newline="\n",
            )
            assert git_status_paths(repository) == ["src/security_runner.py"]
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="NO_FINDINGS",
            )

        codex = _FakeExecutor(
            ExecutorKind.CODEX_EXEC,
            [codex_fix, mutating_final_review],
        )
        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, []),
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="final-review-ignored-mutation-task",
            risk=CodingRisk.HIGH,
            expected_paths=["src/security_runner.py"],
            cloud_execution=True,
            verification_commands=[
                VerificationCommandV1(
                    argv=[
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.security_check",
                        "-v",
                    ],
                    purpose="Run the command-injection regression test.",
                    timeout_seconds=60,
                )
            ],
        )

        with pytest.raises(
            CodingEngineError,
            match="ignored files appeared.*independent reviewer",
        ):
            engine.run(request)
        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        worktree = Path(state.worktree.worktree_path)

        assert state.status is CodingTaskStatus.FAILED
        assert state.review is None
        assert state.commit_sha is None
        assert (worktree / "artifacts" / "reviewer-cache.txt").is_file()
        assert git_status_paths(worktree) == ["src/security_runner.py"]
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


def test_failed_verifier_evidence_is_bounded_into_the_next_executor_prompt(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-verifier-feedback") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right",
                    b"return left + right",
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected the calculator using bounded verifier evidence.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [fix_calculator, fix_calculator],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)

        def verification_with_one_failure(**kwargs: object):
            attempt_index = kwargs["attempt_index"]
            if attempt_index != 1:
                return _synthetic_passed_verification(**kwargs)
            artifacts = kwargs["artifacts"]
            repository = Path(str(kwargs["repository"]))
            assert isinstance(artifacts, ArtifactStore)
            command_id = "a1-verify-synthetic"
            output = artifacts.write_text(
                kind=ArtifactKind.COMMAND_OUTPUT,
                text="AssertionError: calculator returned 3; expected 4",
                producer="synthetic-verifier",
                occurrence_id=command_id,
            )
            now = datetime.now(timezone.utc)
            result = CommandResultV1(
                command_id=command_id,
                argv=[sys.executable, "-m", "unittest", "tests.test_calculator"],
                cwd=str(repository),
                purpose="Exercise the calculator regression.",
                status=CommandStatus.FAILED,
                exit_code=1,
                started_at=now,
                finished_at=now,
                duration_ms=1,
                output_artifact_id=output.artifact_id,
                summary="Synthetic calculator regression failed.",
            )
            return [result], {command_id}, [output]

        monkeypatch.setattr(engine, "_verification", verification_with_one_failure)
        request = _request(
            fixture.repository,
            task_id="verifier-feedback-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
        )

        result = engine.run(request)

        assert result.status is CodingTaskStatus.COMPLETED
        assert len(qwen.calls) == 2
        retry_prompt = str(qwen.calls[1]["prompt"])
        assert "a1-verify-synthetic" in retry_prompt
        assert "tests.test_calculator" in retry_prompt
        assert "calculator returned 3; expected 4" in retry_prompt
        assert "untrusted evidence" in retry_prompt
        state = store.load(request.task_id)
        assert state is not None
        assert state.attempts[0].error_summary is not None
        assert "calculator returned 3; expected 4" in state.attempts[0].error_summary
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_engine_edit_runs_tests_preserves_source_and_creates_exactly_one_local_commit():
    with coding_fixture(run_id="engine-local-commit") as fixture:
        source_before = file_snapshot(fixture.repository)

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected calculator and left commit ownership to the engine.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="local-commit-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.COMPLETED
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert result.commit_sha is not None
        assert result.modified_files == ["src/calculator.py"]
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"], cwd=worktree
            ).stdout.strip()
            == "1"
        )
        assert (
            fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
            == result.commit_sha
        )
        assert fixture.git(
            ["log", "-1", "--pretty=%s"], cwd=worktree
        ).stdout.strip() == ("Fix synthetic calculator")
        assert git_status_paths(worktree) == []
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        diff_artifacts = [
            item for item in state.artifacts if item.kind is ArtifactKind.DIFF
        ]
        assert len(diff_artifacts) == 1
        assert b"return left + right" in Path(diff_artifacts[0].path).read_bytes()
        assert len(qwen.calls) == 1
        fixture.assert_remote_unchanged()


def test_false_approved_review_gate_never_advances_owned_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-false-approved-review-gate") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared a change before the forged review gate.",
            )

        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator]),
        )
        monkeypatch.setattr(engine, "_verification", _synthetic_passed_verification)
        invalid_approval = ReviewResultV1(
            reviewer_id="invalid-approved-review",
            reviewer=ExecutorKind.DETERMINISTIC,
            verdict=ReviewVerdict.APPROVED,
            findings=[],
            checked_requirements=True,
            checked_tests=False,
            checked_diff_scope=True,
            checked_secrets=True,
            checked_constitution=True,
            summary="An invalid approval with an incomplete delivery gate.",
            reviewed_at=datetime.now(timezone.utc),
        )
        monkeypatch.setattr(
            engine,
            "_review",
            lambda **_kwargs: (invalid_approval, None, ()),
        )
        request = _request(
            fixture.repository,
            task_id="false-approved-review-gate-task",
            expected_paths=["src/calculator.py"],
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="review delivery is not complete"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )


def test_tampered_local_semantic_evidence_cannot_reach_commit_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-precommit-semantic-evidence-tamper") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared a valid change before evidence tampering.",
            )

        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator]),
        )
        monkeypatch.setattr(engine, "_verification", _synthetic_passed_verification)
        original_transition = engine._transition

        def tamper_after_passed(
            state,
            version,
            status,
            event_type,
            *,
            reason_code=None,
            **updates,
        ):
            transitioned, next_version = original_transition(
                state,
                version,
                status,
                event_type,
                reason_code=reason_code,
                **updates,
            )
            if event_type == "attempt.passed":
                review = transitioned.review
                assert review is not None and review.evidence_artifact_id is not None
                evidence = next(
                    item
                    for item in transitioned.artifacts
                    if item.artifact_id == review.evidence_artifact_id
                )
                path = Path(evidence.path)
                path.write_bytes(path.read_bytes() + b"\nsubstituted")
            return transitioned, next_version

        monkeypatch.setattr(engine, "_transition", tamper_after_passed)
        request = _request(
            fixture.repository,
            task_id="precommit-semantic-evidence-tamper-task",
            expected_paths=["src/calculator.py"],
            local_commit=True,
        )

        with pytest.raises(
            CodingEngineError,
            match="semantic review evidence could not be re-authenticated",
        ):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )


def test_deleted_local_semantic_evidence_after_commit_blocks_completion(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-postcommit-semantic-evidence-delete") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared a valid change before evidence deletion.",
            )

        engine, store, manager, _ = _engine(
            fixture,
            qwen=_FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator]),
        )
        monkeypatch.setattr(engine, "_verification", _synthetic_passed_verification)
        original_terminal_gate = engine._assert_terminal_commit_gate

        def validate_then_delete_evidence(**kwargs):
            original_terminal_gate(**kwargs)
            state = kwargs["state"]
            assert state.review is not None
            evidence = next(
                item
                for item in state.artifacts
                if item.artifact_id == state.review.evidence_artifact_id
            )
            Path(evidence.path).unlink()

        monkeypatch.setattr(
            engine,
            "_assert_terminal_commit_gate",
            validate_then_delete_evidence,
        )
        request = _request(
            fixture.repository,
            task_id="postcommit-semantic-evidence-delete-task",
            expected_paths=["src/calculator.py"],
            local_commit=True,
        )

        with pytest.raises(
            CodingEngineError,
            match="semantic review evidence could not be re-authenticated",
        ):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-list", "--count", "HEAD", f"^{fixture.baseline_sha}", "--"],
                cwd=Path(state.worktree.worktree_path),
            ).stdout.strip()
            == "1"
        )


def test_git_metadata_change_between_review_and_commit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-precommit-git-metadata") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected calculator before the injected Git metadata race.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="precommit-git-metadata-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )
        original_commit = engine._commit

        def mutate_then_commit(state, repository, source, approved_binding):
            fixture.git(
                [
                    "remote",
                    "set-url",
                    "origin",
                    str(fixture.root / "unexpected-remote.git"),
                ]
            )
            return original_commit(state, repository, source, approved_binding)

        monkeypatch.setattr(engine, "_commit", mutate_then_commit)

        with pytest.raises(CodingEngineError, match="protected Git metadata"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None
        assert state.status is CodingTaskStatus.FAILED
        assert (
            state.review is not None and state.review.verdict is ReviewVerdict.APPROVED
        )
        assert state.commit_sha is None
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"],
                cwd=Path(state.worktree.worktree_path),
            ).stdout.strip()
            == "0"
        )
        fixture.assert_remote_unchanged()


def test_delayed_worktree_writer_between_review_and_staging_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-prestage-worktree-race") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected calculator before an injected delayed writer.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="prestage-worktree-race-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )
        original_commit = engine._commit

        def mutate_then_commit(state, repository, source, approved_binding):
            target = repository / "src" / "calculator.py"
            target.write_bytes(target.read_bytes() + b"\n# DELAYED_WRITER\n")
            return original_commit(state, repository, source, approved_binding)

        monkeypatch.setattr(engine, "_commit", mutate_then_commit)

        with pytest.raises(CodingEngineError, match="changed before staging"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"],
                cwd=Path(state.worktree.worktree_path),
            ).stdout.strip()
            == "0"
        )
        fixture.assert_remote_unchanged()


def test_delayed_worktree_writer_after_commit_blocks_terminal_completion(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-postcommit-worktree-race") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected calculator before a post-commit writer race.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="postcommit-worktree-race-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )
        original_commit = engine._commit

        def commit_then_mutate(state, repository, source, approved_binding):
            gate = original_commit(state, repository, source, approved_binding)
            target = repository / "src" / "calculator.py"
            target.write_bytes(target.read_bytes() + b"\n# POST_COMMIT_WRITER\n")
            return gate

        monkeypatch.setattr(engine, "_commit", commit_then_mutate)

        with pytest.raises(CodingEngineError, match="before terminal completion"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert state.worktree is not None and state.worktree.status == "orphaned"
        worktree = Path(state.worktree.worktree_path)
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"],
                cwd=worktree,
            ).stdout.strip()
            == "1"
        )
        assert git_status_paths(worktree) == ["src/calculator.py"]
        fixture.assert_remote_unchanged()


def test_index_mutation_after_staging_binding_cannot_reach_commit_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-index-race-before-commit-tree") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared an approved change before an index race.",
                inspected_files=("src/calculator.py",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        original_create = engine._create_guarded_commit

        def reset_index_then_create(**kwargs):
            repository = Path(kwargs["repository"])
            fixture.git(
                ["reset", "HEAD", "--", "src/calculator.py"],
                cwd=repository,
            )
            return original_create(**kwargs)

        monkeypatch.setattr(engine, "_create_guarded_commit", reset_index_then_create)
        request = _request(
            fixture.repository,
            task_id="index-race-before-commit-tree-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="staged index changed"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )


def test_index_swap_between_staged_diff_and_tree_never_advances_owned_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-index-split-observation-race") as fixture:

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared the independently approved calculator change.",
                inspected_files=("src/calculator.py",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        original_staged_diff = engine._staged_diff
        approved_staged: list[bytes] = []

        def replay_approved_staged_diff(repository: Path) -> bytes:
            if not approved_staged:
                approved_staged.append(original_staged_diff(repository))
            return approved_staged[0]

        original_run_git = coding_engine.run_git
        swapped = False

        def swap_index_before_first_tree(
            repository: Path, arguments: list[str], **kwargs
        ):
            nonlocal swapped
            if arguments == ["write-tree"] and not swapped:
                target = repository / "src" / "calculator.py"
                target.write_bytes(
                    target.read_bytes().replace(
                        b"return left + right", b"return left * right"
                    )
                )
                fixture.git(["add", "src/calculator.py"], cwd=repository)
                swapped = True
            return original_run_git(repository, arguments, **kwargs)

        monkeypatch.setattr(engine, "_staged_diff", replay_approved_staged_diff)
        monkeypatch.setattr(coding_engine, "run_git", swap_index_before_first_tree)
        request = _request(
            fixture.repository,
            task_id="index-split-observation-race-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="unreachable commit differs"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )


def test_head_ref_mutation_after_staging_cannot_advance_any_branch(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-head-race-before-commit-tree") as fixture:
        fixture.git(["branch", "user-race-target", fixture.baseline_sha])

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Prepared an approved change before a HEAD race.",
                inspected_files=("src/calculator.py",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        original_create = engine._create_guarded_commit

        def switch_head_then_create(**kwargs):
            repository = Path(kwargs["repository"])
            fixture.git(
                ["symbolic-ref", "HEAD", "refs/heads/user-race-target"],
                cwd=repository,
            )
            return original_create(**kwargs)

        monkeypatch.setattr(engine, "_create_guarded_commit", switch_head_then_create)
        request = _request(
            fixture.repository,
            task_id="head-race-before-commit-tree-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="Git metadata scope changed"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )
        assert (
            fixture.git(["rev-parse", "refs/heads/user-race-target"]).stdout.strip()
            == fixture.baseline_sha
        )


def test_engine_preserves_exact_binary_bytes_in_owned_worktree():
    desired = b"QWEN_CODE_OK"
    with coding_fixture(run_id="engine-exact-bytes") as fixture:
        source_before = (fixture.repository / "exact.bin").read_bytes()

        def write_exact(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            (repository / "exact.bin").write_bytes(desired)
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Wrote the exact requested binary payload.",
                inspected_files=("exact.bin",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [write_exact])
        engine, _, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="exact-bytes-task",
            expected_paths=["exact.bin"],
            verification_commands=[
                VerificationCommandV1(
                    argv=[
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.exact_check",
                        "-v",
                    ],
                    purpose="Verify the exact byte-level fixture contract.",
                    timeout_seconds=60,
                )
            ],
        )

        result = engine.run(request)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.COMPLETED
        assert result.commit_sha is None
        assert result.modified_files == ["exact.bin"]
        assert (worktree / "exact.bin").read_bytes() == desired
        assert (fixture.repository / "exact.bin").read_bytes() == source_before
        assert git_status_paths(worktree) == ["exact.bin"]
        fixture.assert_remote_unchanged()


def test_retry_review_uses_current_passed_semantic_evidence_not_prior_failure():
    with coding_fixture(run_id="engine-retry-current-evidence") as fixture:

        def leave_bug(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="First hypothesis did not correct the calculator.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        def fix_bug(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Second hypothesis corrected the calculator.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [leave_bug, fix_bug])
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="retry-current-evidence-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.COMPLETED
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert state is not None and state.review is not None
        assert [attempt.status for attempt in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert len(state.command_results) == 4
        assert state.command_results[0].status is CommandStatus.FAILED
        assert all(
            result.status is CommandStatus.PASSED
            for result in state.command_results[2:]
        )
        assert state.attempts[-1].command_ids == [
            "a2-verify-1",
            "a2-verify-2",
        ]
        fixture.assert_remote_unchanged()


def test_retry_cannot_reuse_old_semantic_pass_for_current_structural_only_evidence(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-retry-stale-evidence") as fixture:
        original_readme = (fixture.repository / "README.md").read_bytes()

        def broad_first_change(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            calculator = repository / "src" / "calculator.py"
            calculator.write_bytes(
                calculator.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            (repository / "README.md").write_bytes(
                original_readme + b"\nUnexpected first-attempt edit.\n"
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="First attempt changed a file outside the declared scope.",
            )

        def narrow_second_change(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            (repository / "README.md").write_bytes(original_readme)
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Second attempt narrowed the diff to the requested file.",
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [broad_first_change, narrow_second_change],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        original_verification = engine._verification

        def attempt_scoped_verification(**kwargs: object):
            results, required, artifacts = original_verification(**kwargs)  # type: ignore[arg-type]
            if kwargs["attempt_index"] == 1:
                return results, required, artifacts
            structural = results[-1]
            structural_artifacts = [
                item
                for item in artifacts
                if item.artifact_id == structural.output_artifact_id
            ]
            return [structural], {structural.command_id}, structural_artifacts

        monkeypatch.setattr(engine, "_verification", attempt_scoped_verification)
        request = _request(
            fixture.repository,
            task_id="retry-stale-evidence-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert state is not None and state.review is not None
        assert "requirements.evidence_missing" in {
            finding.code for finding in state.review.findings
        }
        assert [attempt.status for attempt in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
        ]
        assert state.attempts[-1].command_ids == ["a2-verify-2"]
        assert [item.command_id for item in state.command_results] == [
            "a1-verify-1",
            "a1-verify-2",
            "a2-verify-2",
        ]
        retry_prompt = str(qwen.calls[1]["prompt"])
        assert "diff.unexpected_file [README.md]" in retry_prompt
        assert "Revert this file" in retry_prompt
        assert "Declared writable path scope" in retry_prompt
        assert "src/calculator.py" in retry_prompt
        fixture.assert_remote_unchanged()


def test_executor_commit_is_rejected_and_engine_never_adds_a_second_commit():
    with coding_fixture(run_id="engine-forbidden-commit") as fixture:

        def commit_directly(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            if ordinal == 1:
                target = repository / "src" / "calculator.py"
                target.write_bytes(
                    target.read_bytes().replace(
                        b"return left - right", b"return left + right"
                    )
                )
                fixture.git(["add", "src/calculator.py"], cwd=repository)
                fixture.git(
                    ["commit", "-m", "forbidden executor commit"], cwd=repository
                )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Executor improperly changed HEAD.",
            )

        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [commit_directly, commit_directly],
        )
        engine, store, _, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="forbidden-commit-task",
            expected_paths=["src/calculator.py"],
            local_commit=True,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert result.commit_sha is None
        assert state is not None and state.review is not None
        assert state.review.verdict is ReviewVerdict.REJECTED
        assert "executor.commit_forbidden" in {
            finding.code for finding in state.review.findings
        }
        assert len(state.attempts) == 2
        assert all(item.status is AttemptStatus.FAILED for item in state.attempts)
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"], cwd=worktree
            ).stdout.strip()
            == "1"
        )
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert len(qwen.calls) == 2
        fixture.assert_remote_unchanged()


def test_replaced_linked_worktree_git_pointer_is_rejected_before_engine_git_mutation():
    with coding_fixture(run_id="engine-tampered-worktree-git-pointer") as fixture:
        original_marker: bytes | None = None

        def replace_git_pointer(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            nonlocal original_marker
            repository = Path(str(kwargs["repository"]))
            marker = repository / ".git"
            original_marker = marker.read_bytes()
            os.chmod(marker, stat.S_IWRITE)
            marker.unlink()
            marker.write_text(
                f"gitdir: {fixture.repository / '.git'}\n",
                encoding="utf-8",
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Attempted to redirect trusted Git metadata.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [replace_git_pointer])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="tampered-worktree-git-pointer-task",
            expected_paths=["src/calculator.py"],
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="Git metadata scope changed"):
            engine.run(request)

        state = store.load(request.task_id)
        assert original_marker is not None
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        fixture.assert_remote_unchanged()


def test_well_formed_standalone_git_replacement_is_not_the_registered_metadata_graph():
    with coding_fixture(run_id="engine-standalone-git-replacement") as fixture:

        def replace_with_standalone_git(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            branch = fixture.git(
                ["symbolic-ref", "--short", "HEAD"], cwd=repository
            ).stdout.strip()
            marker = repository / ".git"
            os.chmod(marker, stat.S_IWRITE)
            marker.unlink()
            fixture.git(["init", "-b", branch], cwd=repository)
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Replaced the linked metadata with a standalone repository.",
                inspected_files=("README.md",),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [replace_with_standalone_git])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="standalone-git-replacement-task",
            local_commit=True,
        )

        with pytest.raises(CodingEngineError, match="registered identity"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.worktree is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.commit_sha is None
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert (
            fixture.git(
                ["rev-parse", f"refs/heads/{state.worktree.branch}"]
            ).stdout.strip()
            == fixture.baseline_sha
        )
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_two_local_failures_create_complete_handoff_then_resume_same_worktree_with_codex():
    with coding_fixture(run_id="engine-handoff-resume") as fixture:
        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [
                _failure("local hypothesis one failed"),
                _failure("local hypothesis two failed"),
            ],
        )

        def codex_fix(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert kwargs.get("review_only") is not True
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="Codex resumed the preserved worktree and corrected the calculator.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        def codex_review(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert kwargs.get("review_only") is True
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="NO_FINDINGS",
            )

        codex = _FakeExecutor(
            ExecutorKind.CODEX_EXEC,
            [codex_fix, codex_review],
        )
        engine, store, manager, context = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="handoff-resume-task",
            expected_paths=["src/calculator.py"],
            verify_calculator=True,
            cloud_execution=True,
        )

        handoff_result = engine.run(request)
        handoff_state = store.load(request.task_id)

        assert handoff_result.status is CodingTaskStatus.HANDOFF_READY
        assert handoff_result.handoff_path is not None
        assert handoff_state is not None
        assert len(handoff_state.attempts) == 2
        assert [item.status for item in handoff_state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
        ]
        contract = CodingHandoffV1.model_validate_json(
            Path(handoff_result.handoff_path).read_bytes()
        )
        assert contract.task_id == request.task_id
        assert contract.request_id == request.request_id
        assert contract.worktree_path == handoff_result.worktree_path
        assert contract.branch == handoff_result.branch
        assert contract.source_base_commit == fixture.baseline_sha
        assert len(contract.attempts) == 2
        assert contract.unresolved_questions == [
            "local hypothesis one failed",
            "local hypothesis two failed",
        ]
        assert contract.verification_plan == request.verification_plan
        assert len(contract.applicable_rules) == 2
        assert all(Path(item.path).is_file() for item in contract.artifacts)
        assert len(contract.resume_anchor_sha256) == 64
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]

        resumed = engine.resume(request.task_id)
        final_state = store.load(request.task_id)

        assert resumed.status is CodingTaskStatus.COMPLETED
        assert resumed.worktree_path == handoff_result.worktree_path
        assert resumed.branch == handoff_result.branch
        assert resumed.modified_files == ["src/calculator.py"]
        assert resumed.verification_passed is True
        assert resumed.review_verdict is ReviewVerdict.APPROVED
        assert final_state is not None
        assert len(final_state.attempts) == 3
        assert final_state.attempts[-1].status is AttemptStatus.PASSED
        assert final_state.review is not None
        assert final_state.review.reviewer is ExecutorKind.CODEX_REVIEW
        assert len(qwen.calls) == 2
        assert len(codex.calls) == 2
        assert codex.calls[0].get("review_only") is not True
        assert codex.calls[1].get("review_only") is True
        assert len(context.calls) == 2
        assert context.calls[1]["repository"] == handoff_result.worktree_path
        assert context.calls[1]["unresolved_errors"] == tuple(
            contract.unresolved_questions
        )
        assert manager.load(request.task_id).status == "complete"  # type: ignore[union-attr]
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        fixture.assert_remote_unchanged()


def test_resume_validates_under_lease_and_rechecks_after_context_before_executor(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-handoff-lease-recheck") as fixture:
        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [
                _failure("local attempt one failed"),
                _failure("local attempt two failed"),
            ],
        )
        codex = _FakeExecutor(ExecutorKind.CODEX_EXEC, [])
        engine, store, manager, context = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="handoff-lease-recheck-task",
            expected_paths=["src/calculator.py"],
            cloud_execution=True,
        )
        handoff = engine.run(request)
        assert handoff.status is CodingTaskStatus.HANDOFF_READY

        lease_observations: list[bool] = []
        original_validate = HandoffManager.load_and_validate

        def observe_lease(self, state, artifact):
            lease_observations.append(any(manager.leases_dir.glob("*.lease.json")))
            return original_validate(self, state, artifact)

        monkeypatch.setattr(HandoffManager, "load_and_validate", observe_lease)
        original_build = context.build

        def build_then_tamper(**kwargs):
            built = original_build(**kwargs)
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(target.read_bytes() + b"\n# POST_HANDOFF_TAMPER\n")
            return built

        monkeypatch.setattr(context, "build", build_then_tamper)

        with pytest.raises(
            HandoffPolicyError,
            match="worktree (status|diff|fingerprint)",
        ):
            engine.resume(request.task_id)

        resumed_state = store.load(request.task_id)
        assert lease_observations == [True, True]
        assert len(codex.calls) == 0
        assert resumed_state is not None
        assert resumed_state.status is CodingTaskStatus.HANDOFF_READY
        assert resumed_state.attempts[-1].status is AttemptStatus.FAILED
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_cancelled_executor_preserves_owned_worktree_and_cancellation_evidence():
    with coding_fixture(run_id="engine-cancel") as fixture:
        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [_failure("cancelled by caller")],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="cancelled-task",
            expected_paths=["src/calculator.py"],
        )
        cancel = threading.Event()
        cancel.set()

        result = engine.run(request, cancel_event=cancel)
        state = store.load(request.task_id)

        assert result.status is CodingTaskStatus.CANCELLED
        assert result.verification_passed is False
        assert state is not None
        assert len(state.attempts) == 1
        assert state.attempts[0].status is AttemptStatus.CANCELLED
        assert state.attempts[0].error_summary == "cancelled by caller"
        assert state.attempts[0].artifact_ids
        assert Path(result.worktree_path or "").is_dir()
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.worktree(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert git_status_paths(Path(result.worktree_path or "")) == []
        assert len(qwen.calls) == 1
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_cancelled_cloud_review_preserves_verified_high_risk_codex_write_as_orphan():
    with coding_fixture(run_id="engine-cancel-cloud-review") as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = fixture.git(
            [
                "--git-dir",
                str(fixture.remote),
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
                "refs",
            ],
            cwd=fixture.root,
        ).stdout
        cancel = threading.Event()

        def codex_fix(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            assert kwargs.get("review_only") is not True
            assert kwargs.get("cancel_event") is cancel
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "security_runner.py"
            unsafe = 'f"fixture-report --name {report_name}",\n        shell=True,\n'
            source = target.read_text(encoding="utf-8")
            assert unsafe in source
            target.write_text(
                source.replace(
                    unsafe,
                    '["fixture-report", "--name", report_name],\n',
                ),
                encoding="utf-8",
                newline="\n",
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.CODEX_EXEC,
                summary="Removed shell interpretation from the report command.",
                inspected_files=(
                    "src/security_runner.py",
                    "tests/security_check.py",
                ),
            )

        def cancel_final_review(
            kwargs: dict[str, object], ordinal: int
        ) -> ExecutorResult:
            assert kwargs.get("review_only") is True
            assert kwargs.get("cancel_event") is cancel
            artifacts = kwargs["artifact_store"]
            assert isinstance(artifacts, ArtifactStore)
            output = artifacts.write_text(
                kind=ArtifactKind.COMMAND_OUTPUT,
                text="synthetic caller cancellation during final Codex review",
                producer="fake-executor",
                redact=True,
            )
            cancel.set()
            raise ExecutorFailure(
                "cancelled by caller during independent Codex review",
                output_artifact=output,
                session_id=f"fake-cancelled-review-{ordinal}",
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [])
        codex = _FakeExecutor(
            ExecutorKind.CODEX_EXEC,
            [codex_fix, cancel_final_review],
        )
        engine, store, manager, _ = _engine(
            fixture,
            qwen=qwen,
            codex=codex,
        )
        request = _request(
            fixture.repository,
            task_id="cancelled-cloud-review-task",
            risk=CodingRisk.HIGH,
            expected_paths=["src/security_runner.py"],
            cloud_execution=True,
        ).model_copy(
            update={
                "verification_commands": [
                    VerificationCommandV1(
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.security_check",
                            "-v",
                        ],
                        purpose="Run the synthetic command-injection regression test.",
                        timeout_seconds=60,
                    )
                ]
            }
        )

        result = engine.run(request, cancel_event=cancel)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "")

        assert result.status is CodingTaskStatus.CANCELLED
        assert result.commit_sha is None
        assert result.verification_passed is False
        assert state is not None
        assert state.status is CodingTaskStatus.CANCELLED
        assert state.commit_sha is None
        assert state.modified_files == ["src/security_runner.py"]
        assert len(state.command_results) == 2
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert state.attempts[-1].status is AttemptStatus.CANCELLED
        assert state.attempts[-1].error_summary == (
            "cancelled by caller during independent Codex review"
        )
        assert state.attempts[-1].modified_files == ["src/security_runner.py"]
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.worktree(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert len(codex.calls) == 2
        assert codex.calls[0].get("review_only") is not True
        assert codex.calls[1].get("review_only") is True
        assert qwen.calls == []
        assert worktree.is_dir()
        assert git_status_paths(worktree) == ["src/security_runner.py"]
        fixed = (worktree / "src" / "security_runner.py").read_text(encoding="utf-8")
        assert '["fixture-report", "--name", report_name]' in fixed
        assert "shell=True" not in fixed
        assert fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip() == (
            fixture.baseline_sha
        )
        assert (
            fixture.git(
                ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD"],
                cwd=worktree,
            ).stdout.strip()
            == "0"
        )
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert file_snapshot(fixture.repository) == source_before
        assert (
            fixture.git(
                [
                    "--git-dir",
                    str(fixture.remote),
                    "for-each-ref",
                    "--format=%(refname)%09%(objectname)",
                    "refs",
                ],
                cwd=fixture.root,
            ).stdout
            == remote_before
        )
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_unexpected_engine_failure_is_durable_and_preserves_owned_worktree():
    with coding_fixture(run_id="engine-unexpected-failure") as fixture:

        def crash(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            raise RuntimeError("synthetic adapter crash")

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [crash])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="unexpected-failure-task",
            expected_paths=["src/calculator.py"],
        )

        with pytest.raises(RuntimeError, match="synthetic adapter crash"):
            engine.run(request)

        state = store.load(request.task_id)
        assert state is not None and state.status is CodingTaskStatus.FAILED
        assert state.worktree is not None and state.worktree.status == "orphaned"
        assert manager.load(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert store.worktree(request.task_id).status == "orphaned"  # type: ignore[union-attr]
        assert "synthetic adapter crash" in state.unresolved_errors[-1]
        assert Path(state.worktree.worktree_path).is_dir()
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_post_complete_store_failure_durably_orphans_successful_write_flow(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="engine-post-complete-store-failure") as fixture:
        source_before = file_snapshot(fixture.repository)

        def fix_calculator(kwargs: dict[str, object], ordinal: int) -> ExecutorResult:
            repository = Path(str(kwargs["repository"]))
            target = repository / "src" / "calculator.py"
            target.write_bytes(
                target.read_bytes().replace(
                    b"return left - right", b"return left + right"
                )
            )
            return _success(
                kwargs,
                ordinal,
                executor=ExecutorKind.LOCAL_QWEN,
                summary="Corrected the calculator before the injected persistence fault.",
                inspected_files=("src/calculator.py", "tests/test_calculator.py"),
            )

        qwen = _FakeExecutor(ExecutorKind.LOCAL_QWEN, [fix_calculator])
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="post-complete-store-failure-task",
            expected_paths=["src/calculator.py"],
        ).model_copy(
            update={
                "verification_commands": [
                    VerificationCommandV1(
                        argv=[
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.test_calculator",
                            "-v",
                        ],
                        purpose="Run the focused calculator regression test.",
                        timeout_seconds=60,
                    )
                ]
            }
        )
        original_update_worktree = store.update_worktree
        injected = False

        def fail_once_after_complete(record: WorktreeRecordV1) -> None:
            nonlocal injected
            if record.status == "complete" and not injected:
                injected = True
                registry_record = manager.load(record.task_id)
                assert registry_record is not None
                assert registry_record.status == "complete"
                raise RuntimeError("synthetic post-complete store failure")
            original_update_worktree(record)

        monkeypatch.setattr(store, "update_worktree", fail_once_after_complete)

        with pytest.raises(RuntimeError, match="synthetic post-complete store failure"):
            engine.run(request)

        state = store.load(request.task_id)
        registry_record = manager.load(request.task_id)
        durable_record = store.worktree(request.task_id)

        assert injected is True
        assert state is not None
        assert state.status is CodingTaskStatus.FAILED
        assert state.command_results
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert state.review is not None
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert state.worktree is not None
        assert state.worktree.status == "orphaned"
        assert registry_record is not None
        assert registry_record.status == "orphaned"
        assert registry_record.completed_at is not None
        assert durable_record is not None
        assert durable_record.status == "orphaned"
        assert durable_record.completed_at is not None
        assert "synthetic post-complete store failure" in state.unresolved_errors[-1]
        assert file_snapshot(fixture.repository) == source_before
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        assert git_status_paths(Path(state.worktree.worktree_path)) == [
            "src/calculator.py"
        ]
        assert len(qwen.calls) == 1
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_timed_out_attempts_are_preserved_in_resumable_handoff_evidence():
    with coding_fixture(run_id="engine-timeout-evidence") as fixture:
        qwen = _FakeExecutor(
            ExecutorKind.LOCAL_QWEN,
            [
                _failure("local executor timed out"),
                _failure("local executor timed out"),
            ],
        )
        engine, store, manager, _ = _engine(fixture, qwen=qwen)
        request = _request(
            fixture.repository,
            task_id="timed-out-task",
            expected_paths=["src/calculator.py"],
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        contract = CodingHandoffV1.model_validate_json(
            Path(result.handoff_path or "").read_bytes()
        )

        assert result.status is CodingTaskStatus.HANDOFF_READY
        assert result.verification_passed is False
        assert state is not None
        assert [item.status for item in state.attempts] == [
            AttemptStatus.TIMED_OUT,
            AttemptStatus.TIMED_OUT,
        ]
        assert [item.status for item in contract.attempts] == [
            AttemptStatus.TIMED_OUT,
            AttemptStatus.TIMED_OUT,
        ]
        assert all(
            item.error_summary == "local executor timed out"
            for item in contract.attempts
        )
        assert all(item.artifact_ids for item in contract.attempts)
        assert all(Path(item.path).is_file() for item in contract.artifacts)
        assert manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        assert Path(result.worktree_path or "").is_dir()
        fixture.assert_remote_unchanged()


@pytest.mark.required_e2e
def test_worktree_lease_serializes_concurrent_owners():
    with coding_fixture(run_id="lease-serialization") as fixture:
        manager = WorktreeManager(
            registry_root=fixture.root / "lease-registry",
            owned_worktree_root=fixture.root / "lease-worktrees",
        )
        record = manager.create(
            task_id="lease-task",
            repository=resolve_repository(str(fixture.repository)),
        )
        policy = _policy().model_copy(
            update={"process_poll_seconds": 0.02, "lease_acquire_timeout_seconds": 2}
        )
        lease_path = fixture.root / "shared.lease.json"
        first = WorktreeLease(
            lease_path=lease_path,
            canonical_worktree=Path(record.worktree_path),
            task_id=record.task_id,
            policy=policy,
            timeout_seconds=2,
        )
        second = WorktreeLease(
            lease_path=lease_path,
            canonical_worktree=Path(record.worktree_path),
            task_id=record.task_id,
            policy=policy,
            timeout_seconds=2,
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        order: list[str] = []
        errors: list[BaseException] = []

        def first_owner() -> None:
            try:
                with first:
                    order.append("first-enter")
                    first_entered.set()
                    assert release_first.wait(2)
                    order.append("first-exit")
            except BaseException as exc:  # test thread must report every failure
                errors.append(exc)

        def second_owner() -> None:
            try:
                assert first_entered.wait(2)
                with second:
                    order.append("second-enter")
                    second_entered.set()
            except BaseException as exc:  # test thread must report every failure
                errors.append(exc)

        first_thread = threading.Thread(target=first_owner)
        second_thread = threading.Thread(target=second_owner)
        first_thread.start()
        second_thread.start()
        assert first_entered.wait(2)
        time.sleep(0.15)
        assert second_entered.is_set() is False
        release_first.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

        assert errors == []
        assert first_thread.is_alive() is False
        assert second_thread.is_alive() is False
        assert order == ["first-enter", "first-exit", "second-enter"]
        assert lease_path.exists() is False
        manager.complete(record.task_id)
        manager.cleanup(record.task_id)


def test_stale_dead_owner_is_recorded_as_orphan_without_deleting_worktree():
    with coding_fixture(run_id="orphan-evidence") as fixture:
        manager = WorktreeManager(
            registry_root=fixture.root / "orphan-registry",
            owned_worktree_root=fixture.root / "orphan-worktrees",
        )
        identity = resolve_repository(str(fixture.repository))
        record = manager.create(task_id="orphan-task", repository=identity)
        stale = record.model_copy(
            update={
                "owner_pid": 2_147_483_647,
                "heartbeat_at": datetime.now(timezone.utc)
                - timedelta(seconds=manager.policy.lease_stale_seconds + 5),
            }
        )
        manager._write_record(stale)

        recovered = manager.recover_orphans()

        assert len(recovered) == 1
        assert recovered[0].task_id == record.task_id
        assert recovered[0].status == "orphaned"
        assert Path(record.worktree_path).is_dir()
        assert manager.load(record.task_id).status == "orphaned"  # type: ignore[union-attr]
        manager.complete(record.task_id)
        manager.cleanup(record.task_id)
