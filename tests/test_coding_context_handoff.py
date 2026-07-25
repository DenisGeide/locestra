from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.coding.artifacts import ArtifactStore
from services.coding.config import load_coding_policy
from services.coding.context import CodingContextBuilder
from services.coding.contracts import (
    ArtifactKind,
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskStateV1,
    CodingTaskStatus,
    DataClassification,
    RuleReferenceV1,
)
from services.coding.git import applicable_agent_rules, git_diff, resolve_repository
from services.coding.handoff import CodingHandoffV1, HandoffManager, HandoffPolicyError
from services.coding.worktrees import WorktreeManager
from services.knowledge.contracts import (
    ContextEnvelopeV1,
    RetrievalResultV1,
    SourceRegistrationV1,
)
from tests.coding_fixtures import coding_fixture


ROOT = Path(__file__).resolve().parents[1]


def test_handoff_contract_accepts_legacy_payload_without_diff_scope_fields():
    digest = "0" * 64
    contract = CodingHandoffV1.model_validate(
        {
            "task_id": "legacy-task",
            "request_id": "legacy-request",
            "goal": "Resume a legacy bounded coding task.",
            "repository": "C:/fixture",
            "source_base_commit": digest,
            "source_dirty_fingerprint": digest,
            "branch": "ai-task-legacy",
            "worktree_path": "C:/fixture-worktree",
            "applicable_rules": [],
            "constraints": [],
            "acceptance_criteria": ["The legacy task remains loadable."],
            "route_reasons": [],
            "inspected_files": [],
            "modified_files": [],
            "diff_artifact_id": None,
            "worktree_diff_sha256": digest,
            "worktree_fingerprint_sha256": digest,
            "worktree_status_paths": [],
            "commands": [],
            "attempts": [],
            "artifacts": [],
            "unresolved_questions": [],
            "verification_plan": ["Validate the legacy contract."],
            "resume_anchor_sha256": digest,
        }
    )

    assert contract.expected_diff_paths == []
    assert contract.forbidden_diff_paths == []


def _request(
    repository: Path,
    *,
    task_id: str,
    classification: DataClassification = DataClassification.INTERNAL,
) -> CodingTaskRequestV1:
    return CodingTaskRequestV1(
        task_id=task_id,
        request_id=f"request-{task_id}",
        goal="Correct the calculator using the smallest bounded change.",
        repository_path=str(repository),
        mode=CodingMode.WRITE,
        risk=CodingRisk.MEDIUM,
        constraints=["Do not push or install dependencies."],
        acceptance_criteria=["The calculator returns the exact sum."],
        verification_plan=["Run the standard-library calculator test."],
        permissions=CodingPermissionsV1(
            modify_files=True,
            data_classification=classification,
        ),
        route_reasons=["Synthetic coding fixture."],
        expected_diff_paths=["src/calculator.py"],
        forbidden_diff_paths=["README.md"],
    )


def _envelope(
    *,
    project_path: str,
    goal: str,
    token_budget: int,
    constraints: list[str],
    modified_files: tuple[str, ...],
    unresolved_errors: tuple[str, ...],
    verification_plan: list[str],
) -> ContextEnvelopeV1:
    evidence = RetrievalResultV1(
        project_path=project_path,
        query=goal,
        token_budget=token_budget,
        estimated_tokens=0,
        fragments=[],
    )
    return ContextEnvelopeV1(
        project_path=project_path,
        goal=goal,
        constraints=list(constraints),
        modified_files=list(modified_files),
        unresolved_errors=list(unresolved_errors),
        verification_plan=list(verification_plan),
        repository_summary={"entry_points": ["src/calculator.py"]},
        evidence=evidence,
        token_budget=token_budget,
        estimated_tokens=32,
    )


class _FakeKnowledgeEngine:
    def __init__(self) -> None:
        self.index_registration: SourceRegistrationV1 | None = None
        self.context_kwargs: dict[str, object] | None = None

    def registration(
        self,
        project_path: str,
        *,
        owner_id: str,
        consent: bool,
    ) -> SourceRegistrationV1:
        return SourceRegistrationV1(
            project_path=project_path,
            owner_id=owner_id,
            consent=consent,
        )

    def index_repository(self, registration: SourceRegistrationV1) -> dict[str, object]:
        self.index_registration = registration
        return {
            "tracked_files": 8,
            "allowed_files": 8,
            "blocked_files": 0,
            "git_commit_sha": "a" * 40,
            "worktree_revision": "b" * 64,
            "private_internal_detail": "must not be serialized",
        }

    def build_context(self, **kwargs: object) -> ContextEnvelopeV1:
        self.context_kwargs = dict(kwargs)
        return _envelope(
            project_path=str(kwargs["project_path"]),
            goal=str(kwargs["goal"]),
            token_budget=int(kwargs["token_budget"]),
            constraints=list(kwargs["constraints"]),
            modified_files=tuple(kwargs["modified_files"]),
            unresolved_errors=tuple(kwargs["unresolved_errors"]),
            verification_plan=list(kwargs["verification_plan"]),
        )


def test_context_builder_propagates_task_evidence_budget_and_classification():
    with coding_fixture(run_id="context-builder") as fixture:
        request = _request(
            fixture.repository,
            task_id="context-task",
            classification=DataClassification.PUBLIC,
        )
        knowledge = _FakeKnowledgeEngine()
        policy = load_coding_policy(ROOT / "config" / "coding.json")
        artifacts = ArtifactStore(
            request.task_id,
            root=fixture.root / "context-artifacts",
            policy=policy,
        )
        builder = CodingContextBuilder(knowledge_engine=knowledge, policy=policy)

        context = builder.build(
            request=request,
            repository=fixture.repository,
            artifact_store=artifacts,
            modified_files=("src/calculator.py",),
            unresolved_errors=("The first local attempt failed its test.",),
        )

        assert knowledge.index_registration is not None
        assert knowledge.index_registration.consent is True
        assert knowledge.index_registration.owner_id == "local-user"
        assert knowledge.index_registration.sensitivity_ceiling == "public"
        assert knowledge.context_kwargs == {
            "project_path": str(fixture.repository),
            "goal": request.goal,
            "token_budget": policy.context_token_budget,
            "constraints": request.constraints,
            "modified_files": ("src/calculator.py",),
            "unresolved_errors": ("The first local attempt failed its test.",),
            "verification_plan": request.verification_plan,
        }
        assert context.envelope.modified_files == ["src/calculator.py"]
        assert context.envelope.unresolved_errors == [
            "The first local attempt failed its test."
        ]
        assert context.artifact.kind is ArtifactKind.CONTEXT
        payload = json.loads(Path(context.artifact.path).read_text(encoding="utf-8"))
        assert payload["index"] == {
            "allowed_files": 8,
            "blocked_files": 0,
            "git_commit_sha": "a" * 40,
            "tracked_files": 8,
            "worktree_revision": "b" * 64,
        }
        assert "private_internal_detail" not in payload["index"]
        assert artifacts.verify(context.artifact) is True


def _rules(repository: Path) -> list[RuleReferenceV1]:
    return [
        RuleReferenceV1(
            path=str(path.resolve(strict=True)),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            scope=(
                path.parent.relative_to(repository).as_posix()
                if path.parent != repository
                else "repository-root"
            ),
        )
        for path in applicable_agent_rules(repository, ["src/calculator.py"])
    ]


def test_handoff_bundle_is_complete_resumable_and_hash_validated():
    with coding_fixture(run_id="handoff-contract") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = WorktreeManager(
            registry_root=fixture.root / "handoff-registry",
            owned_worktree_root=fixture.root / "handoff-worktrees",
        )
        record = manager.create(task_id="handoff-task", repository=identity)
        worktree = Path(record.worktree_path)
        target = worktree / "src" / "calculator.py"
        original = target.read_bytes()
        target.write_bytes(
            original.replace(b"return left - right", b"return left + right")
        )
        handoff_bytes = target.read_bytes()
        artifacts = ArtifactStore(
            "handoff-task",
            root=fixture.root / "handoff-artifacts",
        )
        diff = artifacts.write_bytes(
            kind=ArtifactKind.DIFF,
            payload=git_diff(worktree, max_bytes=1_048_576),
            suffix=".diff",
            media_type="text/x-diff",
            producer="test",
        )
        request = _request(fixture.repository, task_id="handoff-task")
        now = datetime.now(timezone.utc)
        state = CodingTaskStateV1(
            request=request,
            status=CodingTaskStatus.EXECUTING,
            source_repository=str(fixture.repository),
            worktree=record,
            applicable_rules=_rules(fixture.repository),
            inspected_files=["README.md", "src/calculator.py"],
            artifacts=[diff],
            modified_files=["src/calculator.py"],
            unresolved_errors=["Two bounded local attempts failed."],
            created_at=now,
            updated_at=now,
        )
        handoff_manager = HandoffManager(
            artifact_store=artifacts,
            worktree_manager=manager,
        )

        bundle = handoff_manager.create(
            state,
            source_dirty_fingerprint=identity.dirty_fingerprint,
            diff_artifact_id=diff.artifact_id,
        )
        loaded = handoff_manager.load_and_validate(state, bundle.json_artifact)

        assert loaded.task_id == request.task_id
        assert loaded.request_id == request.request_id
        assert loaded.repository == str(fixture.repository)
        assert loaded.source_base_commit == fixture.baseline_sha
        assert loaded.source_dirty_fingerprint == identity.dirty_fingerprint
        assert loaded.branch == record.branch
        assert loaded.worktree_path == record.worktree_path
        assert [Path(item.path).name for item in loaded.applicable_rules] == [
            "AGENTS.md",
            "AGENTS.md",
        ]
        assert loaded.modified_files == ["src/calculator.py"]
        assert loaded.expected_diff_paths == request.expected_diff_paths
        assert loaded.forbidden_diff_paths == request.forbidden_diff_paths
        assert loaded.diff_artifact_id == diff.artifact_id
        assert loaded.unresolved_questions == ["Two bounded local attempts failed."]
        assert loaded.verification_plan == request.verification_plan
        assert loaded.artifacts == [diff]
        assert loaded.diff_artifact_sha256 == diff.sha256
        assert loaded.worktree_diff_sha256 == diff.sha256
        assert loaded.worktree_status_paths == ["src/calculator.py"]
        assert len(loaded.worktree_fingerprint_sha256) == 64
        assert len(loaded.resume_anchor_sha256) == 64
        assert artifacts.verify(bundle.json_artifact) is True
        assert artifacts.verify(bundle.markdown_artifact) is True

        target.write_bytes(handoff_bytes + b"\nPOST_HANDOFF_TAMPER")
        with pytest.raises(HandoffPolicyError, match="worktree (diff|fingerprint)"):
            handoff_manager.load_and_validate(state, bundle.json_artifact)
        target.write_bytes(handoff_bytes)

        Path(bundle.json_artifact.path).write_bytes(
            Path(bundle.json_artifact.path).read_bytes() + b"\n"
        )
        with pytest.raises(HandoffPolicyError, match="hash/ownership"):
            handoff_manager.load_and_validate(state, bundle.json_artifact)

        target.write_bytes(original)
        manager.complete(record.task_id)
        removed = manager.cleanup(record.task_id)
        assert removed.status == "removed"
