from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import shutil
import sys
import threading
import uuid
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator

import pytest

from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, load_coding_policy
from services.coding.context import CodingContextBuilder
from services.coding.contracts import (
    ArtifactKind,
    AttemptStatus,
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskResultV1,
    CodingTaskStateV1,
    CodingTaskStatus,
    CommandStatus,
    DataClassification,
    ExecutorKind,
    ReviewVerdict,
    VerificationCommandV1,
)
from services.coding.engine import CodingEngine
from services.coding.executors import (
    CodexExecutor,
    ExecutorFailure,
    ExecutorResult,
    QwenExecutor,
    resolve_codex_executable,
)
from services.coding.handoff import CodingHandoffV1
from services.coding.semantic_review import (
    LocalSemanticReviewConfig,
    _knowledge_projection,
)
from services.coding.store import CodingTaskStore
from services.coding.ui import UIVerificationRunner
from services.coding.worktrees import WorktreeManager
from services.knowledge.engine import KnowledgeEngine
from services.knowledge.store import KnowledgeStore
from services.memory.store import MemoryStore
from tests.coding_fixtures import CodingFixture, coding_fixture, file_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODEX_PUBLIC_APPROVAL = "I_APPROVE_CODEX_PUBLIC_SYNTHETIC_FIXTURE"
EXPECTED_FACT = "CODING_FIXTURE_FACT=violet-otter-731"
EXPECTED_QWEN_BYTES = b"QWEN_CODE_OK"

RUN_LIVE_CODING = os.environ.get("LOCAL_AGENT_RUN_LIVE_CODING") == "1"
RUN_LIVE_CODEX = (
    RUN_LIVE_CODING
    and os.environ.get("LOCAL_AGENT_RUN_LIVE_CODEX") == "1"
    and os.environ.get("LOCAL_AGENT_CODEX_PUBLIC_FIXTURE_APPROVAL")
    == CODEX_PUBLIC_APPROVAL
)

live_coding = pytest.mark.skipif(
    not RUN_LIVE_CODING,
    reason="set LOCAL_AGENT_RUN_LIVE_CODING=1 or use scripts/coding-e2e.ps1",
)
live_codex = pytest.mark.skipif(
    not RUN_LIVE_CODEX,
    reason=(
        "Codex is disabled; use scripts/coding-e2e.ps1 -IncludeCodex for the "
        "explicit PUBLIC synthetic-fixture run"
    ),
)


class _ForbiddenExecutor:
    """Fail loudly if routing reaches an executor not authorized by the test."""

    def __init__(self, kind: ExecutorKind) -> None:
        self.kind = kind

    def execute(self, **_: object) -> ExecutorResult:
        raise AssertionError(f"unauthorized live executor route: {self.kind.value}")


class _RecordingExecutor:
    def __init__(self, delegate: object, kind: ExecutorKind) -> None:
        self.delegate = delegate
        self.kind = kind
        self.results: list[ExecutorResult] = []

    def execute(self, **kwargs: object) -> ExecutorResult:
        result = self.delegate.execute(**kwargs)  # type: ignore[attr-defined]
        self.results.append(result)
        return result


class _DeterministicFailingExecutor:
    """Produce two auditable local failures without spending a model invocation."""

    kind = ExecutorKind.LOCAL_QWEN

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.calls: list[dict[str, object]] = []

    def execute(self, **kwargs: object) -> ExecutorResult:
        ordinal = len(self.calls) + 1
        self.calls.append(dict(kwargs))
        if ordinal > len(self.messages):
            raise AssertionError("unexpected extra deterministic local attempt")
        artifacts = kwargs.get("artifact_store")
        assert isinstance(artifacts, ArtifactStore)
        output = artifacts.write_text(
            kind=ArtifactKind.COMMAND_OUTPUT,
            text=f"deterministic local failure {ordinal}: {self.messages[ordinal - 1]}",
            producer="required-e2e-local-failure",
            redact=True,
        )
        raise ExecutorFailure(
            self.messages[ordinal - 1],
            output_artifact=output,
            session_id=f"required-e2e-local-failure-{ordinal}",
        )


def _policy() -> CodingPolicy:
    return load_coding_policy(PROJECT_ROOT / "config" / "coding.json")


def _temp_parent() -> Path | None:
    raw = os.environ.get("LOCAL_AGENT_LIVE_TEMP_PARENT")
    if not raw:
        return None
    parent = Path(raw).expanduser().resolve(strict=True)
    try:
        parent.relative_to(PROJECT_ROOT.resolve(strict=True))
    except ValueError:
        return parent
    raise AssertionError("live fixture parent must be outside the product repository")


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _remote_refs(fixture: CodingFixture) -> dict[str, str]:
    completed = fixture.git(
        [
            "--git-dir",
            str(fixture.remote),
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            "refs",
        ],
        cwd=fixture.root,
    )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line:
            continue
        name, object_id = line.split("\t", 1)
        refs[name] = object_id
    return refs


def _assert_no_commit_or_push(
    fixture: CodingFixture,
    *,
    worktree: Path,
    remote_before: dict[str, str],
) -> None:
    assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
    assert (
        fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        == fixture.baseline_sha
    )
    assert (
        fixture.git(
            ["rev-list", "--count", f"{fixture.baseline_sha}..HEAD", "--"],
            cwd=worktree,
        ).stdout.strip()
        == "0"
    )
    assert _remote_refs(fixture) == remote_before
    fixture.assert_remote_unchanged()


def _emit_live_failure_diagnostics(
    result: CodingTaskResultV1,
    state: CodingTaskStateV1 | None,
) -> None:
    """Emit bounded synthetic-fixture metadata without raw model payloads."""

    if result.status is CodingTaskStatus.COMPLETED:
        return
    review = state.review if state is not None else None
    review_process_error: str | None = None
    if state is not None and review is not None and any(
        finding.code == "codex.review_failed" for finding in review.findings
    ):
        for artifact in state.artifacts:
            if (
                artifact.kind is ArtifactKind.COMMAND_OUTPUT
                and artifact.producer == ExecutorKind.CODEX_REVIEW.value
                and artifact.size_bytes <= 4_096
            ):
                try:
                    payload = Path(artifact.path).read_bytes()
                except OSError:
                    review_process_error = "[unreadable Codex review artifact]"
                    break
                if (
                    len(payload) == artifact.size_bytes
                    and hashlib.sha256(payload).hexdigest() == artifact.sha256
                ):
                    # This is a secret-scanned artifact from a synthetic PUBLIC
                    # fixture.  Keep only the bounded process error needed to
                    # diagnose CLI/protocol failures; successful model output is
                    # never emitted by this failure-only helper.
                    review_process_error = payload.decode(
                        "utf-8", errors="replace"
                    )[-4_096:]
                else:
                    review_process_error = "[invalid Codex review artifact]"
                break
    print(
        "LIVE_FAILURE_EVIDENCE "
        + json.dumps(
            {
                "task_id": result.task_id,
                "status": result.status.value,
                "attempts": [
                    {
                        "index": item.index,
                        "status": item.status.value,
                        "error": item.error_summary,
                    }
                    for item in (state.attempts if state is not None else [])
                ],
                "command_statuses": [
                    {"id": item.command_id, "status": item.status.value}
                    for item in (state.command_results if state is not None else [])
                ],
                "artifacts": [
                    {
                        "kind": item.kind.value,
                        "producer": item.producer,
                        "size_bytes": item.size_bytes,
                    }
                    for item in (state.artifacts if state is not None else [])
                ],
                "review": (
                    {
                        "reviewer": review.reviewer.value,
                        "verdict": review.verdict.value,
                        "checked_requirements": review.checked_requirements,
                        "checked_tests": review.checked_tests,
                        "checked_diff_scope": review.checked_diff_scope,
                        "checked_secrets": review.checked_secrets,
                        "checked_constitution": review.checked_constitution,
                        "findings": [
                            {
                                "severity": finding.severity.value,
                                "code": finding.code,
                                "failure_scenario": finding.failure_scenario,
                            }
                            for finding in review.findings
                        ],
                    }
                    if review is not None
                    else None
                ),
                "review_process_error": review_process_error,
                "unresolved_errors": state.unresolved_errors
                if state is not None
                else [],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _assert_attested_local_semantic_review(
    state: CodingTaskStateV1,
    *,
    request: CodingTaskRequestV1,
    fixture: CodingFixture,
) -> None:
    def assert_sha256(value: object) -> None:
        assert isinstance(value, str)
        assert len(value) == 64
        assert all(character in "0123456789abcdef" for character in value)

    review = state.review
    assert review is not None
    assert review.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
    assert review.verdict is ReviewVerdict.APPROVED
    assert review.checked_requirements is True
    assert review.checked_tests is True
    assert review.checked_diff_scope is True
    assert review.checked_secrets is True
    assert review.checked_constitution is True
    assert review.findings == []
    assert review.subject_sha256 is not None
    assert review.evidence_artifact_id is not None
    assert review.evidence_artifact_sha256 is not None
    assert_sha256(review.subject_sha256)
    assert_sha256(review.evidence_artifact_sha256)

    matching = [
        item
        for item in state.artifacts
        if item.artifact_id == review.evidence_artifact_id
    ]
    assert len(matching) == 1
    evidence = matching[0]
    assert evidence.kind is ArtifactKind.REVIEW
    assert evidence.producer == "local-semantic-reviewer"
    assert evidence.media_type == "application/json"
    assert evidence.sha256 == review.evidence_artifact_sha256

    artifact_store = ArtifactStore(
        request.task_id,
        root=fixture.root / "live-engine" / "artifacts",
        policy=_policy(),
    )
    assert artifact_store.verify(evidence)
    raw = artifact_store.read_verified(evidence)
    assert raw == Path(evidence.path).read_bytes()
    assert len(raw) == evidence.size_bytes
    assert hashlib.sha256(raw).hexdigest() == evidence.sha256
    payload = json.loads(raw.decode("utf-8", errors="strict"))
    assert raw == _canonical_json_bytes(payload)
    assert set(payload) == {
        "attestation_after",
        "attestation_before",
        "attestation_sha256",
        "canonical_response",
        "canonical_response_sha256",
        "canonical_subject",
        "canonical_subject_sha256",
        "model_response_sha256",
        "model_response_utf8_exact",
        "producer",
        "request_sha256",
        "reviewed_at",
        "schema_version",
        "subject_sha256",
        "verdict",
    }
    assert payload["schema_version"] == "1.0"
    assert payload["producer"] == "local-semantic-reviewer"
    assert payload["verdict"] == "approved"
    assert payload["subject_sha256"] == review.subject_sha256
    assert payload["canonical_subject_sha256"] == review.subject_sha256
    assert (
        hashlib.sha256(_canonical_json_bytes(payload["canonical_subject"])).hexdigest()
        == review.subject_sha256
    )
    assert_sha256(payload["request_sha256"])
    assert_sha256(payload["canonical_response_sha256"])
    assert_sha256(payload["model_response_sha256"])
    assert_sha256(payload["attestation_sha256"])

    model_response = payload["model_response_utf8_exact"]
    assert isinstance(model_response, str)
    assert (
        hashlib.sha256(model_response.encode("utf-8", errors="strict")).hexdigest()
        == payload["model_response_sha256"]
    )
    assert json.loads(model_response) == payload["canonical_response"]

    response = payload["canonical_response"]
    assert isinstance(response, dict)
    assert set(response) == {
        "coverage",
        "findings",
        "schema_version",
        "subject_sha256",
        "verdict",
    }
    assert response["schema_version"] == "1.0"
    assert response["subject_sha256"] == review.subject_sha256
    assert response["verdict"] == "approved"
    assert response["findings"] == []
    assert (
        hashlib.sha256(_canonical_json_bytes(response)).hexdigest()
        == payload["canonical_response_sha256"]
    )

    policy = _policy()
    semantic_config = LocalSemanticReviewConfig.from_policy(policy)
    attestation = payload["attestation_before"]
    assert isinstance(attestation, dict)
    assert set(attestation) == {
        "executable_path",
        "executable_sha256",
        "listener_create_time_ns",
        "listener_pid",
        "model_alias",
        "model_digest",
    }
    assert payload["attestation_after"] == attestation
    assert attestation["listener_pid"] > 0
    assert attestation["listener_create_time_ns"] > 0
    assert attestation["executable_path"].replace("\\", "/") == (
        semantic_config.expected_executable_path.replace("\\", "/")
    )
    assert (
        attestation["executable_sha256"]
        == semantic_config.expected_executable_sha256
    )
    assert attestation["model_alias"] == policy.local_semantic_model
    assert attestation["model_digest"] == policy.local_semantic_expected_model_digest
    attestation_digest = hashlib.sha256(_canonical_json_bytes(attestation)).hexdigest()
    assert (
        payload["attestation_sha256"]
        == hashlib.sha256(
            _canonical_json_bytes(
                {"after": attestation_digest, "before": attestation_digest}
            )
        ).hexdigest()
    )

    subject = payload["canonical_subject"]
    assert isinstance(subject, dict)
    assert set(subject) == {
        "attempt_index",
        "command_evidence",
        "deterministic_review_id",
        "diff_artifact",
        "evidence_allowlist",
        "executor_claimed_summary_untrusted",
        "executor_output_artifact",
        "knowledge_artifact",
        "request",
        "required_command_ids",
        "requirements",
        "schema_version",
        "source_base_commit",
        "source_repository",
        "worktree_binding_sha256",
    }
    assert subject["schema_version"] == "1.0"
    assert subject["attempt_index"] == len(state.attempts)
    assert subject["source_base_commit"] == fixture.baseline_sha
    assert Path(subject["source_repository"]).resolve(strict=True) == (
        fixture.repository.resolve(strict=True)
    )
    assert_sha256(subject["worktree_binding_sha256"])
    assert subject["request"] == request.model_dump(mode="json")
    if request.mode is CodingMode.READ_ONLY:
        assert subject["diff_artifact"] is None
    else:
        assert subject["diff_artifact"] is not None

    state_artifacts = {item.artifact_id: item for item in state.artifacts}

    def assert_subject_artifact(value: object) -> None:
        assert isinstance(value, dict)
        payload_fields = set(value).intersection(
            {
                "payload_utf8_exact",
                "payload_json_projection",
                "payload_sha256_only",
            }
        )
        assert len(payload_fields) == 1
        assert set(value).difference(payload_fields) == {
            "artifact_id",
            "created_at",
            "kind",
            "media_type",
            "producer",
            "role",
            "sha256",
            "size_bytes",
        }
        artifact = state_artifacts[value["artifact_id"]]
        assert value["kind"] == artifact.kind.value
        assert value["producer"] == artifact.producer
        assert value["media_type"] == artifact.media_type
        assert value["sha256"] == artifact.sha256
        assert_sha256(value["sha256"])
        assert value["size_bytes"] == artifact.size_bytes
        artifact_bytes = artifact_store.read_verified(artifact)
        assert len(artifact_bytes) == value["size_bytes"]
        assert hashlib.sha256(artifact_bytes).hexdigest() == value["sha256"]
        if "payload_utf8_exact" in value:
            assert (
                artifact_bytes.decode("utf-8", errors="strict")
                == value["payload_utf8_exact"]
            )
        elif "payload_json_projection" in value:
            source = json.loads(artifact_bytes.decode("utf-8", errors="strict"))
            assert value["role"] == "knowledge"
            assert value["payload_json_projection"] == _knowledge_projection(source)
        else:
            assert value["role"] == "executor_output"
            assert value["payload_sha256_only"] is True

    assert_subject_artifact(subject["knowledge_artifact"])
    assert_subject_artifact(subject["executor_output_artifact"])
    if subject["diff_artifact"] is not None:
        assert_subject_artifact(subject["diff_artifact"])
    command_evidence = subject["command_evidence"]
    assert subject["required_command_ids"] == [
        item["result"]["command_id"] for item in command_evidence
    ]
    state_commands = {item.command_id: item for item in state.command_results}
    for item in command_evidence:
        assert item["result"] == state_commands[
            item["result"]["command_id"]
        ].model_dump(mode="json")
        assert_subject_artifact(item["output_artifact"])

    requirements = [item["requirement_id"] for item in subject["requirements"]]
    coverage = response["coverage"]
    assert [item["requirement_id"] for item in coverage] == requirements
    allowlist = {item["ref"]: item["kind"] for item in subject["evidence_allowlist"]}
    covered_refs: set[str] = set()
    for item in coverage:
        assert set(item) == {"evidence_refs", "requirement_id"}
        assert item["evidence_refs"]
        for evidence_ref in item["evidence_refs"]:
            assert set(evidence_ref) == {"kind", "ref"}
            assert allowlist[evidence_ref["ref"]] == evidence_ref["kind"]
            covered_refs.add(evidence_ref["ref"])
    assert {
        f"command.{command_id}" for command_id in subject["required_command_ids"]
    }.issubset(covered_refs)


def _assert_exactly_one_local_commit_without_push(
    fixture: CodingFixture,
    *,
    worktree: Path,
    commit_sha: str,
    commit_message: str,
    remote_before: dict[str, str],
) -> None:
    assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
    assert fixture.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip() == commit_sha
    assert (
        fixture.git(
            [
                "rev-list",
                "--count",
                commit_sha,
                f"^{fixture.baseline_sha}",
                "--",
            ],
            cwd=worktree,
        ).stdout.strip()
        == "1"
    )
    assert (
        fixture.git(
            ["log", "-1", "--pretty=%s", commit_sha], cwd=worktree
        ).stdout.strip()
        == commit_message
    )
    assert fixture.git(["status", "--porcelain=v1"], cwd=worktree).stdout == ""
    assert _remote_refs(fixture) == remote_before
    fixture.assert_remote_unchanged()


def _request(
    fixture: CodingFixture,
    *,
    task_id: str,
    goal: str,
    mode: CodingMode,
    risk: CodingRisk,
    cloud_execution: bool,
    rule_scope_paths: list[str] | None = None,
    expected_diff_paths: list[str] | None = None,
    verification_commands: list[VerificationCommandV1] | None = None,
    ui_url: str | None = None,
    ui_selector: str | None = None,
    ui_expected_text: str | None = None,
    local_commit: bool = False,
    commit_message: str | None = None,
) -> CodingTaskRequestV1:
    commit_constraint = (
        "Do not create a Git commit yourself. The engine is exclusively responsible for the "
        "single authorized local commit after verification and review."
        if local_commit
        else "Do not create a Git commit; this task does not authorize one."
    )
    return CodingTaskRequestV1(
        task_id=task_id,
        request_id=f"request-{task_id}",
        goal=goal,
        repository_path=str(fixture.repository),
        mode=mode,
        risk=risk,
        constraints=[
            "This repository is a disposable synthetic PUBLIC fixture.",
            commit_constraint,
            "Never push, publish, deploy, install dependencies, alter remotes, or access credentials.",
            "Work only in the isolated task worktree and make the smallest bounded change.",
        ],
        acceptance_criteria=[
            "Return the exact requested fixture result with auditable evidence."
        ],
        verification_plan=[
            "Use only declared checks plus the engine's deterministic diff review."
        ],
        verification_commands=verification_commands or [],
        permissions=CodingPermissionsV1(
            modify_files=(mode is CodingMode.WRITE),
            local_commit=local_commit,
            cloud_execution=cloud_execution,
            data_classification=DataClassification.PUBLIC,
        ),
        route_reasons=[
            "Explicit live test against a generated PUBLIC synthetic repository.",
            f"Risk is {risk.value}; mode is {mode.value}.",
        ],
        rule_scope_paths=rule_scope_paths or [],
        expected_diff_paths=expected_diff_paths or [],
        commit_message=commit_message,
        ui_url=ui_url,
        ui_selector=ui_selector,
        ui_expected_text=ui_expected_text,
    )


def _engine(
    fixture: CodingFixture,
    *,
    qwen_executor: object,
    codex_executor: object,
) -> tuple[CodingEngine, CodingTaskStore]:
    policy = _policy()
    state_root = fixture.root / "live-engine"
    state_root.mkdir()
    store = CodingTaskStore(state_root / "coding.sqlite3")
    memory = MemoryStore(
        state_root / "memory.sqlite3",
        create_migration_backup=False,
    )
    knowledge = KnowledgeEngine(
        KnowledgeStore(state_root / "knowledge.sqlite3"),
        memory_store=memory,
    )
    context = CodingContextBuilder(knowledge_engine=knowledge, policy=policy)
    worktrees = WorktreeManager(
        registry_root=state_root / "registry",
        owned_worktree_root=state_root / "worktrees",
        policy=policy,
    )
    engine = CodingEngine(
        store=store,
        worktree_manager=worktrees,
        context_builder=context,
        qwen_executor=qwen_executor,  # type: ignore[arg-type]
        codex_executor=codex_executor,  # type: ignore[arg-type]
        policy=policy,
        artifact_root=state_root / "artifacts",
    )
    return engine, store


def _live_qwen() -> _RecordingExecutor:
    executable = shutil.which("qwen.cmd") or shutil.which("qwen")
    assert executable, "Qwen Code CLI is required for the selected live test"
    model = os.environ.get("LOCAL_AGENT_LIVE_QWEN_MODEL") or None
    return _RecordingExecutor(
        QwenExecutor(policy=_policy(), executable=executable, model=model),
        ExecutorKind.LOCAL_QWEN,
    )


def _live_codex() -> _RecordingExecutor:
    executable = resolve_codex_executable()
    assert executable, "Codex CLI is required for the explicitly selected cloud test"
    model = os.environ.get("LOCAL_AGENT_LIVE_CODEX_MODEL") or None
    return _RecordingExecutor(
        CodexExecutor(policy=_policy(), executable=executable, model=model),
        ExecutorKind.CODEX_EXEC,
    )


@pytest.mark.required_e2e
@pytest.mark.live_qwen
@live_coding
def test_live_qwen_read_only_finds_exact_fact_without_command_or_diff() -> None:
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-qwen-read")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        qwen = _live_qwen()
        engine, store = _engine(
            fixture,
            qwen_executor=qwen,
            codex_executor=_ForbiddenExecutor(ExecutorKind.CODEX_EXEC),
        )
        request = _request(
            fixture,
            task_id="live-qwen-read-only",
            goal=(
                "Read README.md with a filesystem read tool. Return the complete exact assignment "
                "whose name is CODING_FIXTURE_FACT, byte-for-byte including the variable name and "
                "equals sign. A response containing only the value is incorrect. Do not run any "
                "command and do not change a file."
            ),
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.LOW,
            cloud_execution=False,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)
        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert EXPECTED_FACT in result.summary
        assert result.modified_files == []
        assert result.commit_sha is None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert state is not None
        _assert_attested_local_semantic_review(
            state,
            request=request,
            fixture=fixture,
        )
        assert state.command_results == []
        assert state.modified_files == []
        assert "README.md" in state.inspected_files
        assert len(qwen.results) == 1
        assert qwen.results[0].executor is ExecutorKind.LOCAL_QWEN
        assert qwen.results[0].command_count == 0
        assert not any(item.kind is ArtifactKind.DIFF for item in state.artifacts)
        assert file_snapshot(fixture.repository) == source_before
        assert file_snapshot(worktree) == source_before
        _assert_no_commit_or_push(
            fixture, worktree=worktree, remote_before=remote_before
        )


@pytest.mark.required_e2e
@pytest.mark.live_qwen
@live_coding
def test_live_qwen_write_preserves_exact_bytes_without_commit_or_push() -> None:
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-qwen-write")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        original_bytes = (fixture.repository / "exact.bin").read_bytes()
        remote_before = _remote_refs(fixture)
        qwen = _live_qwen()
        engine, store = _engine(
            fixture,
            qwen_executor=qwen,
            codex_executor=_ForbiddenExecutor(ExecutorKind.CODEX_EXEC),
        )
        request = _request(
            fixture,
            task_id="live-qwen-exact-write",
            goal=(
                "Replace the complete contents of exact.bin with exactly the 12 ASCII bytes "
                "QWEN_CODE_OK (hex: 51 57 45 4e 5f 43 4f 44 45 5f 4f 4b). "
                "There must be no BOM and no trailing newline. Change no other file."
            ),
            mode=CodingMode.WRITE,
            risk=CodingRisk.LOW,
            cloud_execution=False,
            expected_diff_paths=["exact.bin"],
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
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)

        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert result.modified_files == ["exact.bin"]
        assert result.commit_sha is None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert (worktree / "exact.bin").read_bytes() == EXPECTED_QWEN_BYTES
        assert (fixture.repository / "exact.bin").read_bytes() == original_bytes
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        _assert_attested_local_semantic_review(
            state,
            request=request,
            fixture=fixture,
        )
        assert state.modified_files == ["exact.bin"]
        assert 1 <= len(state.attempts) <= 2
        assert all(item.status is AttemptStatus.FAILED for item in state.attempts[:-1])
        assert state.attempts[-1].status is AttemptStatus.PASSED
        assert len(qwen.results) == len(state.attempts)
        assert all(item.executor is ExecutorKind.LOCAL_QWEN for item in qwen.results)
        assert len(state.command_results) == 2
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert state.command_results[0].argv[1:] == [
            "-m",
            "unittest",
            "tests.exact_check",
            "-v",
        ]
        assert Path(state.command_results[1].argv[0]).is_absolute()
        assert Path(state.command_results[1].argv[0]).name.casefold() in {
            "git",
            "git.exe",
        }
        assert state.command_results[1].argv[1:] == ["diff", "--check"]
        assert any(item.kind is ArtifactKind.DIFF for item in state.artifacts)
        _assert_no_commit_or_push(
            fixture, worktree=worktree, remote_before=remote_before
        )


@pytest.mark.required_e2e
@pytest.mark.live_qwen
@live_coding
def test_live_qwen_adds_regression_fixes_calculator_and_engine_commits_once() -> None:
    commit_message = "Add calculator regression and fix implementation"
    with coding_fixture(temp_parent=_temp_parent(), run_id=_run_id("qr")) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        qwen = _live_qwen()
        engine, store = _engine(
            fixture,
            qwen_executor=qwen,
            codex_executor=_ForbiddenExecutor(ExecutorKind.CODEX_EXEC),
        )
        request = _request(
            fixture,
            task_id="live-qwen-regression",
            goal=(
                "First add a new failing unittest method named "
                "test_adds_negative_and_positive_integer to the existing CalculatorTests class in "
                "tests/test_calculator.py; it must assert add(-4, 9) == 5. Then make the minimal "
                "correction in src/calculator.py so add returns the sum. Preserve the existing test. "
                "Use native file-edit tools only: do not create helper, scratch, or temporary files. "
                "Change no other paths and do not commit; the engine owns the authorized commit."
            ),
            mode=CodingMode.WRITE,
            risk=CodingRisk.LOW,
            cloud_execution=False,
            expected_diff_paths=[
                "src/calculator.py",
                "tests/test_calculator.py",
            ],
            verification_commands=[
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
                    purpose="Run the original and newly added calculator regressions.",
                    timeout_seconds=60,
                )
            ],
            local_commit=True,
            commit_message=commit_message,
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)

        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert result.modified_files == [
            "src/calculator.py",
            "tests/test_calculator.py",
        ]
        assert result.commit_sha is not None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert (
            b"return left + right" in (worktree / "src" / "calculator.py").read_bytes()
        )
        regression = (worktree / "tests" / "test_calculator.py").read_text(
            encoding="utf-8"
        )
        assert "test_adds_negative_and_positive_integer" in regression
        assert "add(-4,9)" in "".join(regression.split())
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        _assert_attested_local_semantic_review(
            state,
            request=request,
            fixture=fixture,
        )
        assert state.commit_sha == result.commit_sha
        assert 1 <= len(state.attempts) <= 2
        assert all(item.status is AttemptStatus.FAILED for item in state.attempts[:-1])
        assert state.attempts[-1].status is AttemptStatus.PASSED
        assert len(qwen.results) == len(state.attempts)
        assert all(item.executor is ExecutorKind.LOCAL_QWEN for item in qwen.results)
        _assert_exactly_one_local_commit_without_push(
            fixture,
            worktree=worktree,
            commit_sha=result.commit_sha,
            commit_message=commit_message,
            remote_before=remote_before,
        )
        assert (
            fixture.git(["rev-parse", "HEAD^"], cwd=worktree).stdout.strip()
            == fixture.baseline_sha
        )
        committed_paths = fixture.git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", result.commit_sha],
            cwd=worktree,
        ).stdout.splitlines()
        assert committed_paths == [
            "src/calculator.py",
            "tests/test_calculator.py",
        ]
        print(
            "REQUIRED_E2E_EVIDENCE "
            + json.dumps(
                {
                    "scenario": "qwen_regression_engine_commit",
                    "task_id": request.task_id,
                    "attempts": len(state.attempts),
                    "executor_duration_ms": [
                        {
                            "executor": item.executor.value,
                            "duration_ms": item.duration_ms,
                        }
                        for item in qwen.results
                    ],
                    "worktree": str(worktree),
                    "branch": result.branch,
                    "base_sha": fixture.baseline_sha,
                    "final_head": result.commit_sha,
                    "commit_sha": result.commit_sha,
                    "remote_unchanged": _remote_refs(fixture) == remote_before,
                },
                sort_keys=True,
            )
        )


class _QuietFixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _DynamicFixtureHandler(_QuietFixtureHandler):
    def __init__(
        self,
        *args: object,
        directory_provider: Callable[[], Path],
        **kwargs: object,
    ) -> None:
        super().__init__(
            *args,
            directory=str(directory_provider().resolve(strict=True)),
            **kwargs,
        )


@contextmanager
def _fixture_web_server(
    web_root: Path | Callable[[], Path],
) -> Iterator[str]:
    provider = web_root if callable(web_root) else lambda: web_root
    handler = functools.partial(
        _DynamicFixtureHandler,
        directory_provider=provider,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        assert 1024 <= port <= 65535
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()


@contextmanager
def _websocket_egress_fixture(
) -> Iterator[tuple[str, threading.Event, threading.Event]]:
    approved_reached = threading.Event()
    forbidden_reached = threading.Event()

    class _ForbiddenWebSocketHandler(_QuietFixtureHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Upgrade", "").casefold() == "websocket":
                forbidden_reached.set()
            self.send_error(403)

    forbidden_server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _ForbiddenWebSocketHandler
    )
    forbidden_thread = threading.Thread(
        target=forbidden_server.serve_forever, daemon=True
    )
    forbidden_thread.start()
    forbidden_port = int(forbidden_server.server_address[1])
    assert 1024 <= forbidden_port <= 65535

    body = (
        "<!doctype html><html><body>"
        '<div data-testid="status">SECURE</div>'
        "<script>"
        "const approved = new WebSocket(`ws://${location.host}/approved`);"
        f"const forbidden = new WebSocket('ws://127.0.0.1:{forbidden_port}/forbidden');"
        "approved.addEventListener('error', () => {});"
        "forbidden.addEventListener('error', () => {});"
        "</script></body></html>"
    ).encode("utf-8")

    class _InlineUiHandler(_QuietFixtureHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Upgrade", "").casefold() == "websocket":
                approved_reached.set()
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    page_server = ThreadingHTTPServer(("127.0.0.1", 0), _InlineUiHandler)
    page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
    page_thread.start()
    try:
        page_port = int(page_server.server_address[1])
        assert 1024 <= page_port <= 65535
        yield (
            f"http://127.0.0.1:{page_port}/index.html",
            approved_reached,
            forbidden_reached,
        )
    finally:
        page_server.shutdown()
        page_server.server_close()
        page_thread.join(timeout=10)
        forbidden_server.shutdown()
        forbidden_server.server_close()
        forbidden_thread.join(timeout=10)
        assert not page_thread.is_alive()
        assert not forbidden_thread.is_alive()


@pytest.mark.required_e2e
@pytest.mark.live_playwright
@live_coding
def test_live_playwright_captures_verified_screenshot_evidence() -> None:
    assert shutil.which("node.exe") or shutil.which("node"), (
        "Node.js is required for the selected Playwright live test"
    )
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-playwright")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        policy = _policy()
        artifacts = ArtifactStore(
            "live-playwright-evidence",
            root=fixture.root / "playwright-artifacts",
            policy=policy,
        )
        with _fixture_web_server(fixture.repository / "web") as url:
            request = _request(
                fixture,
                task_id="live-playwright-task",
                goal="Verify the stable synthetic fixture UI and capture screenshot evidence.",
                mode=CodingMode.READ_ONLY,
                risk=CodingRisk.LOW,
                cloud_execution=False,
                ui_url=url,
                ui_selector='[data-testid="status"]',
                ui_expected_text="PENDING",
            )
            result, screenshot, evidence = UIVerificationRunner(
                artifact_store=artifacts,
                policy=policy,
            ).run(
                request,
                command_id="live-ui-check",
                repository=fixture.repository,
            )

        assert result.status is CommandStatus.PASSED
        assert result.exit_code == 0
        assert screenshot is not None
        assert screenshot.kind is ArtifactKind.SCREENSHOT
        assert screenshot.size_bytes > 100
        assert artifacts.verify(screenshot)
        assert Path(screenshot.path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert evidence.kind is ArtifactKind.UI_EVIDENCE
        assert artifacts.verify(evidence)
        payload = json.loads(Path(evidence.path).read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert payload["screenshot_artifact_id"] == screenshot.artifact_id
        assert payload["evidence"]["matched"] is True
        assert (
            payload["evidence"]["titleHash"]
            == hashlib.sha256(b"Local Agent Fixture").hexdigest()
        )
        assert (
            payload["evidence"]["actualTextHash"]
            == hashlib.sha256(b"PENDING").hexdigest()
        )
        assert file_snapshot(fixture.repository) == source_before
        assert _remote_refs(fixture) == remote_before
        print(
            "PLAYWRIGHT_EVIDENCE "
            + json.dumps(
                {
                    "artifact_id": screenshot.artifact_id,
                    "sha256": screenshot.sha256,
                    "size_bytes": screenshot.size_bytes,
                },
                sort_keys=True,
            )
        )


@pytest.mark.required_e2e
@pytest.mark.live_playwright
@live_coding
def test_live_playwright_blocks_cross_origin_websocket_egress() -> None:
    assert shutil.which("node.exe") or shutil.which("node"), (
        "Node.js is required for the selected Playwright live test"
    )
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-playwright-websocket")
    ) as fixture:
        policy = _policy()
        artifacts = ArtifactStore(
            "live-playwright-websocket-egress",
            root=fixture.root / "playwright-websocket-artifacts",
            policy=policy,
        )
        with _websocket_egress_fixture() as (
            url,
            approved_reached,
            forbidden_reached,
        ):
            request = _request(
                fixture,
                task_id="live-playwright-websocket-task",
                goal="Verify the fixture UI without cross-origin WebSocket egress.",
                mode=CodingMode.READ_ONLY,
                risk=CodingRisk.LOW,
                cloud_execution=False,
                ui_url=url,
                ui_selector='[data-testid="status"]',
                ui_expected_text="SECURE",
            )
            result, screenshot, evidence = UIVerificationRunner(
                artifact_store=artifacts,
                policy=policy,
            ).run(
                request,
                command_id="live-ui-websocket-egress",
                repository=fixture.repository,
            )

            assert result.status is CommandStatus.PASSED
            assert screenshot is not None
            assert artifacts.verify(screenshot)
            assert artifacts.verify(evidence)
            assert approved_reached.wait(1.0)
            assert not forbidden_reached.wait(0.25)


@pytest.mark.required_e2e
@pytest.mark.live_qwen
@pytest.mark.live_playwright
@live_coding
def test_live_qwen_ui_change_is_verified_by_playwright_in_same_worktree() -> None:
    assert shutil.which("node.exe") or shutil.which("node"), (
        "Node.js is required for the selected Playwright live test"
    )
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-qwen-ui")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        qwen = _live_qwen()
        engine, store = _engine(
            fixture,
            qwen_executor=qwen,
            codex_executor=_ForbiddenExecutor(ExecutorKind.CODEX_EXEC),
        )
        task_id = "live-qwen-ui-ready"
        served_roots: list[Path] = []

        def active_worktree_web_root() -> Path:
            record = engine.worktree_manager.load(task_id)
            if record is None:
                raise RuntimeError(
                    "engine worktree is not registered before UI verification"
                )
            root = (Path(record.worktree_path) / "web").resolve(strict=True)
            served_roots.append(root)
            return root

        with _fixture_web_server(active_worktree_web_root) as url:
            request = _request(
                fixture,
                task_id=task_id,
                goal=(
                    "In web/index.html, replace only the visible status text PENDING with READY. "
                    "Preserve the page title, data-testid attribute, HTML structure, and all other files."
                ),
                mode=CodingMode.WRITE,
                risk=CodingRisk.LOW,
                cloud_execution=False,
                expected_diff_paths=["web/index.html"],
                ui_url=url,
                ui_selector='[data-testid="status"]',
                ui_expected_text="READY",
            )
            result = engine.run(request)

        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)
        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert result.modified_files == ["web/index.html"]
        assert result.commit_sha is None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert ">READY<" in (worktree / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        assert ">PENDING<" not in (worktree / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        assert ">PENDING<" in (fixture.repository / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        _assert_attested_local_semantic_review(
            state,
            request=request,
            fixture=fixture,
        )
        assert len(state.attempts) == 1
        assert state.attempts[0].status is AttemptStatus.PASSED
        assert len(qwen.results) == 1
        assert len(state.command_results) == 2
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert served_roots
        assert all(
            item == (worktree / "web").resolve(strict=True) for item in served_roots
        )
        assert fixture.git(
            ["status", "--porcelain=v1"], cwd=worktree
        ).stdout.splitlines() == [" M web/index.html"]
        screenshots = [
            item for item in state.artifacts if item.kind is ArtifactKind.SCREENSHOT
        ]
        ui_evidence = [
            item for item in state.artifacts if item.kind is ArtifactKind.UI_EVIDENCE
        ]
        assert len(screenshots) == 1
        assert len(ui_evidence) == 1
        artifact_store = ArtifactStore(
            request.task_id,
            root=fixture.root / "live-engine" / "artifacts",
            policy=_policy(),
        )
        screenshot = screenshots[0]
        evidence = ui_evidence[0]
        assert artifact_store.verify(screenshot)
        assert artifact_store.verify(evidence)
        assert Path(screenshot.path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        payload = json.loads(Path(evidence.path).read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert payload["screenshot_artifact_id"] == screenshot.artifact_id
        assert payload["evidence"]["matched"] is True
        assert (
            payload["evidence"]["actualTextHash"]
            == hashlib.sha256(b"READY").hexdigest()
        )
        assert any(
            item.command_id == "a1-ui"
            and item.status is CommandStatus.PASSED
            and Path(item.cwd).resolve(strict=True) == worktree
            for item in state.command_results
        )
        _assert_no_commit_or_push(
            fixture,
            worktree=worktree,
            remote_before=remote_before,
        )
        print(
            "REQUIRED_E2E_EVIDENCE "
            + json.dumps(
                {
                    "scenario": "qwen_ui_playwright_worktree",
                    "task_id": request.task_id,
                    "attempts": len(state.attempts),
                    "executor_duration_ms": [
                        {
                            "executor": item.executor.value,
                            "duration_ms": item.duration_ms,
                        }
                        for item in qwen.results
                    ],
                    "worktree": str(worktree),
                    "branch": result.branch,
                    "base_sha": fixture.baseline_sha,
                    "final_head": fixture.git(
                        ["rev-parse", "HEAD"], cwd=worktree
                    ).stdout.strip(),
                    "commit_sha": result.commit_sha,
                    "remote_unchanged": _remote_refs(fixture) == remote_before,
                    "ui_screenshot_artifact_id": screenshot.artifact_id,
                    "ui_screenshot_sha256": screenshot.sha256,
                    "ui_screenshot_size_bytes": screenshot.size_bytes,
                },
                sort_keys=True,
            )
        )


@pytest.mark.required_e2e
@pytest.mark.live_codex_public
@live_codex
def test_live_codex_public_read_only_reports_security_finding_without_diff() -> None:
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-codex-review")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        codex = _live_codex()
        engine, store = _engine(
            fixture,
            qwen_executor=_ForbiddenExecutor(ExecutorKind.LOCAL_QWEN),
            codex_executor=codex,
        )
        request = _request(
            fixture,
            task_id="live-codex-security-review",
            goal=(
                "Perform a read-only security review of src/security_runner.py. Report the concrete "
                "command-injection flaw caused by shell=True and untrusted report_name. Do not edit files."
            ),
            mode=CodingMode.READ_ONLY,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
            rule_scope_paths=["src/security_runner.py"],
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)
        print(
            "CODEX_READ_ONLY_REVIEW_EVIDENCE "
            + json.dumps(
                state.review.model_dump(mode="json") if state and state.review else {},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
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
        assert state.review.findings
        serialized_review = state.review.model_dump_json().casefold()
        assert "security_runner.py" in serialized_review
        assert "shell" in serialized_review or "injection" in serialized_review
        assert len(state.attempts) == 1
        assert state.attempts[0].executor is ExecutorKind.CODEX_REVIEW
        assert state.attempts[0].status is AttemptStatus.PASSED
        assert not any(item.kind is ArtifactKind.DIFF for item in state.artifacts)
        assert len(codex.results) == 1
        assert codex.results[0].executor is ExecutorKind.CODEX_REVIEW
        assert file_snapshot(fixture.repository) == source_before
        assert file_snapshot(worktree) == source_before
        _assert_no_commit_or_push(
            fixture, worktree=worktree, remote_before=remote_before
        )


def _assert_safe_security_runner(path: Path) -> None:
    module = ast.parse(path.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)]
    subprocess_calls = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
        and call.func.attr == "run"
    ]
    assert len(subprocess_calls) == 1
    call = subprocess_calls[0]
    assert call.args and isinstance(call.args[0], (ast.List, ast.Tuple))
    shell_keywords = [item for item in call.keywords if item.arg == "shell"]
    assert not shell_keywords or all(
        isinstance(item.value, ast.Constant) and item.value.value is False
        for item in shell_keywords
    )


@pytest.mark.required_e2e
@pytest.mark.live_codex_public
@live_codex
def test_live_codex_public_writable_security_fix_is_verified_without_commit_or_push() -> (
    None
):
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-codex-fix")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        codex = _live_codex()
        engine, store = _engine(
            fixture,
            qwen_executor=_ForbiddenExecutor(ExecutorKind.LOCAL_QWEN),
            codex_executor=codex,
        )
        request = _request(
            fixture,
            task_id="live-codex-security-fix",
            goal=(
                "Fix the command injection in src/security_runner.py. Pass report_name as one literal "
                "argument in the argv list ['fixture-report', '--name', report_name], do not use a shell, "
                "preserve capture_output=True, text=True, check=True and change no other file."
            ),
            mode=CodingMode.WRITE,
            risk=CodingRisk.HIGH,
            cloud_execution=True,
            expected_diff_paths=["src/security_runner.py"],
            verification_commands=[
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
            ],
        )

        result = engine.run(request)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)

        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert result.modified_files == ["src/security_runner.py"]
        assert result.commit_sha is None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        _assert_safe_security_runner(worktree / "src" / "security_runner.py")
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        assert state.modified_files == ["src/security_runner.py"]
        assert len(state.command_results) == 2
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert state.review is not None
        assert state.review.reviewer is ExecutorKind.CODEX_REVIEW
        assert [item.executor for item in codex.results] == [
            ExecutorKind.CODEX_EXEC,
            ExecutorKind.CODEX_REVIEW,
        ]
        _assert_no_commit_or_push(
            fixture, worktree=worktree, remote_before=remote_before
        )


@pytest.mark.required_e2e
@pytest.mark.live_codex_public
@live_codex
def test_live_two_local_failures_handoff_resumes_same_worktree_with_codex() -> None:
    failure_messages = [
        "deterministic local hypothesis one failed before making a change",
        "deterministic local hypothesis two failed before making a change",
    ]
    with coding_fixture(
        temp_parent=_temp_parent(), run_id=_run_id("live-handoff-codex")
    ) as fixture:
        source_before = file_snapshot(fixture.repository)
        remote_before = _remote_refs(fixture)
        local_failures = _DeterministicFailingExecutor(failure_messages)
        codex = _live_codex()
        engine, store = _engine(
            fixture,
            qwen_executor=local_failures,
            codex_executor=codex,
        )
        request = _request(
            fixture,
            task_id="live-handoff-resume-codex",
            goal=(
                "Resume the preserved worktree and replace only the single ASCII '-' operator "
                "byte in src/calculator.py with '+', so add(2, 3) returns 5. Preserve every "
                "other byte, including final newlines; preserve all tests and change no other "
                "file."
            ),
            mode=CodingMode.WRITE,
            risk=CodingRisk.MEDIUM,
            cloud_execution=True,
            expected_diff_paths=["src/calculator.py"],
            verification_commands=[
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
                    purpose="Run the synthetic calculator regression after Codex resume.",
                    timeout_seconds=60,
                )
            ],
        )

        handoff = engine.run(request)
        handoff_state = store.load(request.task_id)
        assert handoff.status is CodingTaskStatus.HANDOFF_READY
        assert handoff.handoff_path is not None
        assert handoff.commit_sha is None
        assert handoff_state is not None
        assert [item.executor for item in handoff_state.attempts] == [
            ExecutorKind.LOCAL_QWEN,
            ExecutorKind.LOCAL_QWEN,
        ]
        assert [item.status for item in handoff_state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
        ]
        assert len(local_failures.calls) == 2
        assert codex.results == []
        assert handoff_state.unresolved_errors == failure_messages
        assert engine.worktree_manager.load(request.task_id).status == "active"  # type: ignore[union-attr]
        contract = CodingHandoffV1.model_validate_json(
            Path(handoff.handoff_path).read_bytes()
        )
        assert contract.task_id == request.task_id
        assert contract.request_id == request.request_id
        assert contract.worktree_path == handoff.worktree_path
        assert contract.branch == handoff.branch
        assert contract.source_base_commit == fixture.baseline_sha
        assert contract.unresolved_questions == failure_messages
        assert len(contract.attempts) == 2
        assert contract.modified_files == []
        assert contract.commands == []
        assert len(contract.resume_anchor_sha256) == 64
        assert all(Path(item.path).is_file() for item in contract.artifacts)
        handoff_worktree = Path(handoff.worktree_path or "").resolve(strict=True)
        assert file_snapshot(handoff_worktree) == source_before

        result = engine.resume(request.task_id)
        state = store.load(request.task_id)
        worktree = Path(result.worktree_path or "").resolve(strict=True)

        _emit_live_failure_diagnostics(result, state)
        assert result.status is CodingTaskStatus.COMPLETED
        assert result.worktree_path == handoff.worktree_path
        assert result.branch == handoff.branch
        assert worktree == handoff_worktree
        assert result.modified_files == ["src/calculator.py"]
        assert result.commit_sha is None
        assert result.verification_passed is True
        assert result.review_verdict is ReviewVerdict.APPROVED
        assert (
            b"return left + right" in (worktree / "src" / "calculator.py").read_bytes()
        )
        assert file_snapshot(fixture.repository) == source_before
        assert state is not None
        assert [item.status for item in state.attempts] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
            AttemptStatus.PASSED,
        ]
        assert state.attempts[-1].executor is ExecutorKind.CODEX_EXEC
        assert state.review is not None
        assert state.review.reviewer is ExecutorKind.CODEX_REVIEW
        assert state.review.verdict is ReviewVerdict.APPROVED
        assert state.review.findings == []
        assert [item.executor for item in codex.results] == [
            ExecutorKind.CODEX_EXEC,
            ExecutorKind.CODEX_REVIEW,
        ]
        assert len(state.command_results) == 2
        assert all(
            item.status is CommandStatus.PASSED for item in state.command_results
        )
        assert fixture.git(
            ["status", "--porcelain=v1"], cwd=worktree
        ).stdout.splitlines() == [" M src/calculator.py"]
        assert engine.worktree_manager.load(request.task_id).status == "complete"  # type: ignore[union-attr]
        _assert_no_commit_or_push(
            fixture,
            worktree=worktree,
            remote_before=remote_before,
        )
        print(
            "REQUIRED_E2E_EVIDENCE "
            + json.dumps(
                {
                    "scenario": "two_local_failures_codex_resume",
                    "task_id": request.task_id,
                    "attempts": len(state.attempts),
                    "local_failure_attempts": len(local_failures.calls),
                    "executor_duration_ms": [
                        {
                            "executor": item.executor.value,
                            "duration_ms": item.duration_ms,
                        }
                        for item in codex.results
                    ],
                    "worktree": str(worktree),
                    "branch": result.branch,
                    "base_sha": fixture.baseline_sha,
                    "final_head": fixture.git(
                        ["rev-parse", "HEAD"], cwd=worktree
                    ).stdout.strip(),
                    "commit_sha": result.commit_sha,
                    "handoff_artifact": handoff.handoff_path,
                    "remote_unchanged": _remote_refs(fixture) == remote_before,
                },
                sort_keys=True,
            )
        )
