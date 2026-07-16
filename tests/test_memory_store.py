import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from services import common
from services.memory import (
    MemoryRecordType,
    MemoryRetention,
    MemoryScope,
    MemorySensitivity,
    MemorySourceV1,
    MemoryStatus,
    MemoryStore,
    MemoryUpsertV1,
)
from services.memory.privacy import MemoryPrivacyError
from services.memory.store import MemoryNotFoundError, MemoryPolicyError, MemoryRevisionError


def make_store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3", create_migration_backup=False)


def request(
    tmp_path,
    *,
    subject="project.python_version",
    value=None,
    source_uri="user://manual",
    source_hash=None,
    source_mtime_ns=None,
    status=MemoryStatus.CANDIDATE,
    scope=MemoryScope.PROJECT,
    record_type=MemoryRecordType.PROJECT_KNOWLEDGE,
    owner_id="local-user",
    task_id=None,
    retention=MemoryRetention.MANUAL,
    expires_at=None,
    project_commit_sha=None,
    sensitivity=MemorySensitivity.INTERNAL,
):
    return MemoryUpsertV1(
        record_type=record_type,
        scope=scope,
        subject=subject,
        value=value if value is not None else {"version": "3.12"},
        source=MemorySourceV1(
            source_type="user_assertion",
            uri=source_uri,
            source_hash=source_hash,
            source_mtime_ns=source_mtime_ns,
        ),
        owner_id=owner_id,
        project_path=str(tmp_path) if scope in {MemoryScope.PROJECT, MemoryScope.TASK} else None,
        task_id=task_id,
        status=status,
        retention=retention,
        expires_at=expires_at,
        project_commit_sha=project_commit_sha,
        sensitivity=sensitivity,
    )


def test_crud_revision_confirm_reject_and_soft_delete(tmp_path):
    store = make_store(tmp_path)
    created = store.upsert(request(tmp_path))
    assert created.status is MemoryStatus.CANDIDATE
    confirmed = store.confirm(created.record_id, expected_revision=created.revision)
    assert confirmed.status is MemoryStatus.CONFIRMED
    with pytest.raises(MemoryRevisionError):
        store.reject(created.record_id, expected_revision=created.revision)
    rejected = store.reject(created.record_id)
    assert rejected.status is MemoryStatus.REJECTED
    with pytest.raises(MemoryPolicyError):
        store.confirm(created.record_id)

    second = store.upsert(request(tmp_path, subject="project.formatter", value="ruff"))
    deleted = store.soft_delete(second.record_id)
    assert deleted.status is MemoryStatus.DELETED
    with pytest.raises(MemoryNotFoundError):
        store.get(second.record_id)
    with pytest.raises(MemoryPolicyError):
        store.upsert(request(tmp_path, subject="project.formatter", value="ruff"))


def test_same_fact_deduplicates_and_adds_distinct_provenance(tmp_path):
    store = make_store(tmp_path)
    first = store.upsert(request(tmp_path, source_uri="user://one"))
    same = store.upsert(request(tmp_path, source_uri="user://one"))
    observed_elsewhere = store.upsert(request(tmp_path, source_uri="user://two"))

    assert first.record_id == same.record_id == observed_elsewhere.record_id
    assert len(observed_elsewhere.sources) == 2
    assert len(store.list_records()) == 1


def test_normalized_identity_and_detected_sensitivity_are_persisted(tmp_path):
    store = make_store(tmp_path)
    full_width = store.upsert(
        request(tmp_path, subject="project.runtime_name", value="Ｐｙｔｈｏｎ")
    )
    normalized = store.upsert(
        request(
            tmp_path,
            subject="project.runtime_name",
            value="Python",
            source_uri="user://second-observation",
        )
    )
    assert full_width.record_id == normalized.record_id
    assert normalized.value == "Python"

    sensitive = store.upsert(
        request(
            tmp_path,
            subject="project.contact",
            value={"email": "developer@example.invalid"},
            sensitivity="public",
        )
    )
    assert sensitive.sensitivity.value == "sensitive"


def test_conflict_is_visible_and_requires_explicit_winner(tmp_path):
    store = make_store(tmp_path)
    first = store.upsert(request(tmp_path, value="pytest"))
    second = store.upsert(request(tmp_path, value="unittest", source_uri="user://two"))

    assert store.get(first.record_id).status is MemoryStatus.CONFLICTED
    assert second.status is MemoryStatus.CONFLICTED
    assert not store.retrieve(project_path=str(tmp_path), query="test framework").items

    winner = store.confirm(second.record_id)
    assert winner.status is MemoryStatus.CONFIRMED
    assert store.get(first.record_id).status is MemoryStatus.REJECTED


def test_supersede_is_atomic_versioned_edit(tmp_path):
    store = make_store(tmp_path)
    old = store.upsert(request(tmp_path, value="black"))
    replacement = store.supersede(
        old.record_id,
        value="ruff-format",
        source=MemorySourceV1(source_type="user_assertion", uri="user://edit"),
    )

    assert replacement.supersedes_record_id == old.record_id
    assert replacement.status is MemoryStatus.CANDIDATE
    assert store.get(old.record_id, include_deleted=True).status is MemoryStatus.SUPERSEDED


def test_scope_and_owner_isolation(tmp_path):
    store = make_store(tmp_path)
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    a = store.upsert(request(project_a, value="A"))
    b = store.upsert(request(project_b, value="B"))
    other = store.upsert(request(project_a, owner_id="another-user", value="private"))

    a_rows = store.list_records(project_path=str(project_a))
    assert [row.record_id for row in a_rows] == [a.record_id]
    assert b.record_id not in {row.record_id for row in a_rows}
    with pytest.raises(MemoryNotFoundError):
        store.get(other.record_id, owner_id="local-user")


def test_task_scope_requires_same_project_even_when_task_ids_collide(tmp_path):
    store = make_store(tmp_path)
    project_a = tmp_path / "task-project-a"
    project_b = tmp_path / "task-project-b"
    project_a.mkdir()
    project_b.mkdir()
    task_id = "same-task-id"
    a = store.upsert(
        request(
            project_a,
            scope=MemoryScope.TASK,
            record_type=MemoryRecordType.TASK_HISTORY,
            task_id=task_id,
            subject="task.python_version",
            value={
                "goal_summary": "inspect Python runtime A",
                "executor": "qwen_code",
                "route": "local_code",
                "attempts": 1,
            },
        )
    )
    b = store.upsert(
        request(
            project_b,
            scope=MemoryScope.TASK,
            record_type=MemoryRecordType.TASK_HISTORY,
            task_id=task_id,
            subject="task.python_version",
            value={
                "goal_summary": "inspect Python runtime B",
                "executor": "qwen_code",
                "route": "local_code",
                "attempts": 1,
            },
        )
    )
    store.confirm(a.record_id)
    store.confirm(b.record_id)

    result = store.retrieve(
        project_path=str(project_a), task_id=task_id, query="python version"
    )
    assert [item.record_id for item in result.items] == [a.record_id]
    listed = store.list_records(
        scope=MemoryScope.TASK, project_path=str(project_a), task_id=task_id
    )
    assert [item.record_id for item in listed] == [a.record_id]


def test_ttl_commit_hash_and_mtime_invalidation(tmp_path):
    store = make_store(tmp_path)
    expired = store.upsert(
        request(
            tmp_path,
            subject="task.short_fact",
            value="temporary",
            retention=MemoryRetention.TTL,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    store.confirm(expired.record_id)
    assert store.sweep_retention() == 1
    assert store.get(expired.record_id).status is MemoryStatus.STALE

    commit_bound = store.upsert(
        request(tmp_path, subject="project.commit_fact", value="old", project_commit_sha="a" * 40)
    )
    store.confirm(commit_bound.record_id)
    assert store.invalidate_project_commit(str(tmp_path), "b" * 40) == 1
    assert store.get(commit_bound.record_id).status is MemoryStatus.STALE

    source_bound = store.upsert(
        request(
            tmp_path,
            subject="project.source_fact",
            value="observed",
            source_uri="README.md",
            source_hash="c" * 64,
            source_mtime_ns=10,
        )
    )
    store.confirm(source_bound.record_id)
    assert store.invalidate_source(
        "project://README.md", current_hash="d" * 64, current_mtime_ns=11
    ) == 1
    assert store.get(source_bound.record_id).status is MemoryStatus.STALE


def test_commit_reobservation_and_owner_scoped_invalidation(tmp_path):
    store = make_store(tmp_path)
    first = store.upsert(
        request(
            tmp_path,
            subject="project.commit_observation",
            value="same fact",
            project_commit_sha="a" * 40,
        )
    )
    store.confirm(first.record_id)
    reobserved = store.upsert(
        request(
            tmp_path,
            subject="project.commit_observation",
            value="same fact",
            project_commit_sha="b" * 40,
        )
    )
    assert reobserved.record_id == first.record_id
    assert reobserved.project_commit_sha == "b" * 40
    assert len(reobserved.sources) == 2
    assert store.invalidate_project_commit(str(tmp_path), "b" * 40) == 0
    assert store.retrieve(
        project_path=str(tmp_path),
        query="commit observation",
        current_commit_sha="b" * 40,
    ).items

    other = store.upsert(
        request(
            tmp_path,
            owner_id="another-user",
            subject="project.owner_commit",
            value="other owner fact",
            project_commit_sha="a" * 40,
        )
    )
    store.confirm(other.record_id, owner_id="another-user")
    store.invalidate_project_commit(
        str(tmp_path), "b" * 40, owner_id="local-user"
    )
    assert store.get(other.record_id, owner_id="another-user").status is MemoryStatus.CONFIRMED


def test_commit_bound_memory_requires_current_revision_to_retrieve(tmp_path):
    store = make_store(tmp_path)
    record = store.upsert(
        request(
            tmp_path,
            subject="project.commit_only",
            value="revision fact",
            project_commit_sha="a" * 40,
        )
    )
    store.confirm(record.record_id)
    assert not store.retrieve(
        project_path=str(tmp_path), query="commit revision"
    ).items


def test_retrieval_is_confirmed_scoped_budgeted_and_explained(tmp_path):
    store = make_store(tmp_path)
    selected = store.upsert(
        request(tmp_path, subject="project.python_version", value={"version": "3.12"})
    )
    store.confirm(selected.record_id)
    candidate = store.upsert(request(tmp_path, subject="project.python_style", value="ruff"))
    archive = store.upsert(
        request(
            tmp_path,
            subject="archive.reference",
            value={"archive_id": "old-001", "kind": "chat"},
            record_type=MemoryRecordType.ARCHIVE_REFERENCE,
        )
    )
    store.confirm(archive.record_id)

    result = store.retrieve(
        project_path=str(tmp_path), query="python version", max_records=1, max_chars=500
    )
    assert [item.record_id for item in result.items] == [selected.record_id]
    assert result.used_chars <= result.max_chars == 500
    assert result.items[0].source_refs
    assert "lexical match" in result.items[0].why
    assert candidate.record_id not in {item.record_id for item in result.items}
    assert archive.record_id not in {item.record_id for item in result.items}


def test_operational_state_and_archive_reference_are_strict_metadata(tmp_path):
    store = make_store(tmp_path)
    operational = store.upsert(
        request(
            tmp_path,
            scope=MemoryScope.TASK,
            record_type=MemoryRecordType.OPERATIONAL_STATE,
            task_id="operation-1",
            subject="operation.current_stage",
            value={
                "active_goal": "verify bounded memory",
                "stage": "testing",
                "unresolved_errors": [],
                "next_action": "run the focused suite",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "lease_owner": "worker-1",
                "lease_expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            },
        )
    )
    assert operational.value["stage"] == "testing"
    assert operational.value["lease_owner"] == "worker-1"

    reference = store.upsert(
        request(
            tmp_path,
            scope=MemoryScope.PROJECT,
            record_type=MemoryRecordType.ARCHIVE_REFERENCE,
            subject="archive.typed_metadata",
            value={
                "archive_id": "archive-typed-1",
                "kind": "chat",
                "uri": "archive://fixture/item",
                "metadata": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "message_count": 12,
                    "language": "ru-RU",
                },
            },
        )
    )
    assert reference.value["metadata"]["message_count"] == 12
    assert reference.value["metadata"]["language"] == "ru-RU"

    with pytest.raises(ValidationError):
        store.upsert(
            request(
                tmp_path,
                scope=MemoryScope.TASK,
                record_type=MemoryRecordType.OPERATIONAL_STATE,
                task_id="operation-2",
                subject="operation.invalid_lease",
                value={
                    "active_goal": "resume safely",
                    "stage": "testing",
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "lease_owner": "worker-without-expiry",
                },
            )
        )

    with pytest.raises(MemoryPolicyError):
        store.upsert(
            request(
                tmp_path,
                scope=MemoryScope.PROJECT,
                record_type=MemoryRecordType.ARCHIVE_REFERENCE,
                subject="archive.nested_payload",
                value={
                    "archive_id": "archive-1",
                    "kind": "chat",
                    "metadata": {"nested": {"messages": ["raw archive body"]}},
                },
            )
        )

    for value in (
        {
            "archive_id": "archive-2",
            "kind": "chat",
            "metadata": {"conversation": "raw archive body"},
        },
        {
            "archive_id": "archive-3",
            "kind": "chat",
            "uri": "data:text/plain,raw-archive-body",
        },
        {
            "archive_id": "archive-4",
            "kind": "chat",
            "metadata": {"labels": ["raw", "archive"]},
        },
        {
            "archive_id": "archive-5",
            "kind": "chat",
            "metadata": {"conversation_text": "raw archive body"},
        },
        {
            "archive_id": "archive-6",
            "kind": "tool",
            "metadata": {"tool-output": "raw tool output"},
        },
        {
            "archive_id": "archive-7",
            "kind": "voice",
            "metadata": {"full_transcript": "raw transcript"},
        },
    ):
        with pytest.raises((MemoryPolicyError, ValidationError)):
            store.upsert(
                request(
                    tmp_path,
                    scope=MemoryScope.PROJECT,
                    record_type=MemoryRecordType.ARCHIVE_REFERENCE,
                    subject="archive.raw_content_rejected",
                    value=value,
                )
            )


def test_nfkc_expansion_is_revalidated_before_any_insert(tmp_path):
    store = make_store(tmp_path)
    expanding_source = "user://" + "\ufdfa" * 200

    with pytest.raises((MemoryPrivacyError, ValidationError)):
        store.upsert(
            request(
                tmp_path,
                subject="project.normalization_guard",
                value="safe",
                source_uri=expanding_source,
            )
        )

    connection = store._connect(readonly=True)
    try:
        assert connection.execute("SELECT count(*) FROM memory_records").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_retrieval_prefilters_before_recency_cap_and_ignores_generic_scope_tokens(tmp_path):
    store = make_store(tmp_path)
    oldest_relevant = store.upsert(
        request(tmp_path, subject="project.python_version", value={"version": "3.12"})
    )
    store.confirm(oldest_relevant.record_id)

    for index in range(80):
        unrelated = store.upsert(
            request(
                tmp_path,
                subject=f"project.unrelated_color_{index}",
                value=f"shade-{index}",
                source_uri=f"user://unrelated/{index}",
            )
        )
        store.confirm(unrelated.record_id)

    result = store.retrieve(
        project_path=str(tmp_path), query="check the Python version", max_chars=1_000
    )
    assert [item.record_id for item in result.items] == [oldest_relevant.record_id]

    generic = store.retrieve(
        project_path=str(tmp_path), query="fix database migration in this project"
    )
    assert not generic.items

    tail_term = store.upsert(
        request(tmp_path, subject="project.zzzneedle", value="tail match")
    )
    store.confirm(tail_term.record_id)
    long_goal = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
        "lima mike november oscar papa quebec romeo sierra tango zzzneedle"
    )
    long_result = store.retrieve(project_path=str(tmp_path), query=long_goal)
    assert tail_term.record_id in {item.record_id for item in long_result.items}


def test_retrieval_with_commit_filter_never_waits_for_a_writer_lock(tmp_path):
    store = make_store(tmp_path)
    record = store.upsert(
        request(
            tmp_path,
            subject="project.lock_free_read",
            value="retrieval marker",
            project_commit_sha="a" * 40,
        )
    )
    store.confirm(record.record_id)
    writer = store._connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        result = store.retrieve_safe(
            project_path=str(tmp_path),
            query="lock free retrieval marker",
            current_commit_sha="a" * 40,
        )
        elapsed = time.monotonic() - started
    finally:
        writer.rollback()
        writer.close()

    assert not result.degraded
    assert record.record_id in {item.record_id for item in result.items}
    assert elapsed < 1.0


def test_privacy_rejection_never_reaches_database_audit_or_error(tmp_path):
    store = make_store(tmp_path)
    synthetic = "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0" * 2
    with pytest.raises(MemoryPrivacyError) as captured:
        store.upsert(request(tmp_path, value={"note": synthetic}))
    assert synthetic not in str(captured.value)
    assert not store.list_records()
    serialized_audit = json.dumps(store.audit_events())
    assert synthetic not in serialized_audit
    with pytest.raises(MemoryPrivacyError):
        store.upsert(
            request(
                tmp_path,
                subject="project.private_source",
                value="safe metadata",
                source_uri=f"https://example.invalid/docs?token={synthetic}",
            )
        )
    store.database_path.with_suffix(store.database_path.suffix + "-wal").touch(exist_ok=True)
    for path in (store.database_path, store.database_path.with_name(store.database_path.name + "-wal")):
        assert synthetic.encode() not in path.read_bytes()


def test_audit_actor_is_allowlisted_metadata_not_caller_payload(tmp_path):
    store = make_store(tmp_path)
    actor_payload = "sk-proj-" + "Q1w2E3r4T5y6U7i8O9p0" * 2
    created = store.upsert(
        request(tmp_path, subject="project.audit_actor", value="safe").model_copy(
            update={"actor": actor_payload}
        )
    )
    events = store.audit_events(record_id=created.record_id)
    assert events[0]["actor"] == "external-actor"
    assert actor_payload not in json.dumps(events)


def test_export_is_scope_bound_and_excludes_legacy_task_payload(tmp_path):
    store = make_store(tmp_path)
    record = store.upsert(request(tmp_path, value={"version": "3.12"}))
    store.confirm(record.record_id)
    with store._write() as connection:
        connection.execute(
            "INSERT INTO tasks(id,created_at,updated_at,route,status,project_path,prompt,result,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy", 1.0, 1.0, "fast_chat", "complete", None, "legacy private prompt", "legacy result", "{}"),
        )

    exported = store.export_records(scope=MemoryScope.PROJECT, project_path=str(tmp_path))
    assert record.record_id in exported
    assert "legacy private prompt" not in exported
    assert "legacy result" not in exported


def test_hard_purge_removes_payload_and_keeps_payload_free_audit(tmp_path):
    store = make_store(tmp_path)
    record = store.upsert(request(tmp_path, value="purge-me"))
    with pytest.raises(MemoryPolicyError):
        store.hard_purge(record.record_id, confirm_record_id="wrong")
    store.hard_purge(record.record_id, confirm_record_id=record.record_id)

    with pytest.raises(MemoryNotFoundError):
        store.get(record.record_id, include_deleted=True)
    audit = store.audit_events(record_id=record.record_id)
    assert audit[0]["action"] == "purge"
    assert "purge-me" not in json.dumps(audit)


def test_hard_purge_removes_empty_conflict_metadata(tmp_path):
    store = make_store(tmp_path)
    first = store.upsert(request(tmp_path, subject="project.conflict", value="one"))
    second = store.upsert(
        request(
            tmp_path,
            subject="project.conflict",
            value="two",
            source_uri="user://conflict-two",
        )
    )
    store.hard_purge(first.record_id, confirm_record_id=first.record_id)
    store.hard_purge(second.record_id, confirm_record_id=second.record_id)
    connection = store._connect(readonly=True)
    try:
        assert connection.execute("SELECT count(*) FROM memory_conflicts").fetchone()[0] == 0
    finally:
        connection.close()


def test_concurrent_same_fact_upserts_are_idempotent(tmp_path):
    store = make_store(tmp_path)

    def write(index):
        return store.upsert(request(tmp_path, source_uri=f"user://source/{index % 3}")).record_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))

    assert len(set(ids)) == 1
    record = store.get(ids[0])
    assert len(record.sources) == 3
    assert len(store.list_records()) == 1


def test_concurrent_task_and_memory_writes_share_wal_without_corruption(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    database = tmp_path / "memory.sqlite3"
    common._INITIALIZED_DATABASES.discard(database.resolve())
    store = MemoryStore(database, create_migration_backup=False)

    def write(item):
        kind, index = item
        if kind == "task":
            common.save_task(
                f"concurrent-task-{index}",
                "fast_chat",
                "complete",
                f"bounded task {index}",
            )
            return
        store.upsert(
            request(
                tmp_path,
                subject=f"project.concurrent_fact_{index}",
                value={"index": index},
                source_uri=f"user://concurrent/{index}",
            )
        )

    work = [(kind, index) for index in range(20) for kind in ("task", "memory")]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, work))

    connection = store._connect(readonly=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute(
            "SELECT count(*) FROM tasks WHERE id LIKE 'concurrent-task-%'"
        ).fetchone()[0] == 20
        assert connection.execute(
            "SELECT count(*) FROM memory_records WHERE memory_key LIKE 'project.concurrent_fact_%'"
        ).fetchone()[0] == 20
    finally:
        connection.close()


def test_retrieve_safe_degrades_without_exposing_query(tmp_path):
    store = make_store(tmp_path)
    store.database_path.unlink()
    private_query = "private-query-that-must-not-appear"
    result = store.retrieve_safe(project_path=str(tmp_path), query=private_query)
    assert result.degraded is True
    assert not result.items
    assert private_query not in (result.diagnostic or "")
