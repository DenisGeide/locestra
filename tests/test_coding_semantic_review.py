from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

import services.coding.semantic_review as semantic_review
from services.coding.artifacts import ArtifactStore
from services.coding.contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CommandResultV1,
    CommandStatus,
    ExecutorKind,
    ReviewResultV1,
    ReviewVerdict,
)
from services.coding.reviewer import merge_local_semantic_review
from services.coding.semantic_review import (
    LocalSemanticReviewConfig,
    LocalSemanticReviewer,
    SemanticArtifactEvidence,
    SemanticAttestation,
    SemanticCommandEvidence,
    SemanticReviewBlocked,
    SemanticReviewSubject,
    build_semantic_review_subject_sha256,
    validate_semantic_result,
)


_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
_BASE = "1" * 40
_BINDING = "2" * 64
_EXE_SHA = "3" * 64
_MODEL_SHA = "005d4fcb23bcdfccb3e919c6844cb550dc91972f207cb6f5d52184115ef44573"
_EXE_PATH = "C:/Program Files/Ollama/ollama.exe"


def test_ollama_executable_resolves_from_runtime_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = tmp_path / ("ollama.exe" if semantic_review.os.name == "nt" else "ollama")
    executable.write_bytes(b"synthetic executable fixture")
    monkeypatch.setenv("LOCESTRA_OLLAMA_EXECUTABLE", str(executable))

    assert Path(semantic_review.resolve_ollama_executable()).resolve() == executable.resolve()


def test_ollama_executable_auto_digest_is_runtime_derived_and_pin_remains_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = tmp_path / ("ollama.exe" if semantic_review.os.name == "nt" else "ollama")
    payload = b"synthetic portable Ollama executable"
    executable.write_bytes(payload)
    monkeypatch.setenv("LOCESTRA_OLLAMA_EXECUTABLE", str(executable))

    automatic = LocalSemanticReviewConfig(
        expected_executable_path="auto",
        expected_executable_sha256="auto",
        expected_model_digest=_MODEL_SHA,
    )
    assert automatic.expected_executable_sha256 == hashlib.sha256(payload).hexdigest()

    pinned = LocalSemanticReviewConfig(
        expected_executable_path="auto",
        expected_executable_sha256=_EXE_SHA,
        expected_model_digest=_MODEL_SHA,
    )
    assert pinned.expected_executable_sha256 == _EXE_SHA


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _config(**overrides: object) -> LocalSemanticReviewConfig:
    values: dict[str, object] = {
        "expected_executable_path": _EXE_PATH,
        "expected_executable_sha256": _EXE_SHA,
        "expected_model_digest": _MODEL_SHA,
    }
    values.update(overrides)
    return LocalSemanticReviewConfig(**values)  # type: ignore[arg-type]


def _request(mode: CodingMode) -> CodingTaskRequestV1:
    write = mode is CodingMode.WRITE
    return CodingTaskRequestV1(
        task_id="semantic-task",
        request_id="semantic-request",
        goal=(
            "Make calculate_total reject a negative quantity."
            if write
            else "Report the exact API_VERSION constant."
        ),
        repository_path="C:/synthetic/repository",
        mode=mode,
        risk=CodingRisk.MEDIUM,
        constraints=[
            "Do not change the public function signature.",
            "Base every factual claim on current evidence.",
        ],
        acceptance_criteria=[
            (
                "calculate_total(-1, 10) raises ValueError."
                if write
                else "The answer exactly matches src/version.py."
            ),
            "The conclusion is supported by current-attempt evidence.",
        ],
        verification_plan=["Inspect exact evidence and run the bounded verifier."],
        permissions=CodingPermissionsV1(modify_files=write),
        expected_diff_paths=["src/calculator.py"] if write else [],
    )


def _artifact(
    artifact_id: str,
    kind: ArtifactKind,
    payload: bytes,
    *,
    producer: str = "coding-engine",
) -> SemanticArtifactEvidence:
    return SemanticArtifactEvidence(
        reference=ArtifactReferenceV1(
            artifact_id=artifact_id,
            kind=kind,
            path=f"artifacts/{artifact_id}.json",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/json"
            if kind is not ArtifactKind.DIFF
            else "text/x-diff",
            producer=producer,
            created_at=_NOW,
        ),
        payload=payload,
    )


def _subject(
    mode: CodingMode,
    *,
    diff: bytes | None = None,
    output: bytes = b"1 passed: negative quantity raises ValueError\n",
) -> SemanticReviewSubject:
    write = mode is CodingMode.WRITE
    diff_payload = (
        diff
        if diff is not None
        else (
            b"diff --git a/src/calculator.py b/src/calculator.py\n"
            b"--- a/src/calculator.py\n"
            b"+++ b/src/calculator.py\n"
            b"@@ -1,2 +1,4 @@\n"
            b" def calculate_total(quantity, price):\n"
            b"+    if quantity < 0:\n"
            b"+        raise ValueError('negative quantity')\n"
            b"     return quantity * price\n"
            if write
            else b""
        )
    )
    commands: tuple[SemanticCommandEvidence, ...] = ()
    required: tuple[str, ...] = ()
    if write:
        output_artifact = _artifact(
            "command-output-a1",
            ArtifactKind.COMMAND_OUTPUT,
            output,
            producer="coding-verifier",
        )
        command = CommandResultV1(
            command_id="verify-a1",
            argv=["python", "-m", "pytest", "tests/test_calculator.py"],
            cwd=".",
            purpose="Verify negative-quantity behavior.",
            status=CommandStatus.PASSED,
            exit_code=0,
            started_at=_NOW,
            finished_at=_NOW,
            duration_ms=10,
            output_artifact_id=output_artifact.reference.artifact_id,
            summary="Acceptance test passed.",
        )
        commands = (SemanticCommandEvidence(command, output_artifact),)
        required = (command.command_id,)
    knowledge = _canonical(
        {
            "files": [
                {
                    "content": 'API_VERSION = "2026.07"',
                    "path": "src/version.py",
                }
            ]
        }
    )
    return SemanticReviewSubject(
        request=_request(mode),
        attempt_index=1,
        source_repository="C:/synthetic/repository",
        source_base_commit=_BASE,
        worktree_binding_sha256=_BINDING,
        deterministic_review_id="det-review-a1",
        executor_claimed_summary="The requested result is complete.",
        executor_output_artifact=_artifact(
            "executor-output-a1",
            ArtifactKind.COMMAND_OUTPUT,
            (
                b"The requested implementation is complete.\n"
                if write
                else b"API_VERSION is 2026.07.\n"
            ),
            producer="local-qwen-executor",
        ),
        diff_artifact=(
            _artifact("diff-a1", ArtifactKind.DIFF, diff_payload) if write else None
        ),
        knowledge_artifact=_artifact(
            "knowledge-a1",
            ArtifactKind.CONTEXT,
            knowledge,
            producer="knowledge-engine",
        ),
        required_command_ids=required,
        command_evidence=commands,
    )


def _attestation(
    *, pid: int = 100, model_digest: str = _MODEL_SHA
) -> SemanticAttestation:
    return SemanticAttestation(
        listener_pid=pid,
        listener_create_time_ns=1_000_000_000,
        executable_path=_EXE_PATH,
        executable_sha256=_EXE_SHA,
        model_alias="local-strong",
        model_digest=model_digest,
    )


class _Attestor:
    def __init__(self, *values: SemanticAttestation) -> None:
        self.values = list(values or (_attestation(), _attestation()))

    def attest(self, *_args: object, **_kwargs: object) -> SemanticAttestation:
        return self.values.pop(0)


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
        declared_length: int | None = None,
        chunks: Callable[[], Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {
            "content-type": content_type,
            "content-length": str(
                len(body) if declared_length is None else declared_length
            ),
        }
        self.body = body
        self.chunks = chunks
        self.iterated = False

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self):
        self.iterated = True
        if self.chunks is not None:
            yield from self.chunks()
        else:
            yield self.body


def _api_body(
    content: bytes,
    *,
    model: str = "local-strong",
    finish_reason: str = "stop",
    reasoning: str | None = "bounded private chain-of-thought is ignored",
) -> bytes:
    message: dict[str, object] = {
        "role": "assistant",
        "content": content.decode("utf-8"),
    }
    if reasoning is not None:
        message["reasoning"] = reasoning
    return _canonical(
        {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "index": 0,
                    "message": message,
                }
            ],
            "model": model,
        }
    )


def _install_api(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Callable[[bytes], _FakeResponse],
) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        def stream(
            self,
            method: str,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> _FakeResponse:
            captured.update(method=method, url=url, content=content, headers=headers)
            return response_factory(content)

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(semantic_review.httpx, "Client", FakeClient)
    return captured


def _requirement_ids(subject: SemanticReviewSubject) -> list[str]:
    return [
        "goal",
        *(f"constraint.{index}" for index, _ in enumerate(subject.request.constraints)),
        *(
            f"acceptance.{index}"
            for index, _ in enumerate(subject.request.acceptance_criteria)
        ),
    ]


def _response(
    subject: SemanticReviewSubject,
    config: LocalSemanticReviewConfig,
    *,
    approved: bool,
    finding_title: str = "negative quantity is not rejected",
    subject_sha256: str | None = None,
    coverage_override: list[dict[str, object]] | None = None,
) -> bytes:
    digest = subject_sha256 or build_semantic_review_subject_sha256(subject, config)
    refs = (
        [
            {"kind": "artifact", "ref": "artifact.diff.diff-a1"},
            {"kind": "command_result", "ref": "command.verify-a1"},
        ]
        if subject.request.mode is CodingMode.WRITE
        else [{"kind": "artifact", "ref": "artifact.knowledge.knowledge-a1"}]
    )
    coverage = coverage_override or [
        {"evidence_refs": refs, "requirement_id": requirement_id}
        for requirement_id in _requirement_ids(subject)
    ]
    findings: list[dict[str, object]] = []
    if not approved:
        findings = [
            {
                "code": "behavior.wrong",
                "evidence_refs": refs,
                "failure_scenario": (
                    "The supplied evidence contradicts the executor's claimed result."
                ),
                "file": (
                    "src/calculator.py"
                    if subject.request.mode is CodingMode.WRITE
                    else "src/version.py"
                ),
                "line": 2 if subject.request.mode is CodingMode.WRITE else 1,
                "priority": "P1",
                "requirement_ids": ["goal"],
                "title": finding_title,
            }
        ]
    return _canonical(
        {
            "coverage": coverage,
            "findings": findings,
            "schema_version": "1.0",
            "subject_sha256": digest,
            "verdict": "approved" if approved else "rejected",
        }
    )


def _review(
    monkeypatch: pytest.MonkeyPatch,
    subject: SemanticReviewSubject,
    semantic_response: bytes,
    *,
    config: LocalSemanticReviewConfig | None = None,
    attestor: _Attestor | None = None,
    api_response: _FakeResponse | None = None,
    cancel_event: threading.Event | None = None,
    invariant: Callable[[SemanticReviewSubject], None] | None = None,
):
    selected = config or _config()
    _install_api(
        monkeypatch,
        lambda _request: api_response or _FakeResponse(_api_body(semantic_response)),
    )
    return LocalSemanticReviewer(
        selected,
        attestor=attestor or _Attestor(),
    ).review(
        subject,
        assert_subject_current=invariant or (lambda _subject: None),
        cancel_event=cancel_event,
    )


def _deterministic(*, all_structural_gates: bool = True) -> ReviewResultV1:
    return ReviewResultV1(
        reviewer_id="det-review-a1",
        reviewer=ExecutorKind.DETERMINISTIC,
        verdict=ReviewVerdict.APPROVED,
        findings=[],
        checked_requirements=False,
        checked_tests=all_structural_gates,
        checked_diff_scope=all_structural_gates,
        checked_secrets=all_structural_gates,
        checked_constitution=all_structural_gates,
        summary="Structural gates passed; semantic coverage is pending.",
        reviewed_at=_NOW,
    )


def _persisted_evidence(result) -> ArtifactReferenceV1:
    payload = result.evidence.artifact_bytes()
    return ArtifactReferenceV1(
        artifact_id="semantic-review-a1",
        kind=ArtifactKind.REVIEW,
        path="artifacts/semantic-review-a1.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="application/json",
        producer="local-semantic-reviewer",
        created_at=_NOW,
    )


def test_wrong_read_only_fact_is_rejected_by_independent_local_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    response = _response(
        subject,
        config,
        approved=False,
        finding_title="reported API version contradicts current source",
    )
    result = _review(monkeypatch, subject, response, config=config)

    assert result.verdict == "rejected"
    assert result.findings[0].priority == "P1"
    merged = merge_local_semantic_review(
        _deterministic(),
        subject=subject,
        semantic_result=result,
        evidence_artifact=_persisted_evidence(result),
        worktree_unchanged=True,
        semantic_config=config,
    )
    assert merged.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
    assert merged.verdict is ReviewVerdict.REJECTED
    assert merged.findings[-1].code.startswith("local_semantic.p1.")
    assert not merged.findings[-1].code.startswith("codex.")


def test_wrong_write_is_rejected_despite_generic_green_test(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    wrong_diff = (
        b"diff --git a/src/calculator.py b/src/calculator.py\n"
        b"+    if quantity < 0:\n"
        b"+        return 0\n"
    )
    subject = _subject(
        CodingMode.WRITE, diff=wrong_diff, output=b"1 smoke test passed\n"
    )
    response = _response(subject, config, approved=False)
    result = _review(monkeypatch, subject, response, config=config)

    assert result.findings[0].title == "negative quantity is not rejected"
    assert result.evidence.subject_sha256 == build_semantic_review_subject_sha256(
        subject, config
    )


def test_correct_result_requires_all_structural_gates_and_binds_artifact(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.WRITE)
    response = _response(subject, config, approved=True)
    captured = _install_api(
        monkeypatch,
        lambda _request: _FakeResponse(_api_body(response)),
    )
    result = LocalSemanticReviewer(config, attestor=_Attestor()).review(
        subject,
        assert_subject_current=lambda _subject: None,
    )
    artifact = _persisted_evidence(result)

    validate_semantic_result(result, subject, config)
    merged = merge_local_semantic_review(
        _deterministic(),
        subject=subject,
        semantic_result=result,
        evidence_artifact=artifact,
        worktree_unchanged=True,
        semantic_config=config,
    )
    assert merged.verdict is ReviewVerdict.APPROVED
    assert merged.reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
    assert merged.checked_requirements is True
    assert merged.evidence_artifact_id == artifact.artifact_id
    assert merged.evidence_artifact_sha256 == artifact.sha256
    assert merged.subject_sha256 == result.subject_sha256
    kwargs = captured["client_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["trust_env"] is False
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"].connect <= 10
    assert kwargs["timeout"].read > 30
    api_request = json.loads(captured["content"])
    assert api_request["reasoning_effort"] == "high"
    assert api_request["max_tokens"] == 6144
    assert api_request["response_format"]["type"] == "json_schema"
    response_schema = api_request["response_format"]["json_schema"]
    assert response_schema["strict"] is True
    assert response_schema["name"] == "local_semantic_review_v1"
    assert response_schema["schema"]["additionalProperties"] is False
    coverage_schema = response_schema["schema"]["properties"]["coverage"]
    assert len(coverage_schema["items"]["oneOf"]) == len(_requirement_ids(subject))
    assert coverage_schema["minItems"] == coverage_schema["maxItems"]

    def assert_portable_schema(value: object) -> None:
        if isinstance(value, dict):
            assert "prefixItems" not in value
            assert "uniqueItems" not in value
            assert "contains" not in value
            assert not isinstance(value.get("items"), bool)
            for nested in value.values():
                assert_portable_schema(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_portable_schema(nested)

    assert_portable_schema(response_schema["schema"])

    structurally_blocked = merge_local_semantic_review(
        _deterministic(all_structural_gates=False),
        subject=subject,
        semantic_result=result,
        evidence_artifact=artifact,
        worktree_unchanged=True,
        semantic_config=config,
    )
    assert structurally_blocked.verdict is ReviewVerdict.BLOCKED


@pytest.mark.parametrize(
    "malformation",
    [
        "no_findings",
        "approval_without_refs",
        "missing_criterion",
        "duplicate_criterion",
        "unknown_ref",
        "forged_subject",
    ],
)
def test_hostile_sentinel_and_incomplete_or_forged_coverage_block(
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
):
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    if malformation == "no_findings":
        response = b"NO_FINDINGS"
    else:
        normal = json.loads(_response(subject, config, approved=True))
        if malformation == "approval_without_refs":
            normal["coverage"][0]["evidence_refs"] = []
        elif malformation == "missing_criterion":
            normal["coverage"].pop()
        elif malformation == "duplicate_criterion":
            normal["coverage"][-1] = normal["coverage"][0]
        elif malformation == "unknown_ref":
            normal["coverage"][0]["evidence_refs"] = [
                {"kind": "artifact", "ref": "artifact.knowledge.forged"}
            ]
        elif malformation == "forged_subject":
            normal["subject_sha256"] = "f" * 64
        response = _canonical(normal)
        if malformation == "noncanonical":
            response = json.dumps(normal, indent=2).encode("utf-8")

    with pytest.raises(SemanticReviewBlocked) as caught:
        _review(monkeypatch, subject, response, config=config)
    assert caught.value.code in {
        "semantic_review.protocol_invalid",
        "semantic_review.coverage_invalid",
        "semantic_review.subject_stale",
    }


def test_approval_must_cover_every_required_command_result(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    base = _subject(CodingMode.WRITE)
    second_output = _artifact(
        "command-output-a2",
        ArtifactKind.COMMAND_OUTPUT,
        b"1 passed: public signature remains compatible\n",
        producer="coding-verifier",
    )
    second_command = CommandResultV1(
        command_id="verify-a2",
        argv=["python", "-m", "pytest", "tests/test_public_api.py"],
        cwd=".",
        purpose="Verify the unchanged public signature.",
        status=CommandStatus.PASSED,
        exit_code=0,
        started_at=_NOW,
        finished_at=_NOW,
        duration_ms=11,
        output_artifact_id=second_output.reference.artifact_id,
        summary="Public API regression test passed.",
    )
    subject = replace(
        base,
        required_command_ids=("verify-a1", "verify-a2"),
        command_evidence=(
            *base.command_evidence,
            SemanticCommandEvidence(second_command, second_output),
        ),
    )
    # The helper cites only verify-a1. Even though every requirement entry is
    # present, approval must fail when any required command evidence is absent.
    response = _response(subject, config, approved=True)

    with pytest.raises(SemanticReviewBlocked) as caught:
        _review(monkeypatch, subject, response, config=config)

    assert caught.value.code == "semantic_review.coverage_invalid"


def test_equal_silent_outputs_from_distinct_commands_are_independent_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    config = _config()
    base = _subject(CodingMode.WRITE)
    store = ArtifactStore("semantic-silent-commands", root=tmp_path)
    first_ref = store.write_text(
        kind=ArtifactKind.COMMAND_OUTPUT,
        text="[no output]",
        producer="coding-verification",
        occurrence_id="verify-a1",
    )
    second_ref = store.write_text(
        kind=ArtifactKind.COMMAND_OUTPUT,
        text="[no output]",
        producer="coding-verification",
        occurrence_id="verify-a2",
    )
    first_result = base.command_evidence[0].result.model_copy(
        update={"output_artifact_id": first_ref.artifact_id}
    )
    second_result = CommandResultV1(
        command_id="verify-a2",
        argv=["git", "diff", "--check"],
        cwd=".",
        purpose="Verify patch whitespace.",
        status=CommandStatus.PASSED,
        exit_code=0,
        started_at=_NOW,
        finished_at=_NOW,
        duration_ms=1,
        output_artifact_id=second_ref.artifact_id,
        summary="Patch whitespace verification passed.",
    )
    subject = replace(
        base,
        required_command_ids=("verify-a1", "verify-a2"),
        command_evidence=(
            SemanticCommandEvidence(
                first_result,
                SemanticArtifactEvidence(first_ref, store.read_verified(first_ref)),
            ),
            SemanticCommandEvidence(
                second_result,
                SemanticArtifactEvidence(second_ref, store.read_verified(second_ref)),
            ),
        ),
    )
    evidence_refs = [
        {"kind": "artifact", "ref": "artifact.diff.diff-a1"},
        {"kind": "command_result", "ref": "command.verify-a1"},
        {"kind": "command_result", "ref": "command.verify-a2"},
    ]
    coverage = [
        {"evidence_refs": evidence_refs, "requirement_id": requirement_id}
        for requirement_id in _requirement_ids(subject)
    ]

    result = _review(
        monkeypatch,
        subject,
        _response(subject, config, approved=True, coverage_override=coverage),
        config=config,
    )

    assert result.verdict == "approved"
    assert first_ref.artifact_id != second_ref.artifact_id
    assert first_ref.sha256 == second_ref.sha256


def test_schema_constrained_noncanonical_model_json_is_preserved_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    value = json.loads(_response(subject, config, approved=True))
    model_response = json.dumps(value, indent=2).encode("utf-8")

    result = _review(monkeypatch, subject, model_response, config=config)

    assert result.evidence.model_response == model_response
    assert (
        result.evidence.model_response_sha256
        == hashlib.sha256(model_response).hexdigest()
    )
    assert result.evidence.canonical_response == _canonical(value)
    validate_semantic_result(result, subject, config)


def test_verbose_coding_knowledge_is_authenticated_then_projected_without_fragment_loss():
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    source = {
        "index": {
            "allowed_files": 4,
            "blocked_files": 0,
            "git_commit_sha": _BASE,
            "tracked_files": 4,
            "worktree_revision": _BINDING,
        },
        "context": {
            "degraded": False,
            "evidence": {
                "degraded": False,
                "fragments": [
                    {
                        "conflict": False,
                        "content": "CODING_FIXTURE_FACT=violet-otter-731",
                        "fragment_id": "fragment-a1",
                        "provenance": {
                            "end_line": 4,
                            "fragment_locator": "README.md:4",
                            "project_commit_sha": _BASE,
                            "source_hash": "3" * 64,
                            "source_uri": "README.md",
                            "start_line": 4,
                            "status": "active",
                            "worktree_revision": _BINDING,
                            "parser": "redundant-projection-field",
                        },
                        "source_kind": "repository_file",
                        "stale": False,
                        "title": "README.md",
                        "reason": "redundant projection field",
                    }
                ],
                "reason_code": None,
            },
            "fresh_tool_results": [],
            "goal": subject.request.goal,
            "reason_code": None,
            "repository_summary": {"entry_points": [], "tests": []},
        },
    }
    payload = _canonical(source)
    projected_subject = replace(
        subject,
        knowledge_artifact=_artifact(
            "knowledge-projected-a1",
            ArtifactKind.CONTEXT,
            payload,
            producer="coding-context",
        ),
    )

    prepared = semantic_review._prepare_subject(projected_subject, config)
    knowledge = prepared.value["knowledge_artifact"]

    assert isinstance(knowledge, dict)
    assert "payload_utf8_exact" not in knowledge
    projection = knowledge["payload_json_projection"]
    assert projection == semantic_review._knowledge_projection(source)
    assert (
        projection["context"]["evidence"]["fragments"][0]["content"]
        == "CODING_FIXTURE_FACT=violet-otter-731"
    )
    assert knowledge["sha256"] == hashlib.sha256(payload).hexdigest()


def test_raw_executor_stream_is_sha_bound_but_not_duplicated_into_model_context():
    config = _config()
    raw = b'{"type":"tool","payload":"' + (b"X" * 16_384) + b'"}'
    subject = replace(
        _subject(CodingMode.READ_ONLY),
        executor_output_artifact=_artifact(
            "executor-output-large-a1",
            ArtifactKind.COMMAND_OUTPUT,
            raw,
            producer="qwen-code",
        ),
        executor_claimed_summary="CODING_FIXTURE_FACT=violet-otter-731",
    )

    prepared = semantic_review._prepare_subject(subject, config)
    executor = prepared.value["executor_output_artifact"]

    assert isinstance(executor, dict)
    assert executor["payload_sha256_only"] is True
    assert "payload_utf8_exact" not in executor
    assert executor["sha256"] == hashlib.sha256(raw).hexdigest()
    assert raw not in prepared.canonical_bytes
    assert "artifact.executor_output" not in prepared.allowlist


def test_ui_evidence_can_back_a_required_semantic_command(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.WRITE)
    current = subject.command_evidence[0]
    ui_output = _artifact(
        "ui-evidence-a1",
        ArtifactKind.UI_EVIDENCE,
        _canonical({"status": "passed", "matched": True}),
        producer="coding-playwright",
    )
    command = current.result.model_copy(
        update={"output_artifact_id": ui_output.reference.artifact_id}
    )
    ui_subject = replace(
        subject,
        required_command_ids=(command.command_id,),
        command_evidence=(SemanticCommandEvidence(command, ui_output),),
    )

    result = _review(
        monkeypatch,
        ui_subject,
        _response(ui_subject, config, approved=True),
        config=config,
    )

    assert result.verdict == "approved"
    validate_semantic_result(result, ui_subject, config)


def test_forged_result_dataclass_and_stale_artifact_cannot_merge(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.WRITE)
    result = _review(
        monkeypatch,
        subject,
        _response(subject, config, approved=True),
        config=config,
    )
    forged = replace(result, coverage=result.coverage[:-1])
    merged = merge_local_semantic_review(
        _deterministic(),
        subject=subject,
        semantic_result=forged,
        evidence_artifact=_persisted_evidence(result),
        worktree_unchanged=True,
        semantic_config=config,
    )
    assert merged.verdict is ReviewVerdict.BLOCKED
    assert merged.findings[0].code == "local_semantic.result_invalid"

    artifact = _persisted_evidence(result)
    stale_artifact = artifact.model_copy(update={"sha256": "a" * 64})
    merged = merge_local_semantic_review(
        _deterministic(),
        subject=subject,
        semantic_result=result,
        evidence_artifact=stale_artifact,
        worktree_unchanged=True,
        semantic_config=config,
    )
    assert merged.verdict is ReviewVerdict.BLOCKED
    assert merged.findings[0].code == "local_semantic.evidence_binding_invalid"


def test_subject_rejects_stale_attempt_diff_command_and_output(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.WRITE)
    response = _response(subject, config, approved=True)

    stale_diff_ref = subject.diff_artifact.reference.model_copy(
        update={"sha256": "0" * 64}
    )
    stale_diff = replace(
        subject,
        diff_artifact=SemanticArtifactEvidence(
            stale_diff_ref,
            subject.diff_artifact.payload,
        ),
    )
    with pytest.raises(SemanticReviewBlocked, match="digest"):
        _review(monkeypatch, stale_diff, response, config=config)

    stale_attempt = replace(subject, attempt_index=2)
    with pytest.raises(SemanticReviewBlocked) as stale_response:
        _review(monkeypatch, stale_attempt, response, config=config)
    assert stale_response.value.code == "semantic_review.subject_stale"

    missing_command = replace(subject, required_command_ids=("different-command",))
    with pytest.raises(SemanticReviewBlocked) as missing:
        _review(monkeypatch, missing_command, response, config=config)
    assert missing.value.code == "semantic_review.subject_stale"

    command = subject.command_evidence[0]
    stale_output_ref = command.output_artifact.reference.model_copy(
        update={"sha256": "0" * 64}
    )
    stale_output = replace(
        subject,
        command_evidence=(
            replace(
                command,
                output_artifact=SemanticArtifactEvidence(
                    stale_output_ref,
                    command.output_artifact.payload,
                ),
            ),
        ),
    )
    with pytest.raises(SemanticReviewBlocked):
        _review(monkeypatch, stale_output, response, config=config)

    stale_executor_ref = subject.executor_output_artifact.reference.model_copy(
        update={"sha256": "0" * 64}
    )
    stale_executor_output = replace(
        subject,
        executor_output_artifact=SemanticArtifactEvidence(
            stale_executor_ref,
            subject.executor_output_artifact.payload,
        ),
    )
    with pytest.raises(SemanticReviewBlocked):
        _review(monkeypatch, stale_executor_output, response, config=config)


def test_engine_invariant_is_checked_before_and_after_inference(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    response = _response(subject, config, approved=True)
    calls = 0

    def invariant(_subject: SemanticReviewSubject) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("attempt changed")

    with pytest.raises(SemanticReviewBlocked) as caught:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            invariant=invariant,
        )
    assert caught.value.code == "semantic_review.subject_stale"
    assert calls == 3


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434/v1/chat/completions",
        "http://localhost:11434/v1/chat/completions",
        "http://127.0.0.2:11434/v1/chat/completions",
        "http://127.0.0.1:11435/v1/chat/completions",
        "http://user@127.0.0.1:11434/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions/",
    ],
)
def test_endpoint_is_exact_numeric_loopback(endpoint: str):
    with pytest.raises(ValueError, match="loopback"):
        _config(endpoint=endpoint)


def test_listener_spoof_restart_retag_and_api_model_mismatch_block(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.READ_ONLY)
    response = _response(subject, config, approved=True)

    spoof = replace(_attestation(), executable_sha256="a" * 64)
    with pytest.raises(SemanticReviewBlocked) as spoofed:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            attestor=_Attestor(spoof),
        )
    assert spoofed.value.code == "semantic_review.attestation_mismatch"

    with pytest.raises(SemanticReviewBlocked) as restarted:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            attestor=_Attestor(_attestation(pid=10), _attestation(pid=11)),
        )
    assert restarted.value.code == "semantic_review.attestation_changed"

    retagged = replace(_attestation(), model_digest="b" * 64)
    with pytest.raises(SemanticReviewBlocked) as retag:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            attestor=_Attestor(retagged),
        )
    assert retag.value.code == "semantic_review.attestation_mismatch"

    _install_api(
        monkeypatch,
        lambda _request: _FakeResponse(
            _api_body(response, model="forged-local-strong")
        ),
    )
    with pytest.raises(SemanticReviewBlocked) as model:
        LocalSemanticReviewer(config, attestor=_Attestor()).review(
            subject,
            assert_subject_current=lambda _subject: None,
        )
    assert model.value.code == "semantic_review.model_mismatch"


def test_overall_deadline_and_caller_cancellation_cover_slow_stream(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(timeout_seconds=0.05, deadline_poll_seconds=0.01)
    subject = _subject(CodingMode.READ_ONLY)
    response = _response(subject, config, approved=True)

    def slow_chunks():
        time.sleep(0.2)
        yield _api_body(response)

    started = time.monotonic()
    with pytest.raises(SemanticReviewBlocked) as deadline:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            api_response=_FakeResponse(b"", declared_length=0, chunks=slow_chunks),
        )
    assert deadline.value.code == "semantic_review.deadline_exceeded"
    assert time.monotonic() - started < 0.18

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(SemanticReviewBlocked) as cancellation:
        _review(
            monkeypatch,
            subject,
            response,
            config=_config(),
            cancel_event=cancelled,
        )
    assert cancellation.value.code == "semantic_review.cancelled"


def test_caller_deadline_cannot_be_reset_or_expand_reviewer_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(timeout_seconds=1.0)
    subject = _subject(CodingMode.READ_ONLY)
    called = False

    def response_factory(_request: bytes) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(b"{}")

    _install_api(monkeypatch, response_factory)
    reviewer = LocalSemanticReviewer(config, attestor=_Attestor())

    with pytest.raises(SemanticReviewBlocked) as expired:
        reviewer.review(
            subject,
            assert_subject_current=lambda _subject: None,
            deadline=time.monotonic() - 0.01,
        )
    assert expired.value.code == "semantic_review.deadline_exceeded"
    assert called is False

    with pytest.raises(SemanticReviewBlocked) as expanded:
        reviewer.review(
            subject,
            assert_subject_current=lambda _subject: None,
            deadline=time.monotonic() + 10.0,
        )
    assert expanded.value.code == "semantic_review.deadline_invalid"
    assert called is False


def test_declared_oversize_blocks_before_body_iteration(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(max_response_bytes=2_048, max_canonical_response_bytes=1_024)
    subject = _subject(CodingMode.READ_ONLY)
    response = _response(subject, config, approved=True)
    fake = _FakeResponse(_api_body(response), declared_length=4_096)
    with pytest.raises(SemanticReviewBlocked) as caught:
        _review(
            monkeypatch,
            subject,
            response,
            config=config,
            api_response=fake,
        )
    assert caught.value.code == "semantic_review.response_oversize"
    assert fake.iterated is False


def test_exact_subject_fails_before_http_when_32k_context_would_overflow(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config()
    subject = _subject(CodingMode.WRITE, diff=b"+" + b"x" * 40_000)
    called = False

    def response_factory(_request: bytes) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(b"{}")

    _install_api(monkeypatch, response_factory)
    with pytest.raises(SemanticReviewBlocked) as caught:
        LocalSemanticReviewer(config, attestor=_Attestor()).review(
            subject,
            assert_subject_current=lambda _subject: None,
        )
    assert caught.value.code == "semantic_review.context_overflow"
    assert called is False


def test_oversized_api_string_field_is_rejected_by_raw_preflight():
    config = _config(
        max_response_bytes=8_192,
        max_canonical_response_bytes=1_024,
    )
    raw = _api_body(b"x" * 2_000, reasoning=None)
    with pytest.raises(SemanticReviewBlocked) as caught:
        semantic_review._parse_api_response(raw, config)
    assert caught.value.code == "semantic_review.response_oversize"
