from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from services.knowledge.config import KnowledgePolicy, load_knowledge_policy
from services.knowledge.contracts import (
    ContextEnvelopeV1,
    BlockedRepositorySourceV1,
    FreshnessRequirement,
    ImportRequestV1,
    ImportResultV1,
    RepositoryMapV1,
    RetrievalRequestV1,
    RetrievalResultV1,
    SourceKind,
    SourceRegistrationV1,
    SourceStatus,
)
from services.knowledge.parsers import PARSER_VERSION, extract_facts, parse_source
from services.knowledge.privacy import (
    KnowledgePolicyError,
    canonical_project,
    detect_secret,
    read_registered_source,
    reject_secret_text,
)
from services.knowledge.repository import (
    PreparedSource,
    RepositoryError,
    build_repository_map,
    git_commit,
    git_changed_paths,
    git_history_payload,
    git_remote,
    git_worktree_object_ids,
    infer_source_kind,
    prepare_tracked_source,
    ripgrep_allowed_files,
    tracked_source_observation,
    tracked_files,
    worktree_revision,
)
from services.knowledge.store import (
    KnowledgeStore,
    KnowledgeStoreError,
    conservative_token_estimate,
)


class KnowledgeEngine:
    def __init__(
        self,
        store: KnowledgeStore | None = None,
        *,
        policy: KnowledgePolicy | None = None,
        memory_store: object | None = None,
    ) -> None:
        self._store_was_injected = store is not None
        self.store = store or KnowledgeStore()
        self.policy = policy or load_knowledge_policy()
        self._memory_store = memory_store

    @staticmethod
    def registration(project_path: str, *, owner_id: str = "local-user", consent: bool = False) -> SourceRegistrationV1:
        return SourceRegistrationV1(owner_id=owner_id, project_path=project_path, consent=consent)

    @staticmethod
    def _validate_registration_metadata(registration: SourceRegistrationV1) -> None:
        reject_secret_text(registration.owner_id)
        reject_secret_text(registration.adapter_version)

    @staticmethod
    def _safe_blocked_path(path: str) -> str:
        try:
            reject_secret_text(path)
            return path
        except KnowledgePolicyError:
            return f"blocked:{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}"

    def _memory_target(self, *, required: bool) -> object | None:
        if self._memory_store is not None:
            return self._memory_store
        if self._store_was_injected:
            if required:
                raise KnowledgeStoreError(
                    "an explicit memory store is required with an injected knowledge store"
                )
            return None
        from services.memory.store import MemoryStore

        self._memory_store = MemoryStore()
        return self._memory_store

    def _invalidate_memory_source(
        self,
        *,
        owner_id: str,
        project_path: str,
        source_uri: str,
        source_hash: str,
        mtime_ns: int | None,
    ) -> None:
        target = self._memory_target(required=False)
        if target is None:
            return
        target.invalidate_source(
            source_uri,
            current_hash=source_hash,
            current_mtime_ns=mtime_ns,
            project_path=project_path,
            owner_id=owner_id,
            actor="knowledge-engine",
        )

    def import_source(self, request: ImportRequestV1) -> ImportResultV1:
        registration = request.registration
        self._validate_registration_metadata(registration)
        project = canonical_project(registration.project_path)
        initial_project_state = self.store.project_state(registration.owner_id, str(project))
        try:
            read = read_registered_source(project, request.source_path, self.policy)
            source_kind = request.source_kind or infer_source_kind(read.relative_path)
            if source_kind is SourceKind.CONVERSATION_JSON and Path(read.relative_path).suffix.casefold() != ".json":
                raise KnowledgePolicyError("format.adapter_extension_mismatch")
            if source_kind is SourceKind.CONVERSATION_HTML and Path(read.relative_path).suffix.casefold() != ".html":
                raise KnowledgePolicyError("format.adapter_extension_mismatch")
            if Path(read.relative_path).suffix.casefold() == ".json":
                try:
                    json.loads(read.payload)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise KnowledgePolicyError("format.malformed_structured_payload") from exc
            fragments = parse_source(read.payload, source_kind, self.policy)
            for fragment in fragments:
                reject_secret_text(fragment.content)
                reject_secret_text(fragment.locator)
                if fragment.title:
                    reject_secret_text(fragment.title)
            source_hash = hashlib.sha256(read.payload).hexdigest()
            source_uri = f"project://{read.relative_path}"
        except KnowledgePolicyError as exc:
            return ImportResultV1(
                project_path=str(project),
                source_uri="project://blocked",
                source_kind=request.source_kind or SourceKind.TEXT,
                status=(
                    SourceStatus.UNSUPPORTED
                    if exc.reason_code == "format.malformed_structured_payload"
                    else SourceStatus.BLOCKED
                ),
                dry_run=request.dry_run,
                reason_code=exc.reason_code,
            )
        except Exception as exc:
            reason = getattr(exc, "reason_code", "format.parse_failed")
            return ImportResultV1(
                project_path=str(project),
                source_uri=f"project://{read.relative_path}" if "read" in locals() else "project://unsupported",
                source_kind=request.source_kind or SourceKind.TEXT,
                status=SourceStatus.UNSUPPORTED,
                dry_run=request.dry_run,
                reason_code=str(reason),
            )
        if request.dry_run:
            return ImportResultV1(
                project_path=str(project),
                source_uri=source_uri,
                source_kind=source_kind,
                status=SourceStatus.ALLOWED,
                dry_run=True,
                source_hash=source_hash,
                fragments_parsed=len(fragments),
            )
        project_id = self.store.ensure_project(registration.owner_id, str(project))
        existing = self.store.active_source(project_id, source_uri, source_kind, "manual")
        effective_sensitivity = (
            "sensitive"
            if source_kind in {SourceKind.CONVERSATION_JSON, SourceKind.CONVERSATION_HTML}
            else registration.sensitivity_ceiling
        )
        derivation_version = (
            f"{PARSER_VERSION}|{self.policy.policy_version}|{registration.adapter_version}|"
            f"chunk:{self.policy.max_fragment_chars}"
        )
        if (
            existing is not None
            and existing["source_hash"] == source_hash
            and existing["active_derivation_version"] == derivation_version
            and existing["active_policy_version"] == self.policy.policy_version
            and int(existing["active_mtime_ns"]) == read.mtime_ns
            and int(existing["active_size_bytes"]) == read.size_bytes
            and {"public": 0, "internal": 1, "sensitive": 2}[str(existing["active_sensitivity"])]
            >= {"public": 0, "internal": 1, "sensitive": 2}[effective_sensitivity]
        ):
            return ImportResultV1(
                project_path=str(project), source_id=str(existing["source_id"]), source_uri=source_uri,
                source_kind=source_kind, status=SourceStatus.IMPORTED, dry_run=False,
                source_hash=source_hash, fragments_parsed=len(fragments), unchanged=True,
            )
        current = self.store.current_generation(project_id)
        generation = self.store.begin_generation(
            project_id,
            git_commit_sha=str(current["git_commit_sha"]) if current and current.get("git_commit_sha") else None,
            worktree_revision=str(current["worktree_revision"]) if current and current.get("worktree_revision") else None,
            policy_version=str(current["policy_version"]) if current and current.get("worktree_revision") else derivation_version,
            expected_mutation_epoch=initial_project_state[1] if initial_project_state else 0,
        )
        metadata_reclassified = bool(
            existing is not None
            and (
                existing["active_policy_version"] != self.policy.policy_version
                or existing["active_derivation_version"] != derivation_version
                or {"public": 0, "internal": 1, "sensitive": 2}[str(existing["active_sensitivity"])]
                < {"public": 0, "internal": 1, "sensitive": 2}[effective_sensitivity]
            )
        )
        try:
            facts = {fragment.ordinal: tuple(extract_facts(fragment)) for fragment in fragments}
            source_id, _, fragment_count, fact_count = self.store.stage_source_version(
                generation_id=generation, project_id=project_id, owner_id=registration.owner_id,
                source_uri=source_uri, source_kind=source_kind,
                source_origin="manual",
                sensitivity=effective_sensitivity, source_hash=source_hash,
                size_bytes=read.size_bytes, mtime_ns=read.mtime_ns,
                parser=f"{source_kind.value}-parser", derivation_version=derivation_version,
                project_commit_sha=None,
                worktree_revision=None, policy_version=self.policy.policy_version,
                fragments=fragments, facts_by_ordinal=facts,
            )
            self._invalidate_memory_source(
                owner_id=registration.owner_id,
                project_path=str(project),
                source_uri=source_uri,
                source_hash="0" * 64 if metadata_reclassified else source_hash,
                mtime_ns=read.mtime_ns,
            )
            self.store.activate_generation(project_id, generation)
        except Exception:
            self.store.fail_generation(generation, "import.publish_failed")
            raise
        return ImportResultV1(
            project_path=str(project), source_id=source_id, source_uri=source_uri,
            source_kind=source_kind, status=SourceStatus.IMPORTED, dry_run=False,
            source_hash=source_hash, fragments_parsed=len(fragments),
            fragments_published=fragment_count, facts_published=fact_count,
        )

    def index_repository(
        self,
        registration: SourceRegistrationV1,
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        if not registration.consent:
            raise KnowledgePolicyError("approval.required")
        self._validate_registration_metadata(registration)
        project = canonical_project(registration.project_path)
        derivation_version = (
            f"{PARSER_VERSION}|{self.policy.policy_version}|{registration.adapter_version}|"
            f"chunk:{self.policy.max_fragment_chars}|map:1.0"
        )
        initial_project_state = self.store.project_state(registration.owner_id, str(project))
        existing_project_id = initial_project_state[0] if initial_project_state else None
        current = self.store.current_generation(existing_project_id) if existing_project_id else None
        previous_map = self.store.repository_map(registration.owner_id, str(project)) if existing_project_id else None
        previous_files = {item.path: item for item in previous_map.files} if previous_map else {}
        reuse_allowed = bool(current and current.get("policy_version") == derivation_version)
        entries = tracked_files(
            project,
            max_files=self.policy.max_tracked_files,
            max_output_bytes=self.policy.max_git_output_bytes,
        )
        entries_by_path = {item.path: item for item in entries}
        commit = git_commit(project)
        changed_paths = git_changed_paths(project)
        racy_candidates = [
            path
            for path in changed_paths
            if (
                (previous := previous_files.get(path)) is not None
                and previous.git_worktree_object_id is not None
                and (entry := entries_by_path.get(path)) is not None
                and previous.git_index_object_id == entry.git_object
                and previous.git_object_id == entry.head_object
            )
        ]
        try:
            current_worktree_objects = git_worktree_object_ids(project, racy_candidates)
        except RepositoryError:
            current_worktree_objects = {}
        changed_paths.difference_update(
            path
            for path, object_id in current_worktree_objects.items()
            if object_id == previous_files[path].git_worktree_object_id
        )
        prepared: list[PreparedSource] = []
        blocked: list[dict[str, str]] = []
        blocked_sources: list[BlockedRepositorySourceV1] = []
        total_bytes = 0
        attempted_bytes = 0
        inventory_truncated = False
        for entry_index, entry in enumerate(entries):
            try:
                file_size, file_mtime_ns = tracked_source_observation(
                    project, entry, self.policy
                )
                if attempted_bytes + file_size > self.policy.max_total_bytes:
                    safe_path = self._safe_blocked_path(entry.path)
                    blocked.append({"path": safe_path, "reason_code": "limit.total_bytes"})
                    blocked_sources.append(BlockedRepositorySourceV1(
                        path=entry.path if safe_path == entry.path else None,
                        path_hash=hashlib.sha256(entry.path.encode("utf-8")).hexdigest(),
                        reason_code="limit.total_bytes",
                    ))
                    inventory_truncated = True
                    break
                attempted_bytes += file_size
                previous_file = previous_files.get(entry.path)
                fast_reuse = bool(
                    reuse_allowed
                    and previous_file is not None
                    and previous_file.category != "manifest"
                    and entry.path not in changed_paths
                    and previous_file.size_bytes == file_size
                    and previous_file.mtime_ns == file_mtime_ns
                    and previous_file.git_index_object_id == entry.git_object
                    and previous_file.git_object_id == entry.head_object
                )
                source = prepare_tracked_source(
                    project,
                    entry,
                    self.policy,
                    commit,
                    previous_file=previous_file,
                    reuse_allowed=reuse_allowed,
                    fast_reuse=fast_reuse,
                    observed_size_bytes=file_size,
                    observed_mtime_ns=file_mtime_ns,
                )
                if total_bytes + source.read.size_bytes > self.policy.max_total_bytes:
                    safe_path = self._safe_blocked_path(entry.path)
                    blocked.append({"path": safe_path, "reason_code": "limit.total_bytes"})
                    blocked_sources.append(BlockedRepositorySourceV1(
                        path=entry.path if safe_path == entry.path else None,
                        path_hash=hashlib.sha256(entry.path.encode("utf-8")).hexdigest(),
                        reason_code="limit.total_bytes",
                    ))
                    inventory_truncated = True
                    break
                total_bytes += source.read.size_bytes
                prepared.append(source)
            except KnowledgePolicyError as exc:
                safe_path = self._safe_blocked_path(entry.path)
                blocked.append({"path": safe_path, "reason_code": exc.reason_code})
                blocked_sources.append(BlockedRepositorySourceV1(
                    path=entry.path if safe_path == entry.path else None,
                    path_hash=hashlib.sha256(entry.path.encode("utf-8")).hexdigest(),
                    reason_code=exc.reason_code,
                ))
            except Exception:
                safe_path = self._safe_blocked_path(entry.path)
                blocked.append({"path": safe_path, "reason_code": "format.parse_failed"})
                blocked_sources.append(BlockedRepositorySourceV1(
                    path=entry.path if safe_path == entry.path else None,
                    path_hash=hashlib.sha256(entry.path.encode("utf-8")).hexdigest(),
                    reason_code="format.parse_failed",
                ))
        remote = git_remote(project)
        revision = worktree_revision(
            commit,
            prepared,
            self.policy.policy_version,
            derivation_version=derivation_version,
            sensitivity=registration.sensitivity_ceiling,
            remote=remote,
            tracked_entries=entries,
            blocked_sources=blocked_sources,
        )
        repository_map = build_repository_map(
            owner_id=registration.owner_id,
            project=project,
            commit_sha=commit,
            remote=remote,
            prepared=prepared,
            worktree_revision_value=revision,
            policy_version=self.policy.policy_version,
            tracked_files_count=len(entries),
            blocked_files_count=len(blocked),
            blocked_sources=blocked_sources,
        )
        history = git_history_payload(project, self.policy.max_git_history_entries)
        history_privacy_finding = detect_secret(history) if history else None
        if history_privacy_finding:
            blocked.append({"path": "git://history", "reason_code": history_privacy_finding})
            history = b""
        dry_result = {
            "project_path": str(project),
            "dry_run": dry_run,
            "tracked_files": len(entries),
            "allowed_files": len(prepared),
            "blocked_files": len(blocked),
            "blocked": blocked,
            "fragments": sum(len(source.fragments) for source in prepared),
            "reused_files": sum(1 for source in prepared if source.reused),
            "fast_reused_files": sum(
                1 for source in prepared if source.reused and not source.read.payload
            ),
            "source_bytes": total_bytes,
            "scanned_bytes": attempted_bytes,
            "inventory_truncated": inventory_truncated,
            "unprocessed_files": len(entries) - len(prepared) - len(blocked) if inventory_truncated else 0,
            "git_commit_sha": commit,
            "worktree_revision": revision,
            "languages": repository_map.languages,
        }
        if dry_run:
            return dry_result
        if inventory_truncated:
            raise KnowledgePolicyError("limit.total_bytes")
        project_id = self.store.ensure_project(registration.owner_id, str(project))
        current = self.store.current_generation(project_id)
        if current and current.get("worktree_revision") == revision and current.get("policy_version") == derivation_version:
            return {**dry_result, "dry_run": False, "unchanged": True, "generation_id": current["generation_id"]}
        generation = self.store.begin_generation(
            project_id,
            git_commit_sha=commit,
            worktree_revision=revision,
            policy_version=derivation_version,
            expected_mutation_epoch=initial_project_state[1] if initial_project_state else 0,
        )
        self.store.drop_repository_sources(generation)
        previous_observations = {
            source.source_uri: self.store.active_source(
                project_id,
                source.source_uri,
                source.source_kind,
                "repository",
            )
            for source in prepared
        }
        current_uris = {source.source_uri for source in prepared}
        if history:
            current_uris.add("git://history")
        published_fragments = 0
        published_facts = 0
        renamed: list[dict[str, str]] = []
        try:
            for source in prepared:
                rename_from = self.store.find_rename_candidate(project_id, source.source_hash, current_uris)
                source_id, _, fragments_count, facts_count = self.store.stage_source_version(
                    generation_id=generation, project_id=project_id,
                    owner_id=registration.owner_id, source_uri=source.source_uri,
                    source_kind=source.source_kind, source_origin="repository",
                    sensitivity=registration.sensitivity_ceiling,
                    source_hash=source.source_hash, size_bytes=source.read.size_bytes,
                    mtime_ns=source.read.mtime_ns, parser=source.parser,
                    derivation_version=derivation_version,
                    project_commit_sha=source.file.git_commit_sha, fragments=source.fragments,
                    worktree_revision=revision, policy_version=self.policy.policy_version,
                    facts_by_ordinal=source.facts_by_ordinal,
                    renamed_from_source_id=rename_from,
                )
                published_fragments += fragments_count
                published_facts += facts_count
                if rename_from:
                    renamed.append({"source_id": source_id, "renamed_from_source_id": rename_from})
            if history:
                history_hash = hashlib.sha256(history).hexdigest()
                history_fragments = parse_source(history, SourceKind.GIT_HISTORY, self.policy)
                for fragment in history_fragments:
                    reject_secret_text(fragment.content)
                    reject_secret_text(fragment.locator)
                self.store.stage_source_version(
                    generation_id=generation, project_id=project_id,
                    owner_id=registration.owner_id, source_uri="git://history",
                    source_kind=SourceKind.GIT_HISTORY, source_origin="repository",
                    sensitivity=registration.sensitivity_ceiling,
                    source_hash=history_hash, size_bytes=len(history), mtime_ns=0,
                    parser="git-metadata-parser", derivation_version=derivation_version,
                    project_commit_sha=commit,
                    worktree_revision=revision, policy_version=self.policy.policy_version,
                    fragments=history_fragments, facts_by_ordinal={},
                )
            self.store.save_repository_map(generation, project_id, repository_map)
            for source in prepared:
                previous_observation = previous_observations.get(source.source_uri)
                metadata_reclassified = bool(
                    previous_observation is not None
                    and (
                        previous_observation["active_policy_version"] != self.policy.policy_version
                        or previous_observation["active_derivation_version"] != derivation_version
                        or {"public": 0, "internal": 1, "sensitive": 2}[
                            str(previous_observation["active_sensitivity"])
                        ]
                        < {"public": 0, "internal": 1, "sensitive": 2}[
                            registration.sensitivity_ceiling
                        ]
                    )
                )
                self._invalidate_memory_source(
                    owner_id=registration.owner_id,
                    project_path=str(project),
                    source_uri=source.source_uri,
                    source_hash="0" * 64 if metadata_reclassified else source.source_hash,
                    mtime_ns=source.read.mtime_ns,
                )
            previous_repository_paths = {
                item.path for item in previous_map.files
            } if previous_map else set()
            for removed_path in previous_repository_paths - {
                item.read.relative_path for item in prepared
            }:
                self._invalidate_memory_source(
                    owner_id=registration.owner_id,
                    project_path=str(project),
                    source_uri=f"project://{removed_path}",
                    source_hash="0" * 64,
                    mtime_ns=None,
                )
            self.store.activate_generation(project_id, generation)
        except Exception:
            self.store.fail_generation(generation, "repository.publish_failed")
            raise
        return {
            **dry_result,
            "dry_run": False,
            "unchanged": False,
            "generation_id": generation,
            "published_fragments": published_fragments,
            "published_facts": published_facts,
            "renamed": renamed,
        }

    def retrieve(self, request: RetrievalRequestV1) -> RetrievalResultV1:
        project = canonical_project(request.project_path)
        reject_secret_text(request.owner_id)
        reject_secret_text(request.query)
        normalized = request.model_copy(update={"project_path": str(project)})
        candidate_request = normalized.model_copy(
            update={"max_fragments": 32, "token_budget": 32_768}
        )
        page = self.store.retrieve(candidate_request, candidate_pool=True)
        first_page = page
        selected = []
        freshness_dropped = False
        current_commit: str | None | object = object()
        live_sources: dict[str, tuple[tuple[int, int, str] | None, bool]] = {}
        tracked_paths: set[str] | None = None
        repository_scope_valid: bool | None = None
        remaining = request.token_budget
        selected_content_hashes: set[str] = set()
        while True:
            for fragment in page.fragments:
                if len(selected) >= request.max_fragments:
                    break
                provenance = fragment.provenance
                privacy_invalid = (
                    provenance.policy_version != self.policy.policy_version
                    or provenance.parser_version != PARSER_VERSION
                    or f"|chunk:{self.policy.max_fragment_chars}" not in provenance.derivation_version
                )
                stale = fragment.stale or privacy_invalid
                if provenance.source_origin == "repository":
                    if tracked_paths is None:
                        try:
                            tracked_paths = {
                                entry.path
                                for entry in tracked_files(
                                    project,
                                    max_files=self.policy.max_tracked_files,
                                    max_output_bytes=self.policy.max_git_output_bytes,
                                )
                            }
                            repository_scope_valid = True
                        except RepositoryError:
                            tracked_paths = set()
                            repository_scope_valid = False
                    if repository_scope_valid is False:
                        privacy_invalid = True
                    if provenance.source_uri.startswith("project://"):
                        stale = stale or provenance.source_uri[len("project://"):] not in tracked_paths
                if provenance.source_uri.startswith("project://"):
                    relative = provenance.source_uri[len("project://"):]
                    if relative not in live_sources:
                        try:
                            live = read_registered_source(
                                project,
                                relative,
                                self.policy,
                                repository_tracked=provenance.source_origin == "repository",
                            )
                            live_sources[relative] = (
                                (
                                    live.size_bytes,
                                    live.mtime_ns,
                                    hashlib.sha256(live.payload).hexdigest(),
                                ),
                                False,
                            )
                        except KnowledgePolicyError as exc:
                            if exc.reason_code not in {
                                "path.unavailable",
                                "path.open_failed",
                                "source.changed_during_read",
                            }:
                                privacy_invalid = True
                            live_sources[relative] = (None, privacy_invalid)
                        except OSError:
                            live_sources[relative] = (None, False)
                    observation, cached_privacy_invalid = live_sources[relative]
                    privacy_invalid = privacy_invalid or cached_privacy_invalid
                    stale = stale or observation is None or observation != (
                        provenance.source_size_bytes,
                        provenance.source_mtime_ns,
                        provenance.source_hash,
                    )
                elif (
                    provenance.source_uri == "git://history"
                    and provenance.project_commit_sha
                    and repository_scope_valid is not False
                ):
                    if not isinstance(current_commit, (str, type(None))):
                        current_commit = git_commit(project)
                    stale = stale or current_commit != provenance.project_commit_sha
                if privacy_invalid or (
                    stale and request.freshness is FreshnessRequirement.ACTIVE_ONLY
                ):
                    freshness_dropped = True
                    continue
                content_hash = hashlib.sha256(fragment.content.encode("utf-8")).hexdigest()
                if content_hash in selected_content_hashes:
                    continue
                if fragment.estimated_tokens > remaining:
                    continue
                updated_provenance = provenance.model_copy(
                    update={"status": "stale" if stale else "active"}
                )
                selected.append(
                    fragment.model_copy(
                        update={"stale": stale, "provenance": updated_provenance}
                    )
                )
                selected_content_hashes.add(content_hash)
                remaining -= fragment.estimated_tokens
            if len(selected) >= request.max_fragments or page.next_offset is None:
                break
            page = self.store.retrieve(
                candidate_request,
                candidate_pool=True,
                candidate_offset=page.next_offset,
            )
        return first_page.model_copy(
            update={
                "token_budget": request.token_budget,
                "fragments": selected,
                "estimated_tokens": request.token_budget - remaining,
                "degraded": first_page.degraded or freshness_dropped,
                "reason_code": "freshness.filtered" if freshness_dropped else first_page.reason_code,
                "next_offset": None,
            }
        )

    def repository_map(self, project_path: str, *, owner_id: str = "local-user") -> RepositoryMapV1 | None:
        project = canonical_project(project_path)
        repository_map = self.store.repository_map(owner_id, str(project))
        if repository_map is None:
            return None
        stale = repository_map.policy_version != self.policy.policy_version
        try:
            entries = tracked_files(
                project,
                max_files=self.policy.max_tracked_files,
                max_output_bytes=self.policy.max_git_output_bytes,
            )
            stale = stale or git_commit(project) != repository_map.git_commit_sha
            stale = stale or len(entries) != repository_map.tracked_files_count
            entries_by_hash = {
                hashlib.sha256(entry.path.encode("utf-8")).hexdigest(): entry
                for entry in entries
            }
            observed_path_hashes = {
                hashlib.sha256(item.path.encode("utf-8")).hexdigest()
                for item in repository_map.files
            } | {item.path_hash for item in repository_map.blocked_sources}
            stale = stale or set(entries_by_hash) != observed_path_hashes
            changed_paths = git_changed_paths(project) if not stale else set()
            if not stale:
                for item in repository_map.files:
                    entry = entries_by_hash.get(
                        hashlib.sha256(item.path.encode("utf-8")).hexdigest()
                    )
                    if (
                        entry is None
                        or entry.git_object != item.git_index_object_id
                        or entry.head_object != item.git_object_id
                    ):
                        stale = True
                        break
                    if item.path in changed_paths:
                        live = read_registered_source(
                            project,
                            item.path,
                            self.policy,
                            repository_tracked=True,
                        )
                        if (
                            live.size_bytes != item.size_bytes
                            or live.mtime_ns != item.mtime_ns
                            or hashlib.sha256(live.payload).hexdigest() != item.content_hash
                        ):
                            stale = True
                            break
            if not stale:
                commit = repository_map.git_commit_sha
                for blocked_source in repository_map.blocked_sources:
                    entry = entries_by_hash.get(blocked_source.path_hash)
                    if entry is None:
                        stale = True
                        break
                    if entry.path not in changed_paths:
                        continue
                    try:
                        prepare_tracked_source(project, entry, self.policy, commit)
                    except KnowledgePolicyError as exc:
                        if exc.reason_code != blocked_source.reason_code:
                            stale = True
                            break
                    except Exception:
                        if blocked_source.reason_code != "format.parse_failed":
                            stale = True
                            break
                    else:
                        stale = True
                        break
            stale = stale or git_remote(project) != repository_map.git_remote
        except (KnowledgePolicyError, RepositoryError, OSError):
            stale = True
        return repository_map.model_copy(update={"stale": stale})

    def search_repository_text(
        self,
        *,
        project_path: str,
        query: str,
        owner_id: str = "local-user",
        max_matches: int = 100,
    ) -> dict[str, object]:
        """Deterministic rg fallback over the current privacy-approved map."""

        project = canonical_project(project_path)
        reject_secret_text(owner_id)
        reject_secret_text(query)
        repository_map = self.repository_map(str(project), owner_id=owner_id)
        if repository_map is None:
            raise KnowledgeStoreError("repository is not indexed")
        if repository_map.stale:
            raise KnowledgeStoreError("repository map is stale; reindex before rg search")
        # rg is an optional exact-text accelerator, not a trust boundary.  Read
        # and hash its complete allowlisted input set before execution so a
        # path swap cannot expand the approved scope.
        for item in repository_map.files:
            live = read_registered_source(
                project,
                item.path,
                self.policy,
                repository_tracked=True,
            )
            if (
                live.size_bytes != item.size_bytes
                or live.mtime_ns != item.mtime_ns
                or hashlib.sha256(live.payload).hexdigest() != item.content_hash
            ):
                raise KnowledgeStoreError("repository map is stale; reindex before rg search")
        matches = ripgrep_allowed_files(
            project,
            query,
            [item.path for item in repository_map.files],
            max_matches=max_matches,
        )
        post_search_map = self.repository_map(str(project), owner_id=owner_id)
        if (
            post_search_map is None
            or post_search_map.stale
            or post_search_map.worktree_revision != repository_map.worktree_revision
        ):
            raise KnowledgeStoreError("repository changed during rg search")
        for item in repository_map.files:
            live = read_registered_source(
                project,
                item.path,
                self.policy,
                repository_tracked=True,
            )
            if hashlib.sha256(live.payload).hexdigest() != item.content_hash:
                raise KnowledgeStoreError("repository changed during rg search")
        return {
            "project_path": str(project),
            "query": query,
            "worktree_revision": repository_map.worktree_revision,
            "matches": matches,
            "untrusted": True,
            "local_only": True,
        }

    def build_context(
        self,
        *,
        project_path: str,
        goal: str,
        token_budget: int,
        constraints: Sequence[str] = (),
        modified_files: Sequence[str] = (),
        unresolved_errors: Sequence[str] = (),
        verification_plan: Sequence[str] = (),
        fresh_tool_results: Sequence[str] = (),
        owner_id: str = "local-user",
    ) -> ContextEnvelopeV1:
        project = canonical_project(project_path)
        if not (128 <= token_budget <= 32_768):
            raise KnowledgeStoreError("context token budget is out of range")
        if not goal or len(goal) > 2_048:
            raise KnowledgeStoreError("context goal exceeds limit")
        bounded_groups = (
            (constraints, 64, 2_048),
            (modified_files, 256, 1_024),
            (unresolved_errors, 64, 2_048),
            (verification_plan, 64, 2_048),
            (fresh_tool_results, 32, 2_048),
        )
        if any(
            len(group) > count_limit or any(len(item) > item_limit for item in group)
            for group, count_limit, item_limit in bounded_groups
        ):
            raise KnowledgeStoreError("context fixed section exceeds limit")
        for value in (
            goal,
            *constraints,
            *modified_files,
            *unresolved_errors,
            *verification_plan,
            *fresh_tool_results,
        ):
            try:
                reject_secret_text(value)
            except KnowledgePolicyError as exc:
                raise KnowledgeStoreError("context contains blocked secret material") from exc
        repository_map = self.repository_map(str(project), owner_id=owner_id)
        full_summary: dict[str, object] = {}
        if repository_map is not None and not repository_map.stale:
            full_summary = {
                "map_version": repository_map.map_version,
                "git_commit_sha": repository_map.git_commit_sha,
                "languages": repository_map.languages,
                "manifests": [item[:256] for item in repository_map.manifests[:16]],
                "entry_points": [item[:256] for item in repository_map.entry_points[:16]],
                "modules": [item[:256] for item in repository_map.modules[:32]],
                "agents_hierarchy": [item[:256] for item in repository_map.agents_hierarchy[:16]],
            }
        summaries = [full_summary]
        if full_summary:
            summaries.extend(
                [
                    {
                        "map_version": full_summary.get("map_version"),
                        "git_commit_sha": full_summary.get("git_commit_sha"),
                        "languages": full_summary.get("languages", {}),
                        "entry_points": full_summary.get("entry_points", [])[:4],
                    },
                    {
                        "map_version": full_summary.get("map_version"),
                        "git_commit_sha": full_summary.get("git_commit_sha"),
                    },
                    {},
                ]
            )
        empty_evidence = RetrievalResultV1(
            project_path=str(project),
            query=goal,
            token_budget=token_budget,
            estimated_tokens=0,
            fragments=[],
        )

        def assemble(summary: dict[str, object], evidence: RetrievalResultV1) -> ContextEnvelopeV1:
            degraded = summary != full_summary or evidence.degraded
            reason_code = (
                "context.summary_compacted"
                if summary != full_summary
                else evidence.reason_code
            )
            base = ContextEnvelopeV1(
                project_path=str(project), goal=goal, constraints=list(constraints),
                modified_files=list(modified_files), unresolved_errors=list(unresolved_errors),
                verification_plan=list(verification_plan), fresh_tool_results=list(fresh_tool_results),
                repository_summary=summary,
                evidence=evidence, token_budget=token_budget, estimated_tokens=0,
                degraded=degraded, reason_code=reason_code,
            )
            estimate = 0
            for _ in range(6):
                candidate = base.model_copy(update={"estimated_tokens": estimate})
                measured = conservative_token_estimate(candidate.model_dump_json())
                if measured == estimate:
                    break
                estimate = measured
            if estimate > token_budget:
                raise KnowledgeStoreError("context envelope exceeds token budget")
            return ContextEnvelopeV1.model_validate(
                base.model_copy(update={"estimated_tokens": estimate}).model_dump()
            )

        summary: dict[str, object] | None = None
        empty_context: ContextEnvelopeV1 | None = None
        for candidate_summary in summaries:
            try:
                empty_context = assemble(candidate_summary, empty_evidence)
                summary = candidate_summary
                break
            except KnowledgeStoreError:
                continue
        if summary is None or empty_context is None:
            raise KnowledgeStoreError("context fixed sections exceed token budget")
        evidence_budget = max(128, token_budget - empty_context.estimated_tokens + 64)
        if evidence_budget >= 128 and token_budget > empty_context.estimated_tokens:
            retrieval = self.retrieve(
                RetrievalRequestV1(
                    owner_id=owner_id,
                    project_path=str(project),
                    query=(" ".join((goal, *modified_files, *unresolved_errors)))[:2_048],
                    allowed_source_types=[
                        SourceKind.MARKDOWN,
                        SourceKind.TEXT,
                        SourceKind.PROJECT_CONFIG,
                        SourceKind.REPOSITORY_FILE,
                        SourceKind.REPOSITORY_MAP,
                    ],
                    token_budget=evidence_budget,
                )
            )
        else:
            retrieval = empty_evidence
        while True:
            try:
                return assemble(summary, retrieval)
            except KnowledgeStoreError:
                if not retrieval.fragments:
                    return empty_context
                fragments = retrieval.fragments[:-1]
                retrieval = retrieval.model_copy(
                    update={
                        "fragments": fragments,
                        "estimated_tokens": sum(item.estimated_tokens for item in fragments),
                        "degraded": True,
                        "reason_code": "context.evidence_trimmed",
                    }
                )

    def purge_source(
        self,
        source_id: str,
        *,
        owner_id: str,
        project_path: str,
        apply: bool,
    ) -> dict[str, object]:
        preview = self.store.purge_source(
            source_id,
            owner_id=owner_id,
            project_path=project_path,
            apply=False,
        )
        if not apply:
            return preview
        target = self._memory_target(required=True)
        assert target is not None
        try:
            memory_result = target.hard_purge_source(
                str(preview["source_uri"]),
                confirm_source_uri=str(preview["source_uri"]),
                project_path=project_path,
                owner_id=owner_id,
                actor="knowledge-purge",
            )
        except Exception as exc:
            raise KnowledgeStoreError("memory purge failed before knowledge purge") from exc
        if not memory_result.get("physical_purge_complete"):
            return {
                **preview,
                "apply": True,
                "logical_purge_complete": False,
                "physical_purge_complete": False,
                "memory_purge": memory_result,
                "memory_invalidation_complete": False,
                "complete": False,
                "reason_code": "purge.memory_physical_deferred",
            }
        result = self.store.purge_source(
            source_id,
            owner_id=owner_id,
            project_path=project_path,
            apply=True,
        )
        memory_complete = True
        result["memory_purge"] = memory_result
        result["memory_invalidation_complete"] = memory_complete
        result["complete"] = bool(
            result.get("logical_purge_complete")
            and result.get("physical_purge_complete")
            and memory_complete
        )
        if not result["complete"] and not result.get("reason_code"):
            result["reason_code"] = "purge.memory_invalidation_deferred"
        return result

    def compact_storage(self) -> dict[str, object]:
        complete = self.store.compact_storage()
        return {
            "physical_purge_complete": complete,
            "reason_code": None if complete else "purge.physical_deferred",
        }

    def propose_memory_candidate(
        self,
        fact_id: str,
        *,
        project_path: str,
        owner_id: str = "local-user",
        confirmation: str,
    ) -> str:
        if confirmation != "PROPOSE-MEMORY":
            raise KnowledgeStoreError("explicit PROPOSE-MEMORY confirmation is required")
        canonical = str(canonical_project(project_path))
        candidate = self.store.candidate(
            fact_id,
            owner_id=owner_id,
            canonical_path=canonical,
        )
        if candidate is None:
            raise KnowledgeStoreError("knowledge candidate not found")
        if candidate["active_status"] != "candidate":
            raise KnowledgeStoreError("only non-conflicted active candidates may be proposed")
        from services.memory.contracts import (
            MemorySensitivity,
            MemoryRecordType,
            MemoryScope,
            MemorySourceV1,
            MemoryStatus,
            MemoryUpsertV1,
        )
        if (
            candidate["policy_version"] != self.policy.policy_version
            or candidate["parser_version"] != PARSER_VERSION
            or f"|chunk:{self.policy.max_fragment_chars}" not in str(candidate["derivation_version"])
        ):
            raise KnowledgeStoreError("knowledge candidate is stale under the current policy")
        source_uri = str(candidate["source_uri"])
        if source_uri.startswith("project://"):
            relative = source_uri[len("project://"):]
            try:
                live = read_registered_source(
                    canonical,
                    relative,
                    self.policy,
                    repository_tracked=candidate["source_origin"] == "repository",
                )
            except KnowledgePolicyError as exc:
                raise KnowledgeStoreError("knowledge candidate source is unavailable") from exc
            if (
                hashlib.sha256(live.payload).hexdigest() != candidate["source_hash"]
                or live.mtime_ns != candidate["mtime_ns"]
            ):
                raise KnowledgeStoreError("knowledge candidate source is stale")
            if candidate["source_origin"] == "repository":
                try:
                    current_paths = {
                        entry.path for entry in tracked_files(
                            Path(canonical),
                            max_files=self.policy.max_tracked_files,
                            max_output_bytes=self.policy.max_git_output_bytes,
                        )
                    }
                except RepositoryError as exc:
                    raise KnowledgeStoreError("knowledge candidate repository scope is invalid") from exc
                if relative not in current_paths:
                    raise KnowledgeStoreError("knowledge candidate source is no longer tracked")
        target = self._memory_target(required=True)
        assert target is not None
        observed_at = datetime.fromisoformat(str(candidate["observed_at"]))
        record = target.upsert(
            MemoryUpsertV1(
                record_type=MemoryRecordType.PROJECT_KNOWLEDGE,
                scope=MemoryScope.PROJECT,
                subject=str(candidate["fact_key"]),
                value={
                    "key": str(candidate["fact_key"]),
                    "value": str(candidate["fact_value"]),
                    "knowledge_provenance": {
                        "parser": str(candidate["parser"]),
                        "parser_version": str(candidate["parser_version"]),
                        "extraction_method": str(candidate["extraction_method"]),
                        "derivation_version": str(candidate["derivation_version"]),
                        "policy_version": str(candidate["policy_version"]),
                    },
                },
                source=MemorySourceV1(
                    source_type="knowledge_candidate",
                    uri=str(candidate["source_uri"]),
                    fragment=str(candidate["locator"]),
                    source_hash=str(candidate["source_hash"]),
                    observed_at=observed_at,
                    source_commit_sha=candidate["project_commit_sha"],
                    source_mtime_ns=int(candidate["mtime_ns"]),
                    producer="knowledge-engine",
                    author=owner_id,
                ),
                owner_id=str(candidate["owner_id"]),
                project_path=str(candidate["canonical_path"]),
                status=MemoryStatus.CANDIDATE,
                confidence=0.5,
                project_commit_sha=candidate["project_commit_sha"],
                sensitivity=MemorySensitivity(str(candidate["sensitivity"])),
            )
        )
        return record.record_id
