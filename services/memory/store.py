from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from services.memory.contracts import (
    ArchiveReferenceValueV1,
    MEMORY_RECORD_SCHEMA_VERSION,
    MemoryRecordType,
    MemoryRecordV1,
    MemoryRetention,
    MemoryScope,
    MemorySensitivity,
    MemorySourceV1,
    MemoryStatus,
    MemoryUpsertV1,
    OperationalStateValueV1,
    RetrievalItemV1,
    RetrievalResultV1,
    TaskHistoryValueV1,
)
from services.memory.migrations import migrate_database, open_database
from services.memory.privacy import (
    MemoryPrivacyError,
    Sensitivity,
    inspect_memory_payload,
    normalize_source_reference,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / "data" / "memory.sqlite3"
MEMORY_POLICY_VERSION = "stage003-v1"
_ACTIVE_STATUSES = {
    MemoryStatus.CANDIDATE.value,
    MemoryStatus.CONFIRMED.value,
    MemoryStatus.CONFLICTED.value,
}
_RETRIEVABLE_TYPES = {
    MemoryRecordType.USER_PROFILE.value,
    MemoryRecordType.PROJECT_KNOWLEDGE.value,
    MemoryRecordType.TASK_HISTORY.value,
}
_OBJECTIVE_SOURCES = {"file", "git", "manifest", "task_state", "tool_result", "test_result"}
_MODEL_PRODUCER = re.compile(r"(?i)(?:model|llm|qwen|ollama|assistant|agent)")
_SAFE_ACTOR = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_AUDIT_ACTORS = {
    "knowledge-engine",
    "knowledge-purge",
    "local-user",
    "memory-invalidator",
    "memory-retention",
    "trusted-adapter",
}
_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)
_RETRIEVAL_STOPWORDS = {
    "memory", "please", "project", "repo", "repository", "task",
    "память", "пожалуйста", "проект", "репозиторий", "задача",
}
_ARCHIVE_CONTENT_KEYS = {
    "body", "chat", "completion", "content", "conversation", "data",
    "history", "messages", "output", "payload", "prompt", "response",
    "result", "stderr", "stdout", "text", "tool_output", "transcript",
}

_MIGRATED_DATABASES: set[Path] = set()
_MIGRATION_LOCK = threading.Lock()


def _contains_forbidden_archive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _ARCHIVE_CONTENT_KEYS:
                return True
            if _contains_forbidden_archive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_archive_key(item) for item in value)
    return False


class MemoryStoreError(RuntimeError):
    """Safe storage error which never embeds stored payload."""


class MemoryNotFoundError(MemoryStoreError):
    pass


class MemoryRevisionError(MemoryStoreError):
    pass


class MemoryPolicyError(MemoryStoreError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("memory timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def canonical_project_path(project_path: str | Path | None) -> str | None:
    if project_path is None:
        return None
    raw = str(project_path).strip()
    if not raw:
        return None
    return os.path.normcase(str(Path(raw).expanduser().resolve(strict=False)))


def _scope_key(scope: MemoryScope, owner_id: str, project_path: str | None, task_id: str | None) -> str:
    if scope is MemoryScope.USER:
        return f"user:{owner_id}"
    if scope is MemoryScope.PROJECT:
        assert project_path
        return f"project:{project_path}"
    assert task_id
    project_part = project_path or "no-project"
    return f"task:{project_part}:{task_id}"


def _normalize_source_uri(uri: str | None, project_path: str | None) -> str | None:
    if uri is None:
        return None
    value = normalize_source_reference(uri)
    if not value:
        return None
    if "://" in value:
        scheme, remainder = value.split("://", 1)
        scheme = scheme.casefold()
        if scheme not in {"archive", "git", "http", "https", "local-file", "project", "task", "tool", "user"}:
            raise MemoryPolicyError("source URI scheme is not allowed")
        if scheme == "project":
            parts = Path(remainder.replace("/", os.sep)).parts
            if not remainder or Path(remainder).is_absolute() or ".." in parts:
                raise MemoryPolicyError("project source URI must be a relative contained path")
        return scheme + "://" + remainder
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(
        r"^[A-Za-z]:[\\/]", value
    ):
        raise MemoryPolicyError("source URI scheme is not allowed")
    candidate = Path(value).expanduser()
    if project_path:
        project = Path(project_path)
        if not candidate.is_absolute():
            candidate = project / candidate
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(project.resolve(strict=False))
        except ValueError as exc:
            raise MemoryPolicyError("local source must remain inside project scope") from exc
        return "project://" + relative.as_posix()
    if candidate.is_absolute():
        return "local-file://" + candidate.resolve(strict=False).as_posix()
    return value


class MemoryStore:
    """SQLite repository for explicit, scoped and inspectable memory records."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        auto_migrate: bool = True,
        create_migration_backup: bool = True,
    ) -> None:
        self.database_path = Path(database_path or DEFAULT_DATABASE).resolve()
        if auto_migrate:
            self.ensure_migrated(create_backup=create_migration_backup)

    def ensure_migrated(self, *, create_backup: bool = True) -> None:
        with _MIGRATION_LOCK:
            if self.database_path in _MIGRATED_DATABASES:
                return
            migrate_database(self.database_path, create_backup=create_backup)
            _MIGRATED_DATABASES.add(self.database_path)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        return open_database(self.database_path, readonly=readonly)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        record_id: str | None,
        record_type: str | None,
        scope_key: str,
        actor: str,
        action: str,
        reason_code: str,
        before_status: str | None,
        after_status: str | None,
        affected_count: int = 1,
        outcome: str = "success",
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_audit_log
                (operation_id, record_id, record_type, scope_hash, actor, action,
                 outcome, reason_code, before_status, after_status, occurred_at,
                 affected_count, policy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                record_id,
                record_type,
                _sha256(scope_key),
                actor if actor in _AUDIT_ACTORS else "external-actor",
                action,
                outcome,
                reason_code,
                before_status,
                after_status,
                _iso(_utc_now()),
                affected_count,
                MEMORY_POLICY_VERSION,
            ),
        )

    @staticmethod
    def _validate_initial_confirmation(
        request: MemoryUpsertV1, *, trusted_objective: bool
    ) -> None:
        if request.status is not MemoryStatus.CONFIRMED:
            return
        source = request.source
        objective = source.source_type in _OBJECTIVE_SOURCES and bool(
            source.source_hash or source.source_commit_sha or request.project_commit_sha
        )
        if not trusted_objective or not objective or _MODEL_PRODUCER.search(source.producer):
            raise MemoryPolicyError(
                "initial confirmed status is restricted to a trusted hashed adapter; use explicit confirm"
            )

    @staticmethod
    def _normalize_request(request: MemoryUpsertV1) -> MemoryUpsertV1:
        source = request.source
        if (
            request.project_commit_sha
            and source.source_commit_sha
            and request.project_commit_sha.casefold()
            != source.source_commit_sha.casefold()
        ):
            raise MemoryPolicyError("record and source commit revisions must agree")
        composite = {
            "subject": request.subject,
            "value": request.value,
            "owner_id": request.owner_id,
            "task_id": request.task_id,
            "source_type": source.source_type,
            "source_fragment": source.fragment,
            "producer": source.producer,
            "author": source.author,
        }
        decision = inspect_memory_payload(composite, source_uri=source.uri)
        if not decision.allowed:
            reason = decision.reason_codes[0] if decision.reason_codes else "privacy.rejected"
            raise MemoryPrivacyError(reason, decision.reason_codes[1:])
        normalized = decision.normalized_value
        detected = {
            Sensitivity.PUBLIC: MemorySensitivity.PUBLIC,
            Sensitivity.INTERNAL: MemorySensitivity.INTERNAL,
            Sensitivity.SENSITIVE: MemorySensitivity.SENSITIVE,
        }[decision.sensitivity]
        rank = {
            MemorySensitivity.PUBLIC: 0,
            MemorySensitivity.INTERNAL: 1,
            MemorySensitivity.SENSITIVE: 2,
        }
        sensitivity = max((request.sensitivity, detected), key=rank.__getitem__)
        normalized_source = MemorySourceV1.model_validate(
            {
                **source.model_dump(mode="python"),
                "source_type": normalized["source_type"],
                "uri": (
                    normalize_source_reference(source.uri)
                    if source.uri is not None
                    else None
                ),
                "fragment": normalized["source_fragment"],
                "source_commit_sha": (
                    (source.source_commit_sha or request.project_commit_sha).lower()
                    if (source.source_commit_sha or request.project_commit_sha)
                    else None
                ),
                "producer": normalized["producer"],
                "author": normalized["author"],
            }
        )
        return MemoryUpsertV1.model_validate(
            {
                **request.model_dump(mode="python"),
                "subject": normalized["subject"],
                "value": normalized["value"],
                "owner_id": normalized["owner_id"],
                "project_path": (
                    normalize_source_reference(request.project_path)
                    if request.project_path is not None
                    else None
                ),
                "task_id": normalized["task_id"],
                "source": normalized_source,
                "sensitivity": sensitivity,
            }
        )

    def _identity(self, request: MemoryUpsertV1) -> tuple[str | None, str, str, str, str]:
        project = canonical_project_path(request.project_path)
        scope_key = _scope_key(request.scope, request.owner_id, project, request.task_id)
        value_json = _canonical_json(request.value)
        value_hash = _sha256(value_json)
        dedupe_key = _sha256(
            "\x1f".join(
                [request.owner_id, scope_key, request.record_type.value, request.subject, value_hash]
            )
        )
        return project, scope_key, value_json, value_hash, dedupe_key

    @staticmethod
    def _source_key(record_id: str, source: MemorySourceV1, uri: str | None) -> str:
        return _sha256(
            "\x1f".join(
                [
                    record_id,
                    source.source_type,
                    uri or "",
                    source.fragment or "",
                    source.source_hash or "",
                    source.source_commit_sha or "",
                    str(source.source_mtime_ns or ""),
                ]
            )
        )

    def _add_source(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        source: MemorySourceV1,
        project_path: str | None,
    ) -> bool:
        uri = _normalize_source_uri(source.uri, project_path)
        key = self._source_key(record_id, source, uri)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO memory_sources
                (source_id, record_id, source_type, source_uri, source_fragment,
                 source_hash, source_commit_sha, source_mtime_ns, observed_at,
                 producer, author, source_dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "src_" + uuid.uuid4().hex,
                record_id,
                source.source_type,
                uri,
                source.fragment,
                source.source_hash.lower() if source.source_hash else None,
                source.source_commit_sha.lower() if source.source_commit_sha else None,
                source.source_mtime_ns,
                _iso(source.observed_at),
                source.producer,
                source.author,
                key,
            ),
        )
        return cursor.rowcount > 0

    def _open_conflict(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        conflicting_ids: Sequence[str],
    ) -> None:
        conflict = connection.execute(
            """
            SELECT conflict_id FROM memory_conflicts
            WHERE owner_id=? AND scope_type=? AND scope_key=? AND record_type=?
              AND memory_key=? AND status='open'
            """,
            (
                row["owner_id"], row["scope_type"], row["scope_key"],
                row["record_type"], row["memory_key"],
            ),
        ).fetchone()
        conflict_id = conflict["conflict_id"] if conflict else "conf_" + uuid.uuid4().hex
        if conflict is None:
            connection.execute(
                """
                INSERT INTO memory_conflicts
                    (conflict_id, owner_id, scope_type, scope_key, record_type,
                     memory_key, status, resolution_record_id, reason_code,
                     created_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, 'memory.value_disagreement', ?, NULL)
                """,
                (
                    conflict_id, row["owner_id"], row["scope_type"], row["scope_key"],
                    row["record_type"], row["memory_key"], _iso(_utc_now()),
                ),
            )
        all_ids = list(dict.fromkeys([row["record_id"], *conflicting_ids]))
        placeholders = ",".join("?" for _ in all_ids)
        connection.execute(
            f"""UPDATE memory_records SET status='conflicted', updated_at=?, revision=revision+1
                WHERE record_id IN ({placeholders}) AND status IN ('candidate','confirmed','conflicted')""",
            (_iso(_utc_now()), *all_ids),
        )
        for record_id in all_ids:
            connection.execute(
                "INSERT OR IGNORE INTO memory_conflict_members(conflict_id,record_id,added_at) VALUES(?,?,?)",
                (conflict_id, record_id, _iso(_utc_now())),
            )

    def _upsert_in_transaction(
        self,
        connection: sqlite3.Connection,
        request: MemoryUpsertV1,
        *,
        trusted_objective: bool = False,
    ) -> tuple[str, bool]:
        request = self._normalize_request(request)
        if request.record_type is MemoryRecordType.ARCHIVE_REFERENCE:
            if _contains_forbidden_archive_key(request.value):
                raise MemoryPolicyError("archive/chat/tool payload is not durable memory")
            archive = ArchiveReferenceValueV1.model_validate(request.value)
            if archive.uri:
                uri_decision = inspect_memory_payload({}, source_uri=archive.uri)
                if not uri_decision.allowed:
                    reason = uri_decision.reason_codes[0]
                    raise MemoryPrivacyError(reason, uri_decision.reason_codes[1:])
            value = archive.model_dump(mode="json", exclude_none=True)
            request = request.model_copy(update={"value": value})
        elif request.record_type is MemoryRecordType.TASK_HISTORY:
            value = TaskHistoryValueV1.model_validate(request.value).model_dump(
                mode="json", exclude_none=True
            )
            request = request.model_copy(update={"value": value})
        elif request.record_type is MemoryRecordType.OPERATIONAL_STATE:
            value = OperationalStateValueV1.model_validate(request.value).model_dump(
                mode="json", exclude_none=True
            )
            request = request.model_copy(update={"value": value})
        self._validate_initial_confirmation(request, trusted_objective=trusted_objective)
        project, scope_key, value_json, value_hash, dedupe_key = self._identity(request)
        if request.supersedes_record_id:
            predecessor = connection.execute(
                "SELECT * FROM memory_records WHERE record_id=?",
                (request.supersedes_record_id,),
            ).fetchone()
            if predecessor is None or any(
                (
                    predecessor["owner_id"] != request.owner_id,
                    predecessor["scope_type"] != request.scope.value,
                    predecessor["scope_key"] != scope_key,
                    predecessor["record_type"] != request.record_type.value,
                    predecessor["memory_key"] != request.subject,
                    predecessor["status"] != MemoryStatus.SUPERSEDED.value,
                )
            ):
                raise MemoryPolicyError("supersedes relation must remain inside one fact scope")
        existing = connection.execute(
            "SELECT * FROM memory_records WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if existing is not None:
            if existing["status"] in {
                MemoryStatus.DELETED.value,
                MemoryStatus.REJECTED.value,
                MemoryStatus.SUPERSEDED.value,
            }:
                raise MemoryPolicyError("terminal memory is never implicitly resurrected")
            added = self._add_source(connection, existing["record_id"], request.source, project)
            if added:
                observed_commit = request.project_commit_sha or request.source.source_commit_sha
                next_status = (
                    MemoryStatus.CANDIDATE.value
                    if existing["status"] == MemoryStatus.STALE.value
                    else existing["status"]
                )
                connection.execute(
                    """UPDATE memory_records
                       SET observed_at=?,updated_at=?,source_commit_sha=coalesce(?,source_commit_sha),
                           status=?,revision=revision+1 WHERE record_id=?""",
                    (
                        _iso(request.source.observed_at), _iso(_utc_now()), observed_commit,
                        next_status, existing["record_id"],
                    ),
                )
                self._audit(
                    connection,
                    record_id=existing["record_id"], record_type=existing["record_type"],
                    scope_key=scope_key, actor=request.actor, action="observe",
                    reason_code="memory.provenance_added", before_status=existing["status"],
                    after_status=next_status,
                )
            return str(existing["record_id"]), False

        record_id = "mem_" + uuid.uuid4().hex
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO memory_records
                (record_id, record_schema_version, record_type, owner_id, scope_type,
                 scope_key, project_realpath, task_id, memory_key, value_json,
                 value_hash, dedupe_key, status, confidence, sensitivity,
                 retention_class, expires_at, valid_from, valid_to, source_commit_sha,
                 author, producer, supersedes_record_id, created_at, observed_at,
                 updated_at, deleted_at, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (
                record_id, MEMORY_RECORD_SCHEMA_VERSION, request.record_type.value,
                request.owner_id, request.scope.value, scope_key, project, request.task_id,
                request.subject, value_json, value_hash, dedupe_key, request.status.value,
                request.confidence, request.sensitivity.value, request.retention.value,
                _iso(request.expires_at), _iso(request.valid_from), _iso(request.valid_to),
                (
                    request.project_commit_sha or request.source.source_commit_sha
                ).lower() if (request.project_commit_sha or request.source.source_commit_sha) else None,
                request.source.author, request.source.producer, request.supersedes_record_id,
                _iso(now), _iso(request.source.observed_at), _iso(now),
            ),
        )
        self._add_source(connection, record_id, request.source, project)
        conflicts = connection.execute(
            """
            SELECT record_id FROM memory_records
            WHERE owner_id=? AND scope_type=? AND scope_key=? AND record_type=?
              AND memory_key=? AND value_hash<>? AND record_id<>?
              AND status IN ('candidate','confirmed','conflicted')
            """,
            (
                request.owner_id, request.scope.value, scope_key, request.record_type.value,
                request.subject, value_hash, record_id,
            ),
        ).fetchall()
        final_status = request.status.value
        if conflicts:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id=?", (record_id,)
            ).fetchone()
            self._open_conflict(
                connection, row=row, conflicting_ids=[str(item["record_id"]) for item in conflicts]
            )
            final_status = MemoryStatus.CONFLICTED.value
        self._audit(
            connection,
            record_id=record_id, record_type=request.record_type.value, scope_key=scope_key,
            actor=request.actor, action="create", reason_code=(
                "memory.conflict_detected" if conflicts else "memory.created"
            ), before_status=None, after_status=final_status,
        )
        return record_id, True

    def upsert(
        self, request: MemoryUpsertV1, *, trusted_objective: bool = False
    ) -> MemoryRecordV1:
        with self._write() as connection:
            record_id, _ = self._upsert_in_transaction(
                connection, request, trusted_objective=trusted_objective
            )
        return self.get(record_id, owner_id=request.owner_id, include_deleted=True)

    def _sources(self, connection: sqlite3.Connection, record_id: str) -> list[MemorySourceV1]:
        rows = connection.execute(
            "SELECT * FROM memory_sources WHERE record_id=? ORDER BY observed_at,source_id",
            (record_id,),
        ).fetchall()
        return [
            MemorySourceV1(
                source_type=row["source_type"], uri=row["source_uri"],
                fragment=row["source_fragment"], source_hash=row["source_hash"],
                observed_at=_parse_time(row["observed_at"]),
                source_commit_sha=row["source_commit_sha"],
                source_mtime_ns=row["source_mtime_ns"], producer=row["producer"],
                author=row["author"],
            )
            for row in rows
        ]

    def _record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> MemoryRecordV1:
        return MemoryRecordV1(
            schema_version=row["record_schema_version"], record_id=row["record_id"],
            record_type=row["record_type"], owner_id=row["owner_id"],
            scope=row["scope_type"], scope_key=row["scope_key"],
            project_path=row["project_realpath"], task_id=row["task_id"],
            subject=row["memory_key"], value=json.loads(row["value_json"]),
            sources=self._sources(connection, row["record_id"]),
            created_at=_parse_time(row["created_at"]), observed_at=_parse_time(row["observed_at"]),
            updated_at=_parse_time(row["updated_at"]), confidence=row["confidence"],
            status=row["status"], valid_from=_parse_time(row["valid_from"]),
            valid_to=_parse_time(row["valid_to"]), project_commit_sha=row["source_commit_sha"],
            producer=row["producer"], author=row["author"],
            supersedes_record_id=row["supersedes_record_id"], sensitivity=row["sensitivity"],
            retention=row["retention_class"], expires_at=_parse_time(row["expires_at"]),
            deleted_at=_parse_time(row["deleted_at"]), revision=row["revision"],
        )

    def get(
        self, record_id: str, *, owner_id: str = "local-user", include_deleted: bool = False
    ) -> MemoryRecordV1:
        connection = self._connect(readonly=True)
        try:
            sql = "SELECT * FROM memory_records WHERE record_id=? AND owner_id=?"
            params: list[Any] = [record_id, owner_id]
            if not include_deleted:
                sql += " AND status<>'deleted'"
            row = connection.execute(sql, params).fetchone()
            if row is None:
                raise MemoryNotFoundError("memory record was not found in owner scope")
            return self._record(connection, row)
        finally:
            connection.close()

    def list_records(
        self,
        *,
        owner_id: str = "local-user",
        scope: MemoryScope | None = None,
        project_path: str | None = None,
        task_id: str | None = None,
        record_type: MemoryRecordType | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[MemoryRecordV1]:
        clauses = ["owner_id=?"]
        params: list[Any] = [owner_id]
        if scope is not None:
            clauses.append("scope_type=?")
            params.append(scope.value)
            if scope is MemoryScope.TASK and task_id is not None:
                clauses.append("scope_key=?")
                params.append(
                    _scope_key(
                        MemoryScope.TASK,
                        owner_id,
                        canonical_project_path(project_path),
                        task_id,
                    )
                )
        if project_path is not None:
            clauses.append("project_realpath=?")
            params.append(canonical_project_path(project_path))
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
            if scope is not MemoryScope.TASK and project_path is None:
                clauses.append("project_realpath IS NULL")
        if record_type is not None:
            clauses.append("record_type=?")
            params.append(record_type.value)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(item.value for item in statuses)
        elif not include_deleted:
            clauses.append("status<>'deleted'")
        params.append(max(1, min(limit, 1_000)))
        connection = self._connect(readonly=True)
        try:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE " + " AND ".join(clauses)
                + " ORDER BY updated_at DESC,record_id LIMIT ?",
                params,
            ).fetchall()
            return [self._record(connection, row) for row in rows]
        finally:
            connection.close()

    def search_records(
        self,
        query: str,
        *,
        owner_id: str = "local-user",
        scope: MemoryScope,
        project_path: str | None = None,
        task_id: str | None = None,
        record_type: MemoryRecordType | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        limit: int = 100,
    ) -> list[MemoryRecordV1]:
        """Deterministic scoped management search; never crosses scope."""

        if scope is MemoryScope.PROJECT and not project_path:
            raise MemoryPolicyError("project search requires explicit project_path")
        if scope is MemoryScope.TASK and not task_id:
            raise MemoryPolicyError("task search requires explicit task_id")
        candidates = self.list_records(
            owner_id=owner_id,
            scope=scope,
            project_path=project_path,
            task_id=task_id,
            record_type=record_type,
            statuses=statuses,
            limit=min(1_000, max(limit * 8, 64)),
        )
        terms = self._tokens(query)
        selected: list[MemoryRecordV1] = []
        for record in candidates:
            if updated_after and record.updated_at < updated_after:
                continue
            if updated_before and record.updated_at > updated_before:
                continue
            haystack = record.subject + " " + _canonical_json(record.value)
            if terms and not terms.issubset(self._tokens(haystack)):
                continue
            selected.append(record)
            if len(selected) >= max(1, min(limit, 1_000)):
                break
        return selected

    def _transition(
        self,
        record_id: str,
        *,
        owner_id: str,
        actor: str,
        target: MemoryStatus,
        reason_code: str,
        expected_revision: int | None = None,
    ) -> MemoryRecordV1:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id=? AND owner_id=?",
                (record_id, owner_id),
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError("memory record was not found in owner scope")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise MemoryRevisionError("memory record revision changed")
            current = MemoryStatus(row["status"])
            allowed = {
                MemoryStatus.CONFIRMED: {
                    MemoryStatus.CANDIDATE,
                    MemoryStatus.CONFLICTED,
                    MemoryStatus.CONFIRMED,
                },
                MemoryStatus.REJECTED: {
                    MemoryStatus.CANDIDATE,
                    MemoryStatus.CONFLICTED,
                    MemoryStatus.CONFIRMED,
                    MemoryStatus.REJECTED,
                },
                MemoryStatus.DELETED: set(MemoryStatus),
            }[target]
            if current not in allowed:
                raise MemoryPolicyError("memory lifecycle transition is not allowed")
            if target is MemoryStatus.CONFIRMED and _MODEL_PRODUCER.search(actor):
                raise MemoryPolicyError("model producers cannot confirm their own memory")
            now = _iso(_utc_now())
            affected = 1
            if target is MemoryStatus.CONFIRMED and row["status"] == MemoryStatus.CONFLICTED.value:
                conflict = connection.execute(
                    """
                    SELECT c.conflict_id FROM memory_conflicts c
                    JOIN memory_conflict_members m ON m.conflict_id=c.conflict_id
                    WHERE m.record_id=? AND c.status='open'
                    """,
                    (record_id,),
                ).fetchone()
                if conflict:
                    others = connection.execute(
                        "SELECT record_id FROM memory_conflict_members WHERE conflict_id=? AND record_id<>?",
                        (conflict["conflict_id"], record_id),
                    ).fetchall()
                    other_ids = [item["record_id"] for item in others]
                    if other_ids:
                        placeholders = ",".join("?" for _ in other_ids)
                        connection.execute(
                            f"UPDATE memory_records SET status='rejected',updated_at=?,revision=revision+1 WHERE record_id IN ({placeholders}) AND status='conflicted'",
                            (now, *other_ids),
                        )
                    connection.execute(
                        "UPDATE memory_conflicts SET status='resolved',resolution_record_id=?,resolved_at=? WHERE conflict_id=?",
                        (record_id, now, conflict["conflict_id"]),
                    )
                    affected += len(other_ids)
            deleted_at = now if target is MemoryStatus.DELETED else None
            connection.execute(
                """UPDATE memory_records
                   SET status=?,deleted_at=?,updated_at=?,revision=revision+1
                   WHERE record_id=?""",
                (target.value, deleted_at, now, record_id),
            )
            self._audit(
                connection, record_id=record_id, record_type=row["record_type"],
                scope_key=row["scope_key"], actor=actor, action=target.value,
                reason_code=reason_code, before_status=row["status"],
                after_status=target.value, affected_count=affected,
            )
        return self.get(record_id, owner_id=owner_id, include_deleted=True)

    def confirm(self, record_id: str, *, owner_id: str = "local-user", actor: str = "local-user", expected_revision: int | None = None) -> MemoryRecordV1:
        return self._transition(record_id, owner_id=owner_id, actor=actor, target=MemoryStatus.CONFIRMED, reason_code="memory.user_confirmed", expected_revision=expected_revision)

    def reject(self, record_id: str, *, owner_id: str = "local-user", actor: str = "local-user", expected_revision: int | None = None) -> MemoryRecordV1:
        return self._transition(record_id, owner_id=owner_id, actor=actor, target=MemoryStatus.REJECTED, reason_code="memory.user_rejected", expected_revision=expected_revision)

    def soft_delete(self, record_id: str, *, owner_id: str = "local-user", actor: str = "local-user") -> MemoryRecordV1:
        return self._transition(record_id, owner_id=owner_id, actor=actor, target=MemoryStatus.DELETED, reason_code="memory.user_deleted")

    def supersede(
        self,
        record_id: str,
        *,
        value: Any,
        source: MemorySourceV1,
        owner_id: str = "local-user",
        actor: str = "local-user",
        status: MemoryStatus = MemoryStatus.CANDIDATE,
    ) -> MemoryRecordV1:
        with self._write() as connection:
            old = connection.execute(
                "SELECT * FROM memory_records WHERE record_id=? AND owner_id=? AND status<>'deleted'",
                (record_id, owner_id),
            ).fetchone()
            if old is None:
                raise MemoryNotFoundError("memory record was not found in owner scope")
            normalized_probe = self._normalize_request(
                MemoryUpsertV1(
                    record_type=old["record_type"], scope=old["scope_type"],
                    subject=old["memory_key"], value=value, source=source, owner_id=owner_id,
                    project_path=old["project_realpath"], task_id=old["task_id"],
                    sensitivity=old["sensitivity"], retention=old["retention_class"],
                    expires_at=_parse_time(old["expires_at"]), actor=actor,
                )
            )
            if _sha256(_canonical_json(normalized_probe.value)) == old["value_hash"]:
                raise MemoryPolicyError("supersede requires a different normalized value")
            request = MemoryUpsertV1(
                record_type=old["record_type"], scope=old["scope_type"],
                subject=old["memory_key"], value=value, source=source, owner_id=owner_id,
                project_path=old["project_realpath"], task_id=old["task_id"], status=status,
                confidence=old["confidence"], project_commit_sha=old["source_commit_sha"],
                supersedes_record_id=record_id, sensitivity=old["sensitivity"],
                retention=old["retention_class"], expires_at=_parse_time(old["expires_at"]),
                actor=actor,
            )
            now = _iso(_utc_now())
            connection.execute(
                "UPDATE memory_records SET status='superseded',valid_to=?,updated_at=?,revision=revision+1 WHERE record_id=?",
                (now, now, record_id),
            )
            new_id, _ = self._upsert_in_transaction(connection, request)
            self._audit(
                connection, record_id=record_id, record_type=old["record_type"],
                scope_key=old["scope_key"], actor=actor, action="supersede",
                reason_code="memory.explicit_supersede", before_status=old["status"],
                after_status=MemoryStatus.SUPERSEDED.value,
            )
        return self.get(new_id, owner_id=owner_id)

    def hard_purge(
        self,
        record_id: str,
        *,
        confirm_record_id: str,
        owner_id: str = "local-user",
        actor: str = "local-user",
    ) -> None:
        if confirm_record_id != record_id:
            raise MemoryPolicyError("hard purge requires the exact record id as confirmation")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id=? AND owner_id=?",
                (record_id, owner_id),
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError("memory record was not found in owner scope")
            connection.execute("DELETE FROM memory_records WHERE record_id=?", (record_id,))
            connection.execute(
                """DELETE FROM memory_conflicts
                   WHERE NOT EXISTS (
                       SELECT 1 FROM memory_conflict_members m
                       WHERE m.conflict_id=memory_conflicts.conflict_id
                   )"""
            )
            self._audit(
                connection, record_id=record_id, record_type=row["record_type"],
                scope_key=row["scope_key"], actor=actor, action="purge",
                reason_code="memory.explicit_hard_purge", before_status=row["status"],
                after_status=None,
            )
        maintenance = self._connect()
        try:
            maintenance.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            maintenance.execute("VACUUM")
        finally:
            maintenance.close()

    def _compact_storage(self) -> bool:
        connection = self._connect()
        try:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                return False
            connection.execute("VACUUM")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return bool(checkpoint is not None and int(checkpoint[0]) == 0)
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()

    def hard_purge_source(
        self,
        source_uri: str,
        *,
        confirm_source_uri: str,
        project_path: str,
        owner_id: str = "local-user",
        actor: str = "knowledge-purge",
    ) -> dict[str, object]:
        if confirm_source_uri != source_uri:
            raise MemoryPolicyError("source purge requires the exact source URI as confirmation")
        project = canonical_project_path(project_path)
        if project is None:
            raise MemoryPolicyError("source purge requires project scope")
        if not self._compact_storage():
            raise MemoryStoreError("memory purge blocked by active database reader")
        deleted_records = 0
        detached_sources = 0
        retained_records = 0
        with self._write() as connection:
            rows = connection.execute(
                """SELECT DISTINCT r.record_id,r.record_type,r.scope_key,r.status
                   FROM memory_records r JOIN memory_sources s ON s.record_id=r.record_id
                   WHERE r.owner_id=? AND r.project_realpath=? AND s.source_uri=?
                     AND s.source_type LIKE 'knowledge_candidate%'""",
                (owner_id, project, source_uri),
            ).fetchall()
            for row in rows:
                removed = connection.execute(
                    "DELETE FROM memory_sources WHERE record_id=? AND source_uri=? "
                    "AND source_type LIKE 'knowledge_candidate%'",
                    (row["record_id"], source_uri),
                ).rowcount
                detached_sources += max(0, int(removed))
                remaining = int(connection.execute(
                    "SELECT count(*) FROM memory_sources WHERE record_id=?",
                    (row["record_id"],),
                ).fetchone()[0])
                if remaining == 0:
                    connection.execute(
                        "DELETE FROM memory_records WHERE record_id=?",
                        (row["record_id"],),
                    )
                    deleted_records += 1
                    after_status = None
                else:
                    retained_records += 1
                    after_status = str(row["status"])
                self._audit(
                    connection,
                    record_id=str(row["record_id"]),
                    record_type=str(row["record_type"]),
                    scope_key=str(row["scope_key"]),
                    actor=actor,
                    action="purge_source",
                    reason_code="memory.knowledge_source_purged",
                    before_status=str(row["status"]),
                    after_status=after_status,
                )
            connection.execute(
                """DELETE FROM memory_conflicts
                   WHERE NOT EXISTS (
                       SELECT 1 FROM memory_conflict_members m
                       WHERE m.conflict_id=memory_conflicts.conflict_id
                   )"""
            )
        physical_complete = self._compact_storage()
        return {
            "deleted_records": deleted_records,
            "detached_sources": detached_sources,
            "retained_records": retained_records,
            "physical_purge_complete": physical_complete,
        }

    def sweep_retention(self, *, now: datetime | None = None, actor: str = "memory-retention") -> int:
        timestamp = _iso(now or _utc_now())
        with self._write() as connection:
            rows = connection.execute(
                """SELECT record_id,record_type,scope_key,status FROM memory_records
                   WHERE retention_class='ttl' AND expires_at<=?
                     AND status IN ('candidate','confirmed','conflicted')""",
                (timestamp,),
            ).fetchall()
            if rows:
                connection.execute(
                    """UPDATE memory_records SET status='stale',updated_at=?,revision=revision+1
                       WHERE retention_class='ttl' AND expires_at<=?
                         AND status IN ('candidate','confirmed','conflicted')""",
                    (timestamp, timestamp),
                )
                self._audit(
                    connection, record_id=None, record_type=None,
                    scope_key="retention-sweep", actor=actor, action="retention",
                    reason_code="memory.ttl_expired", before_status="active",
                    after_status=MemoryStatus.STALE.value, affected_count=len(rows),
                )
            return len(rows)

    def invalidate_project_commit(
        self,
        project_path: str,
        current_commit_sha: str,
        *,
        owner_id: str = "local-user",
        actor: str = "memory-invalidator",
    ) -> int:
        project = canonical_project_path(project_path)
        with self._write() as connection:
            rows = connection.execute(
                """SELECT record_id FROM memory_records
                   WHERE owner_id=? AND project_realpath=? AND source_commit_sha IS NOT NULL
                     AND lower(source_commit_sha)<>lower(?)
                     AND NOT EXISTS (
                         SELECT 1 FROM memory_sources s
                         WHERE s.record_id=memory_records.record_id
                           AND s.source_commit_sha IS NOT NULL
                           AND lower(s.source_commit_sha)=lower(?)
                     )
                     AND status IN ('candidate','confirmed','conflicted')""",
                (owner_id, project, current_commit_sha, current_commit_sha),
            ).fetchall()
            if rows:
                connection.execute(
                    """UPDATE memory_records SET status='stale',updated_at=?,revision=revision+1
                       WHERE owner_id=? AND project_realpath=? AND source_commit_sha IS NOT NULL
                         AND lower(source_commit_sha)<>lower(?)
                         AND NOT EXISTS (
                             SELECT 1 FROM memory_sources s
                             WHERE s.record_id=memory_records.record_id
                               AND s.source_commit_sha IS NOT NULL
                               AND lower(s.source_commit_sha)=lower(?)
                         )
                         AND status IN ('candidate','confirmed','conflicted')""",
                    (
                        _iso(_utc_now()), owner_id, project,
                        current_commit_sha, current_commit_sha,
                    ),
                )
                self._audit(
                    connection, record_id=None, record_type=MemoryRecordType.PROJECT_KNOWLEDGE.value,
                    scope_key=f"project:{project}", actor=actor, action="invalidate",
                    reason_code="memory.commit_changed", before_status="active",
                    after_status=MemoryStatus.STALE.value, affected_count=len(rows),
                )
            return len(rows)

    def invalidate_source(
        self,
        source_uri: str,
        *,
        current_hash: str | None = None,
        current_mtime_ns: int | None = None,
        project_path: str | None = None,
        owner_id: str = "local-user",
        actor: str = "memory-invalidator",
    ) -> int:
        project = canonical_project_path(project_path) if project_path is not None else None
        with self._write() as connection:
            rows = connection.execute(
                """SELECT DISTINCT r.record_id,r.scope_key FROM memory_records r
                   JOIN memory_sources s ON s.record_id=r.record_id
                   WHERE r.owner_id=? AND s.source_uri=?
                     AND (? IS NULL OR r.project_realpath=?)
                     AND r.status IN ('candidate','confirmed','conflicted')
                     AND ((? IS NOT NULL AND s.source_hash IS NOT NULL AND lower(s.source_hash)<>lower(?))
                       OR (? IS NOT NULL AND s.source_mtime_ns IS NOT NULL AND s.source_mtime_ns<>?))""",
                (
                    owner_id, source_uri, project, project,
                    current_hash, current_hash, current_mtime_ns, current_mtime_ns,
                ),
            ).fetchall()
            ids = [row["record_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE memory_records SET status='stale',updated_at=?,revision=revision+1 WHERE record_id IN ({placeholders})",
                    (_iso(_utc_now()), *ids),
                )
                self._audit(
                    connection, record_id=None, record_type=None, scope_key="source-invalidation",
                    actor=actor, action="invalidate", reason_code="memory.source_changed",
                    before_status="active", after_status=MemoryStatus.STALE.value,
                    affected_count=len(ids),
                )
            return len(ids)

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token
            for item in _WORD.findall(value)
            if (token := item.casefold()) not in _RETRIEVAL_STOPWORDS
        }

    @classmethod
    def _score(cls, query: str, subject: str, value_json: str, confidence: float) -> float:
        query_tokens = cls._tokens(query)
        subject_tokens = cls._tokens(subject)
        value_tokens = cls._tokens(value_json[:4_096])
        if not query_tokens:
            lexical = 0.0
        else:
            overlap = len(query_tokens & (subject_tokens | value_tokens))
            lexical = min(1.0, overlap / max(1, min(len(query_tokens), 8)))
        lowered = query.casefold()
        exact = 0.25 if subject.casefold() in lowered else 0.0
        if lexical == 0.0 and exact == 0.0:
            return 0.0
        return min(1.0, round(exact + lexical * 0.65 + confidence * 0.10, 6))

    def retrieve(
        self,
        *,
        owner_id: str = "local-user",
        project_path: str | None = None,
        task_id: str | None = None,
        query: str,
        max_records: int = 6,
        max_chars: int = 1_500,
        min_relevance: float = 0.08,
        current_commit_sha: str | None = None,
        allowed_types: Iterable[MemoryRecordType] | None = None,
    ) -> RetrievalResultV1:
        max_records = max(1, min(max_records, 32))
        max_chars = max(1, min(max_chars, 32_768))
        project = canonical_project_path(project_path)
        query = query[:4_096]
        query_terms = self._tokens(query)
        if not query_terms:
            return RetrievalResultV1(items=[], used_chars=0, max_chars=max_chars)
        now = _iso(_utc_now())
        scope_clauses = ["(scope_type='user' AND scope_key=?)"]
        scope_params: list[Any] = [f"user:{owner_id}"]
        if project:
            scope_clauses.append("(scope_type='project' AND project_realpath=?)")
            scope_params.append(project)
        if task_id:
            if project:
                scope_clauses.append(
                    "(scope_type='task' AND task_id=? AND project_realpath=?)"
                )
                scope_params.extend([task_id, project])
            else:
                scope_clauses.append(
                    "(scope_type='task' AND task_id=? AND project_realpath IS NULL)"
                )
                scope_params.append(task_id)
        types = [item.value for item in allowed_types] if allowed_types else sorted(_RETRIEVABLE_TYPES)
        sql = (
            "SELECT * FROM memory_records WHERE owner_id=? AND status='confirmed' "
            "AND (expires_at IS NULL OR expires_at>?) AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR valid_to>?) AND record_type IN ("
            + ",".join("?" for _ in types)
            + ") AND (" + " OR ".join(scope_clauses) + ") "
            "AND memory_query_match(memory_key,value_json)=1 "
            "ORDER BY observed_at DESC,record_id LIMIT 512"
        )
        params: list[Any] = [owner_id, now, now, now, *types, *scope_params]
        connection = self._connect(readonly=True)
        try:
            connection.create_function(
                "memory_query_match",
                2,
                lambda subject, value: int(
                    bool(
                        query_terms
                        & self._tokens(
                            str(subject or "") + " " + str(value or "")[:4_096]
                        )
                    )
                ),
                deterministic=True,
            )
            rows = connection.execute(sql, params).fetchall()
            ranked: list[tuple[float, sqlite3.Row, list[MemorySourceV1]]] = []
            for row in rows:
                sources = self._sources(connection, row["record_id"])
                if row["source_commit_sha"]:
                    if not current_commit_sha:
                        continue
                    matching_observation = any(
                        source.source_commit_sha
                        and source.source_commit_sha.casefold() == current_commit_sha.casefold()
                        for source in sources
                    )
                    if (
                        row["source_commit_sha"].casefold() != current_commit_sha.casefold()
                        and not matching_observation
                    ):
                        continue
                score = self._score(query, row["memory_key"], row["value_json"], row["confidence"])
                if score < min_relevance:
                    continue
                ranked.append((score, row, sources))
            ranked.sort(
                key=lambda item: (
                    -item[0],
                    -(
                        _parse_time(item[1]["observed_at"])
                        or datetime(1970, 1, 1, tzinfo=timezone.utc)
                    ).timestamp(),
                    item[1]["record_id"],
                )
            )
            items: list[RetrievalItemV1] = []
            used = 0
            for score, row, sources in ranked:
                value = json.loads(row["value_json"])
                source_refs = [
                    f"{source.source_type}:{source.uri or 'local'}"
                    + (f"#{source.fragment}" if source.fragment else "")
                    for source in sources[:32]
                ]
                overlap = sorted(self._tokens(query) & self._tokens(row["memory_key"] + " " + row["value_json"][:4_096]))
                reason = "confirmed scoped memory"
                if overlap:
                    reason += "; lexical match: " + ", ".join(overlap[:5])
                cost = len(
                    _canonical_json(
                        {
                            "record_id": row["record_id"],
                            "record_type": row["record_type"],
                            "subject": row["memory_key"],
                            "value": value,
                            "score": score,
                            "why": reason,
                            "source_refs": source_refs,
                        }
                    )
                )
                if cost > max_chars - used:
                    continue
                items.append(
                    RetrievalItemV1(
                        record_id=row["record_id"], record_type=row["record_type"],
                        subject=row["memory_key"], value=value, score=score, why=reason,
                        source_refs=source_refs, project_commit_sha=row["source_commit_sha"],
                    )
                )
                used += cost
                if len(items) >= max_records:
                    break
            return RetrievalResultV1(items=items, used_chars=used, max_chars=max_chars)
        finally:
            connection.close()

    def retrieve_safe(self, **kwargs: Any) -> RetrievalResultV1:
        max_chars = int(kwargs.get("max_chars", 1_500) or 1_500)
        try:
            return self.retrieve(**kwargs)
        except Exception as exc:
            return RetrievalResultV1(
                items=[], used_chars=0, max_chars=max(1, max_chars), degraded=True,
                diagnostic=f"memory unavailable: {type(exc).__name__}",
            )

    def export_records(
        self,
        *,
        owner_id: str = "local-user",
        scope: MemoryScope,
        project_path: str | None = None,
        task_id: str | None = None,
        format: str = "json",
        include_deleted: bool = False,
    ) -> str:
        if scope is MemoryScope.PROJECT and not project_path:
            raise MemoryPolicyError("project export requires explicit project_path")
        if scope is MemoryScope.TASK and not task_id:
            raise MemoryPolicyError("task export requires explicit task_id")
        records = self.list_records(
            owner_id=owner_id, scope=scope, project_path=project_path,
            task_id=task_id, include_deleted=include_deleted, limit=1_000,
        )
        payload: list[dict[str, Any]] = []
        for record in records:
            # Re-scan every user-controlled field at export time.  Service
            # generated record/scope identifiers are typed metadata and are
            # intentionally not passed through the free-text entropy detector.
            decision = inspect_memory_payload(
                {
                    "subject": record.subject,
                    "value": record.value,
                    "owner_id": record.owner_id,
                    "task_id": record.task_id,
                    "producer": record.producer,
                    "author": record.author,
                }
            )
            if not decision.allowed:
                reason = decision.reason_codes[0] if decision.reason_codes else "privacy.export_rejected"
                raise MemoryPrivacyError(reason, decision.reason_codes[1:])
            for source in record.sources:
                source_decision = inspect_memory_payload(
                    {
                        "source_type": source.source_type,
                        "fragment": source.fragment,
                        "producer": source.producer,
                        "author": source.author,
                    },
                    source_uri=source.uri,
                )
                if not source_decision.allowed:
                    reason = (
                        source_decision.reason_codes[0]
                        if source_decision.reason_codes
                        else "privacy.export_rejected"
                    )
                    raise MemoryPrivacyError(reason, source_decision.reason_codes[1:])
            payload.append(record.model_dump(mode="json"))
        if format == "json":
            return json.dumps(
                {"schema_version": MEMORY_RECORD_SCHEMA_VERSION, "records": payload},
                ensure_ascii=False, indent=2, sort_keys=True,
            )
        if format == "markdown":
            blocks = ["# Local Agent memory export", "", f"Records: {len(payload)}", ""]
            for record in payload:
                blocks.extend(
                    [
                        f"## {record['record_id']} — {record['subject']}", "",
                        f"- Type: `{record['record_type']}`", f"- Status: `{record['status']}`",
                        f"- Scope: `{record['scope']}`", "", "```json",
                        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).replace("```", "` ` `"),
                        "```", "",
                    ]
                )
            return "\n".join(blocks)
        raise ValueError("export format must be json or markdown")

    def audit_events(self, *, record_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        connection = self._connect(readonly=True)
        try:
            if record_id:
                rows = connection.execute(
                    "SELECT * FROM memory_audit_log WHERE record_id=? ORDER BY event_id DESC LIMIT ?",
                    (record_id, max(1, min(limit, 1_000))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memory_audit_log ORDER BY event_id DESC LIMIT ?",
                    (max(1, min(limit, 1_000)),),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
