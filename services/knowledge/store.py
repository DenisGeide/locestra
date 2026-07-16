from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from services.knowledge.contracts import (
    FactStatus,
    FragmentStatus,
    FreshnessRequirement,
    ProvenanceV1,
    RepositoryMapV1,
    RetrievalRequestV1,
    RetrievalResultV1,
    RetrievedFragmentV1,
    SourceKind,
    SourceStatus,
)
from services.knowledge.migrations import (
    migrate_knowledge_database,
    open_knowledge_database,
    verify_knowledge_database,
)
from services.knowledge.parsers import ExtractedFact, PARSER_VERSION, ParsedFragment


DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "knowledge.sqlite3"


class KnowledgeStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    material = "\x1f".join(parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:40]}"


def _estimated_tokens(content: str) -> int:
    return max(
        1,
        max(
            math.ceil(len(content) / 2),
            math.ceil(len(content.encode("utf-8")) / 3),
        ) + 64,
    )


def conservative_token_estimate(content: str) -> int:
    return _estimated_tokens(content)


_SENSITIVITY_RANK = {"public": 0, "internal": 1, "sensitive": 2}


def _max_sensitivity(left: str, right: str) -> str:
    if left not in _SENSITIVITY_RANK or right not in _SENSITIVITY_RANK:
        raise KnowledgeStoreError("unsupported source sensitivity")
    return left if _SENSITIVITY_RANK[left] >= _SENSITIVITY_RANK[right] else right


def _fts_query(query: str) -> tuple[str, tuple[str, ...]]:
    terms = []
    for token in re.findall(r"[^\W_]{2,64}", query.casefold(), flags=re.UNICODE):
        if token not in terms:
            terms.append(token)
        if len(terms) >= 16:
            break
    if not terms:
        raise KnowledgeStoreError("retrieval query has no searchable terms")
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms), tuple(terms)


class KnowledgeStore:
    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE,
        *,
        initialize: bool = True,
        harden_permissions: bool = True,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        if initialize:
            migrate_knowledge_database(
                self.database_path,
                harden_permissions=harden_permissions,
            )
            self.recover_abandoned_generations()

    def recover_abandoned_generations(self, *, older_than_seconds: int = 3_600) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT generation_id FROM knowledge_generations "
                "WHERE status='failed' OR (status='building' AND started_at<?) ORDER BY started_at",
                (cutoff,),
            ).fetchall()
        for row in rows:
            self.fail_generation(str(row["generation_id"]), "build.abandoned")
        return len(rows)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(open_knowledge_database(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(open_knowledge_database(self.database_path, read_only=True)) as connection:
            yield connection

    def status(self) -> dict[str, object]:
        verification = verify_knowledge_database(self.database_path)
        with self._read() as connection:
            counts = {
                table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in (
                    "knowledge_projects",
                    "knowledge_sources",
                    "knowledge_source_versions",
                    "knowledge_fragments",
                    "knowledge_facts",
                    "knowledge_conflicts",
                    "repository_maps",
                )
            }
        return {
            "schema_version": verification.schema_version,
            "application_id": verification.application_id,
            "integrity_check": verification.integrity_check,
            "fts5": verification.fts5_available,
            "counts": counts,
        }

    def ensure_project(self, owner_id: str, canonical_path: str) -> str:
        project_id = _stable_id("project", owner_id, canonical_path.casefold())
        timestamp = _now()
        with self._write() as connection:
            connection.execute(
                "INSERT INTO knowledge_projects(project_id,owner_id,canonical_path,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(owner_id,canonical_path) DO UPDATE SET updated_at=excluded.updated_at",
                (project_id, owner_id, canonical_path, timestamp, timestamp),
            )
        return project_id

    def project_registered(self, owner_id: str, canonical_path: str) -> bool:
        if not self.database_path.is_file():
            return False
        with self._read() as connection:
            return connection.execute(
                "SELECT 1 FROM knowledge_projects WHERE owner_id=? AND canonical_path=?",
                (owner_id, canonical_path),
            ).fetchone() is not None

    def active_generation_id(self, project_id: str) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT active_generation_id FROM knowledge_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return row["active_generation_id"] if row else None

    def project_id_for(self, owner_id: str, canonical_path: str) -> str | None:
        if not self.database_path.is_file():
            return None
        with self._read() as connection:
            row = connection.execute(
                "SELECT project_id FROM knowledge_projects WHERE owner_id=? AND canonical_path=?",
                (owner_id, canonical_path),
            ).fetchone()
        return str(row["project_id"]) if row else None

    def project_state(self, owner_id: str, canonical_path: str) -> tuple[str, int] | None:
        if not self.database_path.is_file():
            return None
        with self._read() as connection:
            row = connection.execute(
                "SELECT project_id,mutation_epoch FROM knowledge_projects "
                "WHERE owner_id=? AND canonical_path=?",
                (owner_id, canonical_path),
            ).fetchone()
        return (str(row["project_id"]), int(row["mutation_epoch"])) if row else None

    def current_generation(self, project_id: str) -> dict[str, object] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT g.* FROM knowledge_projects p JOIN knowledge_generations g "
                "ON g.generation_id=p.active_generation_id WHERE p.project_id=?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def begin_generation(
        self,
        project_id: str,
        *,
        git_commit_sha: str | None,
        worktree_revision: str | None,
        policy_version: str,
        clone_active: bool = True,
        expected_mutation_epoch: int | None = None,
    ) -> str:
        generation_id = f"gen_{uuid.uuid4().hex}"
        with self._write() as connection:
            project = connection.execute(
                "SELECT active_generation_id,mutation_epoch FROM knowledge_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KnowledgeStoreError("project is not registered")
            if (
                expected_mutation_epoch is not None
                and int(project["mutation_epoch"]) != expected_mutation_epoch
            ):
                raise KnowledgeStoreError("project changed during source preparation")
            connection.execute(
                "INSERT INTO knowledge_generations("
                "generation_id,project_id,base_generation_id,base_mutation_epoch,status,"
                "git_commit_sha,worktree_revision,policy_version,started_at) "
                "VALUES(?,?,?,?,'building',?,?,?,?)",
                (
                    generation_id, project_id, project["active_generation_id"],
                    int(project["mutation_epoch"]), git_commit_sha,
                    worktree_revision, policy_version, _now(),
                ),
            )
            active = project["active_generation_id"]
            if clone_active and active:
                connection.execute(
                    "INSERT INTO knowledge_generation_sources("
                    "generation_id,source_id,version_id,source_origin,sensitivity,size_bytes,mtime_ns,"
                    "project_commit_sha,worktree_revision,derivation_version,policy_version,"
                    "renamed_from_source_id,observed_at) "
                    "SELECT ?,source_id,version_id,source_origin,sensitivity,size_bytes,mtime_ns,"
                    "project_commit_sha,worktree_revision,derivation_version,policy_version,"
                    "renamed_from_source_id,observed_at FROM knowledge_generation_sources "
                    "WHERE generation_id=?",
                    (generation_id, active),
                )
                connection.execute(
                    "INSERT INTO repository_maps(generation_id,project_id,map_version,status,map_json,map_hash,created_at) "
                    "SELECT ?,project_id,map_version,status,map_json,map_hash,created_at FROM repository_maps "
                    "WHERE generation_id=? AND status='active'",
                    (generation_id, active),
                )
        return generation_id

    def drop_repository_sources(self, generation_id: str) -> None:
        with self._write() as connection:
            connection.execute(
                "DELETE FROM knowledge_generation_sources WHERE generation_id=? AND source_id IN "
                "(SELECT source_id FROM knowledge_sources WHERE source_origin='repository')",
                (generation_id,),
            )

    def active_source(
        self,
        project_id: str,
        source_uri: str,
        source_kind: SourceKind,
        source_origin: str,
    ) -> sqlite3.Row | None:
        with self._read() as connection:
            return connection.execute(
                "SELECT s.*,v.version_id,v.source_hash,v.parser_version AS active_parser_version,"
                "gs.sensitivity AS active_sensitivity,gs.size_bytes AS active_size_bytes,"
                "gs.mtime_ns AS active_mtime_ns,gs.project_commit_sha AS active_commit_sha,"
                "gs.worktree_revision AS active_worktree_revision,"
                "gs.derivation_version AS active_derivation_version,"
                "gs.policy_version AS active_policy_version FROM knowledge_projects p "
                "JOIN knowledge_generation_sources gs ON gs.generation_id=p.active_generation_id "
                "JOIN knowledge_sources s ON s.source_id=gs.source_id "
                "JOIN knowledge_source_versions v ON v.version_id=gs.version_id "
                "WHERE p.project_id=? AND s.source_uri=? AND s.source_kind=? AND s.source_origin=?",
                (project_id, source_uri, source_kind.value, source_origin),
            ).fetchone()

    def stage_source_version(
        self,
        *,
        generation_id: str,
        project_id: str,
        owner_id: str,
        source_uri: str,
        source_kind: SourceKind,
        source_origin: str,
        sensitivity: str,
        source_hash: str,
        size_bytes: int,
        mtime_ns: int,
        parser: str,
        derivation_version: str,
        project_commit_sha: str | None,
        worktree_revision: str | None,
        policy_version: str,
        fragments: Sequence[ParsedFragment],
        facts_by_ordinal: dict[int, Sequence[ExtractedFact]],
        renamed_from_source_id: str | None = None,
    ) -> tuple[str, str, int, int]:
        source_id = _stable_id("source", project_id, source_kind.value, source_origin, source_uri)
        version_id = _stable_id("version", source_id, source_hash, derivation_version)
        timestamp = _now()
        inserted_fragments = 0
        inserted_facts = 0
        with self._write() as connection:
            generation = connection.execute(
                "SELECT status FROM knowledge_generations WHERE generation_id=? AND project_id=?",
                (generation_id, project_id),
            ).fetchone()
            if generation is None or generation["status"] != "building":
                raise KnowledgeStoreError("generation is not buildable")
            existing_source = connection.execute(
                "SELECT sensitivity FROM knowledge_sources WHERE project_id=? AND source_uri=? "
                "AND source_kind=? AND source_origin=?",
                (project_id, source_uri, source_kind.value, source_origin),
            ).fetchone()
            effective_sensitivity = (
                _max_sensitivity(str(existing_source["sensitivity"]), sensitivity)
                if existing_source else sensitivity
            )
            connection.execute(
                "INSERT INTO knowledge_sources(source_id,project_id,owner_id,source_uri,source_kind,source_origin,sensitivity,approval_status,status,current_hash,current_size_bytes,current_mtime_ns,parser,parser_version,project_commit_sha,renamed_from_source_id,created_at,updated_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,'approved',?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,source_uri,source_kind,source_origin) DO UPDATE SET "
                "source_id=knowledge_sources.source_id",
                (
                    source_id, project_id, owner_id, source_uri, source_kind.value, source_origin, effective_sensitivity,
                    SourceStatus.ALLOWED.value, source_hash, size_bytes, mtime_ns, parser,
                    PARSER_VERSION, project_commit_sha, renamed_from_source_id,
                    timestamp, timestamp, timestamp,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_source_versions(version_id,source_id,source_hash,source_origin,sensitivity,size_bytes,mtime_ns,parser,parser_version,derivation_version,project_commit_sha,renamed_from_source_id,observed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, source_id, source_hash, source_origin, effective_sensitivity, size_bytes, mtime_ns, parser, PARSER_VERSION, derivation_version, project_commit_sha, renamed_from_source_id, timestamp),
            )
            version_exists = connection.execute(
                "SELECT count(*) FROM knowledge_fragments WHERE version_id=?",
                (version_id,),
            ).fetchone()[0]
            if not version_exists:
                for fragment in fragments:
                    content_hash = _hash_text(fragment.content)
                    fragment_id = _stable_id("fragment", version_id, str(fragment.ordinal), content_hash)
                    connection.execute(
                        "INSERT INTO knowledge_fragments(fragment_id,version_id,ordinal,locator,start_line,end_line,title,content,content_hash,extraction_method,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            fragment_id, version_id, fragment.ordinal, fragment.locator,
                            fragment.start_line, fragment.end_line, fragment.title,
                            fragment.content, content_hash, fragment.extraction_method, timestamp,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO knowledge_fragments_fts(fragment_id,title,content) VALUES(?,?,?)",
                        (
                            fragment_id,
                            f"{source_uri} {fragment.title or ''}".strip(),
                            fragment.content,
                        ),
                    )
                    inserted_fragments += 1
                    for fact in facts_by_ordinal.get(fragment.ordinal, ()):
                        value_hash = _hash_text(fact.value)
                        fact_id = _stable_id("fact", fragment_id, fact.kind.value, fact.key, value_hash)
                        connection.execute(
                            "INSERT INTO knowledge_facts(fact_id,version_id,fragment_id,fact_kind,fact_key,fact_value,value_hash,extraction_method,status,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                fact_id, version_id, fragment_id, fact.kind.value, fact.key,
                                fact.value, value_hash, fact.extraction_method,
                                FactStatus.CANDIDATE.value, timestamp,
                            ),
                        )
                        inserted_facts += 1
            connection.execute(
                "INSERT INTO knowledge_generation_sources("
                "generation_id,source_id,version_id,source_origin,sensitivity,size_bytes,mtime_ns,"
                "project_commit_sha,worktree_revision,derivation_version,policy_version,"
                "renamed_from_source_id,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(generation_id,source_id) DO UPDATE SET "
                "version_id=excluded.version_id,source_origin=excluded.source_origin,"
                "sensitivity=excluded.sensitivity,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,"
                "project_commit_sha=excluded.project_commit_sha,worktree_revision=excluded.worktree_revision,"
                "derivation_version=excluded.derivation_version,policy_version=excluded.policy_version,"
                "renamed_from_source_id=excluded.renamed_from_source_id,observed_at=excluded.observed_at",
                (
                    generation_id, source_id, version_id, source_origin, effective_sensitivity,
                    size_bytes, mtime_ns, project_commit_sha, worktree_revision,
                    derivation_version, policy_version, renamed_from_source_id, timestamp,
                ),
            )
        return source_id, version_id, inserted_fragments, inserted_facts

    def find_rename_candidate(
        self,
        project_id: str,
        source_hash: str,
        current_source_uris: set[str],
    ) -> str | None:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT source_id,source_uri,current_hash FROM knowledge_sources "
                "WHERE project_id=? AND current_hash=? AND source_origin='repository' "
                "ORDER BY updated_at DESC",
                (project_id, source_hash),
            ).fetchall()
        for row in rows:
            if row["source_uri"] not in current_source_uris:
                return str(row["source_id"])
        return None

    @staticmethod
    def _recompute_conflicts(
        connection: sqlite3.Connection,
        generation_id: str,
        project_id: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "DELETE FROM knowledge_conflicts WHERE generation_id=?",
            (generation_id,),
        )
        connection.execute(
            "UPDATE knowledge_facts SET status=? WHERE version_id IN "
            "(SELECT version_id FROM knowledge_generation_sources WHERE generation_id=?)",
            (FactStatus.CANDIDATE.value, generation_id),
        )
        connection.execute(
            "UPDATE knowledge_fragments SET conflict_flag=0 WHERE version_id IN "
            "(SELECT version_id FROM knowledge_generation_sources WHERE generation_id=?)",
            (generation_id,),
        )
        groups = connection.execute(
            "SELECT f.fact_key,count(DISTINCT f.value_hash) AS values_count "
            "FROM knowledge_facts f JOIN knowledge_generation_sources gs ON gs.version_id=f.version_id "
            "WHERE gs.generation_id=? GROUP BY f.fact_key HAVING values_count>1",
            (generation_id,),
        ).fetchall()
        for group in groups:
            key = str(group["fact_key"])
            facts = connection.execute(
                "SELECT f.fact_id,f.fragment_id FROM knowledge_facts f "
                "JOIN knowledge_generation_sources gs ON gs.version_id=f.version_id "
                "WHERE gs.generation_id=? AND f.fact_key=? ORDER BY f.fact_id",
                (generation_id, key),
            ).fetchall()
            ids = [str(row["fact_id"]) for row in facts]
            connection.executemany(
                "UPDATE knowledge_facts SET status=? WHERE fact_id=?",
                ((FactStatus.CONFLICTED.value, fact_id) for fact_id in ids),
            )
            connection.executemany(
                "UPDATE knowledge_fragments SET conflict_flag=1 WHERE fragment_id=?",
                ((str(row["fragment_id"]),) for row in facts),
            )
            conflict_id = _stable_id("conflict", generation_id, key)
            connection.execute(
                "INSERT INTO knowledge_conflicts(conflict_id,generation_id,project_id,fact_key,fact_ids_json,status,detected_at) "
                "VALUES(?,?,?,?,?,'open',?)",
                (conflict_id, generation_id, project_id, key, json.dumps(ids), timestamp),
            )

    def activate_generation(self, project_id: str, generation_id: str) -> None:
        timestamp = _now()
        with self._write() as connection:
            generation = connection.execute(
                "SELECT status,base_generation_id,base_mutation_epoch FROM knowledge_generations "
                "WHERE generation_id=? AND project_id=?",
                (generation_id, project_id),
            ).fetchone()
            if generation is None or generation["status"] != "building":
                raise KnowledgeStoreError("generation cannot be activated")
            project = connection.execute(
                "SELECT active_generation_id,mutation_epoch FROM knowledge_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            previous = project["active_generation_id"]
            if previous != generation["base_generation_id"]:
                raise KnowledgeStoreError("active generation changed during build")
            if int(project["mutation_epoch"]) != int(generation["base_mutation_epoch"]):
                raise KnowledgeStoreError("project mutation epoch changed during build")
            self._recompute_conflicts(connection, generation_id, project_id, timestamp)
            if previous:
                connection.execute(
                    "UPDATE knowledge_generations SET status='superseded' WHERE generation_id=?",
                    (previous,),
                )
            connection.execute(
                "UPDATE knowledge_generations SET status='active',completed_at=? WHERE generation_id=?",
                (timestamp, generation_id),
            )
            connection.execute(
                "UPDATE knowledge_projects SET active_generation_id=?,updated_at=? WHERE project_id=?",
                (generation_id, timestamp, project_id),
            )
            connection.execute(
                "UPDATE knowledge_sources SET status=?,updated_at=? WHERE project_id=?",
                (SourceStatus.STALE.value, timestamp, project_id),
            )
            connection.execute(
                "UPDATE knowledge_sources SET status=?,updated_at=? WHERE source_id IN "
                "(SELECT source_id FROM knowledge_generation_sources WHERE generation_id=?)",
                (SourceStatus.IMPORTED.value, timestamp, generation_id),
            )
            connection.execute(
                "UPDATE knowledge_sources SET "
                "current_hash=(SELECT v.source_hash FROM knowledge_generation_sources gs "
                "JOIN knowledge_source_versions v ON v.version_id=gs.version_id "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "current_size_bytes=(SELECT gs.size_bytes FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "current_mtime_ns=(SELECT gs.mtime_ns FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "parser=(SELECT v.parser FROM knowledge_generation_sources gs "
                "JOIN knowledge_source_versions v ON v.version_id=gs.version_id "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "parser_version=(SELECT v.parser_version FROM knowledge_generation_sources gs "
                "JOIN knowledge_source_versions v ON v.version_id=gs.version_id "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "project_commit_sha=(SELECT gs.project_commit_sha FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "sensitivity=CASE "
                "WHEN sensitivity='sensitive' OR (SELECT gs.sensitivity FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id)='sensitive' THEN 'sensitive' "
                "WHEN sensitivity='internal' OR (SELECT gs.sensitivity FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id)='internal' THEN 'internal' "
                "ELSE 'public' END,"
                "renamed_from_source_id=(SELECT gs.renamed_from_source_id FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND gs.source_id=knowledge_sources.source_id),"
                "last_seen_at=? "
                "WHERE source_id IN (SELECT source_id FROM knowledge_generation_sources WHERE generation_id=?)",
                (
                    generation_id, generation_id, generation_id, generation_id, generation_id,
                    generation_id, generation_id, generation_id, generation_id, timestamp,
                    generation_id,
                ),
            )

    def fail_generation(self, generation_id: str, reason_code: str) -> None:
        with self._write() as connection:
            generation = connection.execute(
                "SELECT g.project_id,p.owner_id FROM knowledge_generations g "
                "JOIN knowledge_projects p ON p.project_id=g.project_id "
                "WHERE g.generation_id=? AND g.status IN ('building','failed')",
                (generation_id,),
            ).fetchone()
            if generation is None:
                return
            disposable_versions = connection.execute(
                "SELECT DISTINCT gs.version_id FROM knowledge_generation_sources gs "
                "WHERE gs.generation_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_generation_sources other "
                "WHERE other.version_id=gs.version_id AND other.generation_id<>?)",
                (generation_id, generation_id),
            ).fetchall()
            version_ids = [str(row["version_id"]) for row in disposable_versions]
            if version_ids:
                placeholders = ",".join("?" for _ in version_ids)
                fragments = connection.execute(
                    f"SELECT fragment_id FROM knowledge_fragments WHERE version_id IN ({placeholders})",
                    version_ids,
                ).fetchall()
                connection.executemany(
                    "DELETE FROM knowledge_fragments_fts WHERE fragment_id=?",
                    ((row["fragment_id"],) for row in fragments),
                )
            connection.execute("DELETE FROM knowledge_generations WHERE generation_id=?", (generation_id,))
            if version_ids:
                placeholders = ",".join("?" for _ in version_ids)
                connection.execute(
                    f"DELETE FROM knowledge_source_versions WHERE version_id IN ({placeholders})",
                    version_ids,
                )
            connection.execute(
                "DELETE FROM knowledge_sources WHERE project_id=? AND NOT EXISTS "
                "(SELECT 1 FROM knowledge_source_versions v WHERE v.source_id=knowledge_sources.source_id)",
                (generation["project_id"],),
            )
            connection.execute(
                "INSERT INTO knowledge_audit_log(occurred_at,owner_id,project_id,action,outcome,reason_code) "
                "VALUES(?,?,?,'build','failed',?)",
                (_now(), generation["owner_id"], generation["project_id"], reason_code[:128]),
            )
        self._checkpoint_truncate()

    def save_repository_map(self, generation_id: str, project_id: str, repository_map: RepositoryMapV1) -> None:
        payload = repository_map.model_dump_json()
        with self._write() as connection:
            connection.execute(
                "INSERT INTO repository_maps(generation_id,project_id,map_version,status,map_json,map_hash,created_at) "
                "VALUES(?,?,'1.0','active',?,?,?) ON CONFLICT(generation_id) DO UPDATE SET "
                "project_id=excluded.project_id,map_version=excluded.map_version,status='active',"
                "map_json=excluded.map_json,map_hash=excluded.map_hash,created_at=excluded.created_at",
                (generation_id, project_id, payload, _hash_text(payload), _now()),
            )

    def _checkpoint_truncate(self) -> bool:
        with closing(open_knowledge_database(self.database_path)) as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return bool(row is not None and int(row[0]) == 0 and int(row[1]) == int(row[2]))

    def repository_map(self, owner_id: str, canonical_path: str) -> RepositoryMapV1 | None:
        if not self.database_path.is_file():
            return None
        with self._read() as connection:
            row = connection.execute(
                "SELECT rm.map_json FROM knowledge_projects p JOIN repository_maps rm "
                "ON rm.generation_id=p.active_generation_id "
                "WHERE p.owner_id=? AND p.canonical_path=? AND rm.status='active'",
                (owner_id, canonical_path),
            ).fetchone()
        return RepositoryMapV1.model_validate_json(row["map_json"]) if row else None

    def retrieve(
        self,
        request: RetrievalRequestV1,
        *,
        candidate_pool: bool = False,
        candidate_offset: int = 0,
    ) -> RetrievalResultV1:
        query, terms = _fts_query(request.query)
        canonical = str(Path(request.project_path).resolve(strict=True))
        if request.allowed_source_types == []:
            return RetrievalResultV1(
                project_path=canonical,
                query=request.query,
                token_budget=request.token_budget,
                estimated_tokens=0,
                fragments=[],
            )
        allowed = request.allowed_source_types if request.allowed_source_types is not None else list(SourceKind)
        placeholders = ",".join("?" for _ in allowed)
        active_clause = "" if request.freshness is FreshnessRequirement.INCLUDE_STALE else "AND obs.is_active=1 "
        sql = (
            "WITH observations AS ("
            "SELECT gs.*,g.status AS generation_status,g.completed_at,g.started_at,"
            "CASE WHEN gs.generation_id=p.active_generation_id THEN 1 ELSE 0 END AS is_active,"
            "ROW_NUMBER() OVER (PARTITION BY gs.source_id,gs.version_id ORDER BY "
            "CASE WHEN gs.generation_id=p.active_generation_id THEN 0 ELSE 1 END,"
            "COALESCE(g.completed_at,g.started_at) DESC,g.generation_id DESC) AS observation_rank "
            "FROM knowledge_generation_sources gs "
            "JOIN knowledge_generations g ON g.generation_id=gs.generation_id "
            "JOIN knowledge_projects p ON p.project_id=g.project_id "
            "WHERE g.status IN ('active','superseded') "
            "AND p.owner_id=? AND p.canonical_path=?"
            "), matches AS ("
            "SELECT f.*,s.source_id,s.source_uri,s.source_kind,s.sensitivity,"
            "v.source_hash,v.parser AS version_parser,v.parser_version AS version_parser_version,"
            "obs.generation_id,obs.source_origin,obs.size_bytes,obs.mtime_ns,obs.observed_at,"
            "obs.project_commit_sha AS version_commit_sha,obs.worktree_revision,"
            "obs.derivation_version,obs.policy_version,obs.is_active,"
            "EXISTS (SELECT 1 FROM knowledge_conflicts c JOIN json_each(c.fact_ids_json) je "
            "JOIN knowledge_facts cf ON cf.fact_id=je.value "
            "WHERE c.generation_id=obs.generation_id AND cf.fragment_id=f.fragment_id) "
            "AS observation_conflict,"
            "bm25(knowledge_fragments_fts,0.0,4.0,1.0) AS rank "
            "FROM knowledge_projects p "
            "JOIN knowledge_sources s ON s.project_id=p.project_id "
            "JOIN knowledge_source_versions v ON v.source_id=s.source_id "
            "JOIN observations obs ON obs.source_id=s.source_id AND obs.version_id=v.version_id "
            "AND obs.observation_rank=1 "
            "JOIN knowledge_fragments f ON f.version_id=v.version_id "
            "JOIN knowledge_fragments_fts ON knowledge_fragments_fts.fragment_id=f.fragment_id "
            f"WHERE p.owner_id=? AND p.canonical_path=? AND s.source_kind IN ({placeholders}) "
            f"{active_clause} AND knowledge_fragments_fts MATCH ?"
            "), balanced AS ("
            "SELECT matches.*,ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY rank,ordinal) AS source_rank "
            "FROM matches"
            ") SELECT * FROM balanced WHERE source_rank<=3 "
            "ORDER BY is_active DESC,rank ASC,source_uri ASC,ordinal ASC LIMIT ? OFFSET ?"
        )
        try:
            with self._read() as connection:
                rows = connection.execute(
                    sql,
                    (
                        request.owner_id,
                        canonical,
                        request.owner_id,
                        canonical,
                        *(kind.value for kind in allowed),
                        query,
                        257 if candidate_pool else 256,
                        candidate_offset if candidate_pool else 0,
                    ),
                ).fetchall()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise KnowledgeStoreError("knowledge retrieval unavailable") from exc
        has_more = candidate_pool and len(rows) > 256
        if candidate_pool:
            rows = rows[:256]
        selected: list[RetrievedFragmentV1] = []
        remaining = request.token_budget
        per_source: dict[str, int] = {}
        selected_content_hashes: set[str] = set()
        for row in rows:
            source_id = str(row["source_id"])
            if per_source.get(source_id, 0) >= 3:
                continue
            content_hash = str(row["content_hash"])
            if not candidate_pool and content_hash in selected_content_hashes:
                continue
            content = str(row["content"])
            tokens = _estimated_tokens(content)
            if not candidate_pool and tokens > remaining:
                continue
            rank = float(row["rank"])
            matched = [term for term in terms if term in content.casefold()]
            score = min(1.0, max(0.0, 0.55 + min(0.4, abs(rank) * 1000) + min(0.05, len(matched) * 0.01)))
            stale = not bool(row["is_active"])
            observed = datetime.fromisoformat(str(row["observed_at"]))
            selected.append(
                RetrievedFragmentV1(
                    fragment_id=str(row["fragment_id"]),
                    source_kind=SourceKind(str(row["source_kind"])),
                    content=content,
                    title=row["title"],
                    provenance=ProvenanceV1(
                        generation_id=str(row["generation_id"]),
                        source_id=source_id,
                        source_uri=str(row["source_uri"]),
                        source_origin=str(row["source_origin"]),
                        source_hash=str(row["source_hash"]),
                        source_size_bytes=int(row["size_bytes"]),
                        source_mtime_ns=int(row["mtime_ns"]),
                        fragment_locator=str(row["locator"]),
                        start_line=row["start_line"],
                        end_line=row["end_line"],
                        parser=str(row["version_parser"]),
                        parser_version=str(row["version_parser_version"]),
                        derivation_version=str(row["derivation_version"]),
                        policy_version=str(row["policy_version"]),
                        extraction_method=str(row["extraction_method"]),
                        observed_at=observed,
                        project_commit_sha=row["version_commit_sha"],
                        worktree_revision=row["worktree_revision"],
                        sensitivity=str(row["sensitivity"]),
                        status="stale" if stale else "active",
                    ),
                    score=score,
                    reason=f"SQLite FTS5 BM25; matched terms: {', '.join(matched[:6]) or 'lexical'}",
                    estimated_tokens=tokens,
                    stale=stale,
                    conflict=bool(row["observation_conflict"]),
                )
            )
            if not candidate_pool:
                remaining -= tokens
            per_source[source_id] = per_source.get(source_id, 0) + 1
            if not candidate_pool:
                selected_content_hashes.add(content_hash)
            if len(selected) >= (256 if candidate_pool else request.max_fragments):
                break
        estimated_tokens = sum(item.estimated_tokens for item in selected)
        return RetrievalResultV1(
            project_path=canonical,
            query=request.query,
            # Candidate pages are an internal freshness-validation pool.  They
            # must contain every SQL row in the page; otherwise advancing the
            # offset would silently skip candidates that did not fit a
            # temporary token budget.  The engine applies the user budget only
            # after validating freshness and deduplicating.
            token_budget=max(request.token_budget, estimated_tokens) if candidate_pool else request.token_budget,
            estimated_tokens=estimated_tokens,
            fragments=selected,
            next_offset=(candidate_offset + 256) if has_more else None,
        )

    def list_candidates(self, owner_id: str, canonical_path: str) -> list[dict[str, object]]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT f.fact_id,f.fact_kind,f.fact_key,f.fact_value,"
                "CASE WHEN EXISTS (SELECT 1 FROM knowledge_conflicts c JOIN json_each(c.fact_ids_json) je "
                "ON je.value=f.fact_id WHERE c.generation_id=p.active_generation_id) "
                "THEN 'conflicted' ELSE 'candidate' END AS status,"
                "s.source_id,s.source_uri,v.source_hash,fr.locator "
                "FROM knowledge_projects p JOIN knowledge_generation_sources gs ON gs.generation_id=p.active_generation_id "
                "JOIN knowledge_sources s ON s.source_id=gs.source_id "
                "JOIN knowledge_source_versions v ON v.version_id=gs.version_id "
                "JOIN knowledge_facts f ON f.version_id=v.version_id "
                "JOIN knowledge_fragments fr ON fr.fragment_id=f.fragment_id "
                "WHERE p.owner_id=? AND p.canonical_path=? ORDER BY f.fact_key,f.fact_id",
                (owner_id, canonical_path),
            ).fetchall()
        return [dict(row) for row in rows]

    def candidate(
        self,
        fact_id: str,
        *,
        owner_id: str,
        canonical_path: str,
    ) -> dict[str, object] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT f.*,CASE WHEN EXISTS (SELECT 1 FROM knowledge_conflicts c "
                "JOIN json_each(c.fact_ids_json) je ON je.value=f.fact_id "
                "WHERE c.generation_id=p.active_generation_id) THEN 'conflicted' ELSE 'candidate' END AS active_status,"
                "s.source_id,s.source_uri,s.source_kind,s.source_origin,s.sensitivity,s.project_id,"
                "p.owner_id,p.canonical_path,v.source_hash,v.parser,v.parser_version,fr.locator,"
                "fr.extraction_method,gs.generation_id,gs.size_bytes,gs.mtime_ns,gs.project_commit_sha,"
                "gs.worktree_revision,gs.derivation_version,gs.policy_version,gs.observed_at "
                "FROM knowledge_facts f JOIN knowledge_source_versions v ON v.version_id=f.version_id "
                "JOIN knowledge_sources s ON s.source_id=v.source_id "
                "JOIN knowledge_projects p ON p.project_id=s.project_id "
                "JOIN knowledge_generation_sources gs ON gs.generation_id=p.active_generation_id "
                "AND gs.source_id=s.source_id AND gs.version_id=v.version_id "
                "JOIN knowledge_fragments fr ON fr.fragment_id=f.fragment_id "
                "WHERE f.fact_id=? AND p.owner_id=? AND p.canonical_path=?",
                (fact_id, owner_id, canonical_path),
            ).fetchone()
        return dict(row) if row else None

    def purge_source(
        self,
        source_id: str,
        *,
        owner_id: str,
        project_path: str,
        apply: bool = False,
    ) -> dict[str, object]:
        supplied_path = os.path.normcase(os.path.abspath(os.path.expanduser(project_path)))
        with self._read() as connection:
            source = connection.execute(
                "SELECT s.source_id,s.project_id,s.owner_id,s.source_uri,s.source_origin,p.canonical_path "
                "FROM knowledge_sources s "
                "JOIN knowledge_projects p ON p.project_id=s.project_id "
                "WHERE s.source_id=? AND s.owner_id=?",
                (source_id, owner_id),
            ).fetchone()
            if source is None or os.path.normcase(os.path.abspath(str(source["canonical_path"]))) != supplied_path:
                raise KnowledgeStoreError("source not found")
            counts = {
                "versions": int(connection.execute("SELECT count(*) FROM knowledge_source_versions WHERE source_id=?", (source_id,)).fetchone()[0]),
                "fragments": int(connection.execute("SELECT count(*) FROM knowledge_fragments WHERE version_id IN (SELECT version_id FROM knowledge_source_versions WHERE source_id=?)", (source_id,)).fetchone()[0]),
                "facts": int(connection.execute("SELECT count(*) FROM knowledge_facts WHERE version_id IN (SELECT version_id FROM knowledge_source_versions WHERE source_id=?)", (source_id,)).fetchone()[0]),
            }
        if not apply:
            return {
                "source_id": source_id,
                "source_uri": source["source_uri"],
                "apply": False,
                "counts": counts,
                "logical_purge_complete": False,
                "physical_purge_complete": False,
            }
        if not self._checkpoint_truncate():
            raise KnowledgeStoreError("purge blocked by active database reader")
        with self._write() as connection:
            scoped = connection.execute(
                "SELECT s.source_id FROM knowledge_sources s JOIN knowledge_projects p ON p.project_id=s.project_id "
                "WHERE s.source_id=? AND s.owner_id=? AND p.canonical_path=?",
                (source_id, owner_id, source["canonical_path"]),
            ).fetchone()
            if scoped is None:
                raise KnowledgeStoreError("source not found")
            connection.execute(
                "UPDATE knowledge_projects SET mutation_epoch=mutation_epoch+1,updated_at=? "
                "WHERE project_id=?",
                (_now(), source["project_id"]),
            )
            connection.execute(
                "UPDATE knowledge_generations SET status='failed',completed_at=? "
                "WHERE project_id=? AND status='building'",
                (_now(), source["project_id"]),
            )
            fragment_rows = connection.execute(
                "SELECT fragment_id FROM knowledge_fragments WHERE version_id IN "
                "(SELECT version_id FROM knowledge_source_versions WHERE source_id=?)",
                (source_id,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM knowledge_fragments_fts WHERE fragment_id=?",
                ((row["fragment_id"],) for row in fragment_rows),
            )
            generation_rows = connection.execute(
                "SELECT DISTINCT generation_id FROM knowledge_generation_sources WHERE source_id=?",
                (source_id,),
            ).fetchall()
            connection.execute("DELETE FROM knowledge_sources WHERE source_id=?", (source_id,))
            for row in generation_rows:
                generation_id = str(row["generation_id"])
                if source["source_origin"] == "repository":
                    connection.execute(
                        "DELETE FROM repository_maps WHERE generation_id=?",
                        (generation_id,),
                    )
                    connection.execute(
                        "UPDATE knowledge_generations SET worktree_revision=NULL WHERE generation_id=?",
                        (generation_id,),
                    )
                self._recompute_conflicts(
                    connection,
                    generation_id,
                    str(source["project_id"]),
                    _now(),
                )
            connection.execute(
                "INSERT INTO knowledge_audit_log(occurred_at,owner_id,project_id,source_id,action,outcome) "
                "VALUES(?,?,?,?, 'purge_source','logical_complete')",
                (_now(), source["owner_id"], source["project_id"], source_id),
            )
            connection.execute(
                "INSERT INTO knowledge_fragments_fts(knowledge_fragments_fts) VALUES('optimize')"
            )
        physical_complete = self._checkpoint_truncate()
        if physical_complete:
            try:
                with closing(open_knowledge_database(self.database_path)) as connection:
                    connection.execute("VACUUM")
                physical_complete = self._checkpoint_truncate()
            except sqlite3.DatabaseError:
                physical_complete = False
        self.audit(
            owner_id=str(source["owner_id"]),
            project_id=str(source["project_id"]),
            source_id=source_id,
            action="purge_compact",
            outcome="physical_complete" if physical_complete else "deferred",
            reason_code=None if physical_complete else "purge.physical_deferred",
        )
        return {
            "source_id": source_id,
            "source_uri": source["source_uri"],
            "apply": True,
            "counts": counts,
            "logical_purge_complete": True,
            "physical_purge_complete": physical_complete,
            "reason_code": None if physical_complete else "purge.physical_deferred",
        }

    def compact_storage(self) -> bool:
        if not self._checkpoint_truncate():
            return False
        try:
            with closing(open_knowledge_database(self.database_path)) as connection:
                connection.execute("VACUUM")
        except sqlite3.DatabaseError:
            return False
        return self._checkpoint_truncate()

    def audit(self, *, owner_id: str, project_id: str | None, source_id: str | None, action: str, outcome: str, reason_code: str | None = None) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO knowledge_audit_log(occurred_at,owner_id,project_id,source_id,action,outcome,reason_code) VALUES(?,?,?,?,?,?,?)",
                (_now(), owner_id, project_id, source_id, action[:64], outcome[:32], reason_code[:128] if reason_code else None),
            )
