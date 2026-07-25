from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import ArtifactKind, ArtifactReferenceV1, CodingTaskRequestV1
from services.knowledge.contracts import ContextEnvelopeV1
from services.knowledge.engine import KnowledgeEngine


@dataclass(frozen=True, slots=True)
class CodingContext:
    envelope: ContextEnvelopeV1
    artifact: ArtifactReferenceV1
    index_result: dict[str, object]


class CodingContextBuilder:
    def __init__(
        self,
        *,
        knowledge_engine: KnowledgeEngine | None = None,
        policy: CodingPolicy | None = None,
    ) -> None:
        self.knowledge_engine = knowledge_engine or KnowledgeEngine()
        self.policy = policy or get_coding_policy()

    def build(
        self,
        *,
        request: CodingTaskRequestV1,
        repository: Path,
        artifact_store: ArtifactStore,
        modified_files: tuple[str, ...] = (),
        unresolved_errors: tuple[str, ...] = (),
    ) -> CodingContext:
        registration = self.knowledge_engine.registration(
            str(repository),
            owner_id="local-user",
            consent=True,
        ).model_copy(
            update={"sensitivity_ceiling": request.permissions.data_classification.value}
        )
        index_result = self.knowledge_engine.index_repository(registration)
        envelope = self.knowledge_engine.build_context(
            project_path=str(repository),
            goal=request.goal[:2_048],
            token_budget=self.policy.context_token_budget,
            constraints=request.constraints,
            modified_files=modified_files,
            unresolved_errors=unresolved_errors,
            verification_plan=request.verification_plan,
        )
        artifact = artifact_store.write_json(
            kind=ArtifactKind.CONTEXT,
            value={
                "index": {
                    "tracked_files": index_result.get("tracked_files"),
                    "allowed_files": index_result.get("allowed_files"),
                    "blocked_files": index_result.get("blocked_files"),
                    "git_commit_sha": index_result.get("git_commit_sha"),
                    "worktree_revision": index_result.get("worktree_revision"),
                },
                "context": envelope.model_dump(mode="json"),
            },
            producer="coding-context",
        )
        return CodingContext(envelope=envelope, artifact=artifact, index_result=index_result)


__all__ = ["CodingContext", "CodingContextBuilder"]
