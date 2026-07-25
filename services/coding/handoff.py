from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from services.coding.artifacts import ArtifactPolicyError, ArtifactStore
from services.coding.contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    CodingTaskStateV1,
    CommandResultV1,
    ExecutionAttemptV1,
    RuleReferenceV1,
    StrictCodingModel,
)
from services.coding.git import (
    git_diff,
    git_ignored_paths,
    git_status_paths,
    resolve_repository,
    run_git,
    scan_changed_content,
    worktree_fingerprint,
)
from services.coding.worktrees import WorktreeManager


class HandoffPolicyError(RuntimeError):
    pass


class CodingHandoffV1(StrictCodingModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    request_id: str
    goal: str
    repository: str
    source_base_commit: str
    source_dirty_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    branch: str
    worktree_path: str
    applicable_rules: list[RuleReferenceV1]
    constraints: list[str]
    acceptance_criteria: list[str]
    route_reasons: list[str]
    expected_diff_paths: list[str] = Field(default_factory=list, max_length=10_000)
    forbidden_diff_paths: list[str] = Field(default_factory=list, max_length=10_000)
    inspected_files: list[str]
    modified_files: list[str]
    diff_artifact_id: str | None
    diff_artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    worktree_diff_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    worktree_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    worktree_status_paths: list[str] = Field(max_length=10_000)
    commands: list[CommandResultV1]
    attempts: list[ExecutionAttemptV1]
    artifacts: list[ArtifactReferenceV1]
    unresolved_questions: list[str]
    verification_plan: list[str]
    resume_anchor_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class HandoffBundle:
    contract: CodingHandoffV1
    json_artifact: ArtifactReferenceV1
    markdown_artifact: ArtifactReferenceV1


def _resume_anchor(state: CodingTaskStateV1) -> str:
    if state.worktree is None:
        raise HandoffPolicyError("handoff requires an isolated worktree")
    payload = {
        "task_id": state.request.task_id,
        "request_id": state.request.request_id,
        "repository": state.source_repository,
        "base_commit": state.worktree.base_commit,
        "branch": state.worktree.branch,
        "worktree_path": state.worktree.worktree_path,
        "owner_token_hash": state.worktree.owner_token_hash,
        "rules": [item.model_dump(mode="json") for item in state.applicable_rules],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HandoffManager:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        worktree_manager: WorktreeManager,
    ) -> None:
        self.artifact_store = artifact_store
        self.worktree_manager = worktree_manager

    def create(
        self,
        state: CodingTaskStateV1,
        *,
        source_dirty_fingerprint: str,
        diff_artifact_id: str | None,
    ) -> HandoffBundle:
        worktree = state.worktree
        if worktree is None or worktree.branch is None:
            raise HandoffPolicyError("handoff requires a registered task branch/worktree")
        target = Path(worktree.worktree_path).resolve(strict=True)
        if git_ignored_paths(target):
            raise HandoffPolicyError(
                "handoff worktree contains untracked ignored files"
            )
        scan_changed_content(
            target, max_bytes=self.artifact_store.policy.max_diff_bytes
        )
        current_diff = git_diff(
            target,
            max_bytes=self.artifact_store.policy.max_diff_bytes,
        )
        current_diff_sha256 = hashlib.sha256(current_diff).hexdigest()
        current_status = git_status_paths(target)
        diff_reference = next(
            (
                item
                for item in state.artifacts
                if item.artifact_id == diff_artifact_id
                and item.kind is ArtifactKind.DIFF
            ),
            None,
        )
        if diff_artifact_id is None:
            if current_diff:
                raise HandoffPolicyError(
                    "handoff worktree has a diff without its canonical artifact"
                )
        else:
            try:
                diff_payload = (
                    self.artifact_store.read_verified(diff_reference)
                    if diff_reference is not None
                    else None
                )
            except ArtifactPolicyError as exc:
                raise HandoffPolicyError(
                    "handoff diff artifact does not match the current worktree"
                ) from exc
            if (
                diff_reference is None
                or diff_payload != current_diff
                or diff_reference.sha256 != current_diff_sha256
            ):
                raise HandoffPolicyError(
                    "handoff diff artifact does not match the current worktree"
                )
        contract = CodingHandoffV1(
            task_id=state.request.task_id,
            request_id=state.request.request_id,
            goal=state.request.goal,
            repository=state.source_repository,
            source_base_commit=worktree.base_commit,
            source_dirty_fingerprint=source_dirty_fingerprint,
            branch=worktree.branch,
            worktree_path=worktree.worktree_path,
            applicable_rules=state.applicable_rules,
            constraints=state.request.constraints,
            acceptance_criteria=state.request.acceptance_criteria,
            route_reasons=state.request.route_reasons,
            expected_diff_paths=state.request.expected_diff_paths,
            forbidden_diff_paths=state.request.forbidden_diff_paths,
            inspected_files=state.inspected_files,
            modified_files=state.modified_files,
            diff_artifact_id=diff_artifact_id,
            diff_artifact_sha256=(
                diff_reference.sha256 if diff_reference is not None else None
            ),
            worktree_diff_sha256=current_diff_sha256,
            worktree_fingerprint_sha256=worktree_fingerprint(
                target,
                include_ignored=True,
            ),
            worktree_status_paths=current_status,
            commands=state.command_results,
            attempts=state.attempts,
            artifacts=state.artifacts,
            unresolved_questions=state.unresolved_errors,
            verification_plan=state.request.verification_plan,
            resume_anchor_sha256=_resume_anchor(state),
        )
        json_artifact = self.artifact_store.write_json(
            kind=ArtifactKind.HANDOFF,
            value=contract.model_dump(mode="json"),
            producer="coding-handoff",
        )
        markdown = self._markdown(contract, json_artifact)
        markdown_artifact = self.artifact_store.write_text(
            kind=ArtifactKind.HANDOFF,
            text=markdown,
            suffix=".md",
            producer="coding-handoff",
        )
        return HandoffBundle(contract, json_artifact, markdown_artifact)

    @staticmethod
    def _markdown(contract: CodingHandoffV1, canonical: ArtifactReferenceV1) -> str:
        commands = "\n".join(
            f"- `{item.command_id}`: {item.status.value} — {item.summary} (artifact `{item.output_artifact_id or 'none'}`)"
            for item in contract.commands
        ) or "- none"
        attempts = "\n".join(
            f"- {item.index}. {item.executor.value}: {item.status.value} — {item.error_summary or item.strategy}"
            for item in contract.attempts
        ) or "- none"
        return (
            "# Resumable Codex coding handoff\n\n"
            f"Canonical JSON artifact: `{canonical.artifact_id}`\n\n"
            f"Task/request: `{contract.task_id}` / `{contract.request_id}`\n\n"
            f"Repository: `{contract.repository}`\n\n"
            f"Base/branch/worktree: `{contract.source_base_commit}` / `{contract.branch}` / `{contract.worktree_path}`\n\n"
            f"Goal: {contract.goal}\n\n"
            "## Constraints\n\n" + ("\n".join(f"- {item}" for item in contract.constraints) or "- none") + "\n\n"
            "## Acceptance criteria\n\n" + ("\n".join(f"- {item}" for item in contract.acceptance_criteria) or "- none") + "\n\n"
            "## Route reasons\n\n" + ("\n".join(f"- {item}" for item in contract.route_reasons) or "- none") + "\n\n"
            "## Expected diff paths\n\n"
            + ("\n".join(f"- `{item}`" for item in contract.expected_diff_paths) or "- unrestricted")
            + "\n\n"
            "## Forbidden diff paths\n\n"
            + ("\n".join(f"- `{item}`" for item in contract.forbidden_diff_paths) or "- none")
            + "\n\n"
            "## Attempts\n\n" + attempts + "\n\n"
            "## Commands\n\n" + commands + "\n\n"
            "## Modified files\n\n" + ("\n".join(f"- `{item}`" for item in contract.modified_files) or "- none") + "\n\n"
            f"Diff artifact: `{contract.diff_artifact_id or 'none'}`\n\n"
            "## Unresolved questions/errors\n\n"
            + ("\n".join(f"- {item}" for item in contract.unresolved_questions) or "- none")
            + "\n"
        )

    def load_and_validate(
        self,
        state: CodingTaskStateV1,
        artifact: ArtifactReferenceV1,
    ) -> CodingHandoffV1:
        if artifact.kind is not ArtifactKind.HANDOFF or artifact.media_type != "application/json":
            raise HandoffPolicyError("resume artifact is not a canonical JSON handoff")
        try:
            # Parse the exact bytes authenticated by the same open handle.
            payload = self.artifact_store.read_verified(artifact)
            contract = CodingHandoffV1.model_validate_json(payload)
        except ArtifactPolicyError as exc:
            raise HandoffPolicyError(
                "handoff artifact hash/ownership validation failed"
            ) from exc
        except ValueError as exc:
            raise HandoffPolicyError("handoff artifact is invalid") from exc
        if contract.task_id != state.request.task_id or contract.request_id != state.request.request_id:
            raise HandoffPolicyError("handoff identity does not match task state")
        if contract.resume_anchor_sha256 != _resume_anchor(state):
            raise HandoffPolicyError("handoff resume anchor no longer matches task scope")
        if contract.repository != state.source_repository:
            raise HandoffPolicyError("handoff repository scope changed")
        registered = self.worktree_manager.load(contract.task_id)
        identity_fields = (
            "task_id",
            "source_repository",
            "worktree_path",
            "branch",
            "git_dir",
            "git_common_dir",
            "git_marker_sha256",
            "base_commit",
            "owner_token_hash",
        )
        if (
            registered is None
            or state.worktree is None
            or any(getattr(registered, field) != getattr(state.worktree, field) for field in identity_fields)
        ):
            raise HandoffPolicyError("registered worktree changed since handoff")

        # Exclude only exact active refs proven by the owned-worktree registry.
        # The branch namespace itself is never treated as ownership evidence.
        exclusions = tuple(
            sorted(
                set(self.worktree_manager.active_owned_branch_refs()).union(
                    {f"refs/heads/{contract.branch}"}
                )
            )
        )
        source = resolve_repository(contract.repository, excluded_refs=exclusions)
        if source.base_commit != contract.source_base_commit:
            raise HandoffPolicyError("source HEAD changed since handoff")
        if source.dirty_fingerprint != contract.source_dirty_fingerprint:
            raise HandoffPolicyError("source dirty work changed since handoff")
        self.validate_current_worktree(contract)
        return contract

    def validate_current_worktree(self, contract: CodingHandoffV1) -> None:
        """Recheck all mutable resume inputs immediately before execution."""

        target = Path(contract.worktree_path).resolve(strict=True)
        if git_ignored_paths(target):
            raise HandoffPolicyError(
                "handoff worktree contains untracked ignored files"
            )
        scan_changed_content(
            target, max_bytes=self.artifact_store.policy.max_diff_bytes
        )
        # Bracket the independently collected values with the comprehensive
        # fingerprint so a writer cannot leave a mixed status/diff observation.
        stable: tuple[bytes, list[str], bytes, str] | None = None
        for _ in range(3):
            before = worktree_fingerprint(target, include_ignored=True)
            head = run_git(
                target,
                ["rev-parse", "--verify", "HEAD"],
                max_output_bytes=16_384,
            ).stdout
            current_status = git_status_paths(target)
            current_diff = git_diff(
                target,
                max_bytes=self.artifact_store.policy.max_diff_bytes,
            )
            after = worktree_fingerprint(target, include_ignored=True)
            if before == after:
                stable = (head, current_status, current_diff, after)
                break
        if stable is None:
            raise HandoffPolicyError("handoff worktree changed during validation")
        head, current_status, current_diff, current_fingerprint = stable
        if head.decode("ascii", errors="strict").strip().casefold() != contract.source_base_commit:
            raise HandoffPolicyError("executor committed in the handoff worktree")
        if current_status != contract.worktree_status_paths:
            raise HandoffPolicyError("handoff worktree status changed after bundle creation")
        current_diff_sha256 = hashlib.sha256(current_diff).hexdigest()
        if current_diff_sha256 != contract.worktree_diff_sha256:
            raise HandoffPolicyError("handoff worktree diff changed after bundle creation")
        if current_fingerprint != contract.worktree_fingerprint_sha256:
            raise HandoffPolicyError(
                "handoff worktree fingerprint changed after bundle creation"
            )

        referenced_payloads: dict[str, bytes] = {}
        try:
            for referenced in contract.artifacts:
                if referenced.artifact_id in referenced_payloads:
                    raise HandoffPolicyError(
                        "handoff contains a duplicate artifact identity"
                    )
                referenced_payloads[referenced.artifact_id] = (
                    self.artifact_store.read_verified(referenced)
                )
        except ArtifactPolicyError as exc:
            raise HandoffPolicyError(
                "handoff references a missing or modified artifact"
            ) from exc
        diff_reference = next(
            (
                item
                for item in contract.artifacts
                if item.artifact_id == contract.diff_artifact_id
                and item.kind is ArtifactKind.DIFF
            ),
            None,
        )
        if contract.diff_artifact_id is None:
            if contract.diff_artifact_sha256 is not None or current_diff:
                raise HandoffPolicyError("handoff diff artifact binding is inconsistent")
        elif (
            diff_reference is None
            or contract.diff_artifact_sha256 is None
            or diff_reference.sha256 != contract.diff_artifact_sha256
            or contract.diff_artifact_sha256 != current_diff_sha256
            or referenced_payloads.get(diff_reference.artifact_id) != current_diff
        ):
            raise HandoffPolicyError("handoff diff artifact binding is invalid")
        for rule in contract.applicable_rules:
            path = Path(rule.path)
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise HandoffPolicyError("applicable rule became unavailable") from exc
            if hashlib.sha256(payload).hexdigest() != rule.sha256:
                raise HandoffPolicyError("applicable rule changed since handoff")


__all__ = [
    "CodingHandoffV1",
    "HandoffBundle",
    "HandoffManager",
    "HandoffPolicyError",
]
