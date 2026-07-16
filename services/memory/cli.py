from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from services.memory.contracts import (
    MemoryRecordType,
    MemoryRetention,
    MemoryScope,
    MemorySourceV1,
    MemoryStatus,
    MemoryUpsertV1,
)
from services.memory.migrations import (
    CURRENT_SCHEMA_VERSION,
    backup_database,
    restrict_private_file,
    restore_database,
    verify_backup,
)
from services.memory.store import DEFAULT_DATABASE, MemoryStore, MemoryStoreError


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _value(value: str) -> Any:
    return json.loads(value)


def _value_options(parser: argparse.ArgumentParser) -> None:
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--value-json",
        help="inline JSON (may remain in shell history; prefer stdin for sensitive values)",
    )
    inputs.add_argument("--value-file", help="read UTF-8 JSON from this file")
    inputs.add_argument("--value-stdin", action="store_true", help="read JSON from stdin")


def _read_value(args: argparse.Namespace) -> Any:
    if args.value_json is not None:
        encoded = args.value_json
    elif args.value_file is not None:
        path = Path(args.value_file)
        with path.open("rb") as stream:
            payload = stream.read(262_145)
        if len(payload) > 262_144:
            raise MemoryStoreError("value input exceeds the CLI safety limit")
        encoded = payload.decode("utf-8")
    else:
        encoded = sys.stdin.read(262_145)
        if len(encoded.encode("utf-8")) > 262_144:
            raise MemoryStoreError("value input exceeds the CLI safety limit")
    return _value(encoded)


def _scope_options(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--scope", choices=[item.value for item in MemoryScope], required=required)
    parser.add_argument("--project")
    parser.add_argument("--task-id")
    parser.add_argument("--owner", default="local-user")


def _source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-type", default="user_assertion")
    parser.add_argument("--source-uri", default="user://manual")
    parser.add_argument("--source-fragment")
    parser.add_argument("--source-hash")
    parser.add_argument("--source-commit")
    parser.add_argument("--source-mtime-ns", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-agent-memory")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    add = sub.add_parser("add")
    _scope_options(add)
    _source_options(add)
    add.add_argument("--type", choices=[item.value for item in MemoryRecordType], required=True)
    add.add_argument("--subject", required=True)
    _value_options(add)
    add.add_argument("--confidence", type=float, default=0.5)
    add.add_argument("--retention", choices=[item.value for item in MemoryRetention], default="manual")
    add.add_argument("--ttl-seconds", type=int)
    add.add_argument("--project-commit")

    listing = sub.add_parser("list")
    _scope_options(listing)
    listing.add_argument("--type", choices=[item.value for item in MemoryRecordType])
    listing.add_argument("--status", action="append", choices=[item.value for item in MemoryStatus])
    listing.add_argument("--include-deleted", action="store_true")
    listing.add_argument("--limit", type=int, default=100)

    search = sub.add_parser("search")
    _scope_options(search)
    search.add_argument("query")
    search.add_argument("--type", choices=[item.value for item in MemoryRecordType])
    search.add_argument("--status", action="append", choices=[item.value for item in MemoryStatus])
    search.add_argument("--updated-after")
    search.add_argument("--updated-before")
    search.add_argument("--limit", type=int, default=100)

    show = sub.add_parser("show")
    show.add_argument("record_id")
    show.add_argument("--owner", default="local-user")

    for name in ("confirm", "reject", "delete"):
        action = sub.add_parser(name)
        action.add_argument("record_id")
        action.add_argument("--owner", default="local-user")
        action.add_argument("--actor", default="local-user")

    for name in ("edit", "supersede"):
        edit = sub.add_parser(name)
        edit.add_argument("record_id")
        _value_options(edit)
        edit.add_argument("--owner", default="local-user")
        edit.add_argument("--actor", default="local-user")
        _source_options(edit)

    purge = sub.add_parser("purge")
    purge.add_argument("record_id")
    purge.add_argument("--confirm-record-id", required=True)
    purge.add_argument("--owner", default="local-user")
    purge.add_argument("--actor", default="local-user")

    export = sub.add_parser("export")
    _scope_options(export)
    export.add_argument("--format", choices=["json", "markdown"], default="json")
    export.add_argument("--include-deleted", action="store_true")
    export.add_argument("--output")

    retrieve = sub.add_parser("retrieve")
    retrieve.add_argument("query")
    retrieve.add_argument("--project")
    retrieve.add_argument("--task-id")
    retrieve.add_argument("--owner", default="local-user")
    retrieve.add_argument("--current-commit")
    retrieve.add_argument("--max-records", type=int, default=6)
    retrieve.add_argument("--max-chars", type=int, default=1500)

    retention = sub.add_parser("retention")
    retention.add_argument("--apply", action="store_true")

    backup = sub.add_parser("backup")
    backup.add_argument("--output")
    verify = sub.add_parser("verify-backup")
    verify.add_argument("path")
    restore = sub.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--confirm", required=True)

    legacy = sub.add_parser("legacy-purge")
    legacy.add_argument("--task-id")
    legacy.add_argument("--project")
    legacy.add_argument("--before", type=float)
    legacy.add_argument("--apply", action="store_true")
    legacy.add_argument("--confirm", default="")
    return parser


def _scope(args: argparse.Namespace) -> MemoryScope:
    scope = MemoryScope(args.scope)
    if scope is MemoryScope.PROJECT and not args.project:
        raise MemoryStoreError("project scope requires --project")
    if scope is MemoryScope.TASK and not args.task_id:
        raise MemoryStoreError("task scope requires --task-id")
    return scope


def _source(args: argparse.Namespace) -> MemorySourceV1:
    return MemorySourceV1(
        source_type=args.source_type,
        uri=args.source_uri,
        fragment=args.source_fragment,
        source_hash=args.source_hash,
        source_commit_sha=args.source_commit,
        source_mtime_ns=args.source_mtime_ns,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported JSON metadata type: {type(value).__name__}")


def _print_json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value]
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    )


def _legacy_purge(store: MemoryStore, args: argparse.Namespace) -> dict[str, Any]:
    if not any((args.task_id, args.project, args.before is not None)):
        raise MemoryStoreError("legacy purge requires task, project, or time scope")
    clauses = ["legacy_payload=1"]
    params: list[Any] = []
    if args.task_id:
        clauses.append("id=?")
        params.append(args.task_id)
    if args.project:
        clauses.append("project_path=?")
        params.append(str(Path(args.project).resolve(strict=False)))
    if args.before is not None:
        clauses.append("created_at<?")
        params.append(args.before)
    where = " AND ".join(clauses)
    with store._write() as connection:
        count = int(connection.execute(f"SELECT count(*) FROM tasks WHERE {where}", params).fetchone()[0])
        if not args.apply:
            return {"matched": count, "deleted": 0, "preview": True}
        if args.confirm != "PURGE-LEGACY-TASKS":
            raise MemoryStoreError("legacy purge apply requires --confirm PURGE-LEGACY-TASKS")
        connection.execute(f"DELETE FROM tasks WHERE {where}", params)
        scope_description = "task" if args.task_id else "project" if args.project else "time"
        store._audit(
            connection,
            record_id=None,
            record_type="legacy_task",
            scope_key="legacy:" + hashlib.sha256(scope_description.encode()).hexdigest(),
            actor="local-user",
            action="legacy_purge",
            reason_code="legacy.explicit_scoped_purge",
            before_status="quarantined",
            after_status=None,
            affected_count=count,
        )
    maintenance = store._connect()
    try:
        maintenance.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        maintenance.execute("VACUUM")
    finally:
        maintenance.close()
    return {"matched": count, "deleted": count, "preview": False}


def execute(args: argparse.Namespace) -> int:
    database = Path(args.database)
    command = args.command
    if command == "verify-backup":
        _print_json(verify_backup(args.path).__dict__)
        return 0
    if command == "restore":
        if args.confirm != "RESTORE":
            raise MemoryStoreError("restore requires --confirm RESTORE")
        _print_json(restore_database(args.path, database, confirm=True).__dict__)
        return 0

    store = MemoryStore(database)
    if command == "status":
        connection = store._connect(readonly=True)
        try:
            task_rows = int(connection.execute("SELECT count(*) FROM tasks").fetchone()[0])
            legacy_rows = int(
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE legacy_payload=1"
                ).fetchone()[0]
            )
            payload = {
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
                "memory_records": int(connection.execute("SELECT count(*) FROM memory_records").fetchone()[0]),
                "task_rows": task_rows,
                "legacy_task_rows": legacy_rows,
                "filtered_task_rows": task_rows - legacy_rows,
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            }
        finally:
            connection.close()
        _print_json(payload)
    elif command == "add":
        scope = _scope(args)
        expires = None
        if args.ttl_seconds is not None:
            if args.retention != MemoryRetention.TTL.value or args.ttl_seconds <= 0:
                raise MemoryStoreError("positive --ttl-seconds requires --retention ttl")
            expires = datetime.now(timezone.utc) + timedelta(seconds=args.ttl_seconds)
        record = store.upsert(
            MemoryUpsertV1(
                record_type=args.type,
                scope=scope,
                subject=args.subject,
                value=_read_value(args),
                source=_source(args),
                owner_id=args.owner,
                project_path=args.project,
                task_id=args.task_id,
                confidence=args.confidence,
                retention=args.retention,
                expires_at=expires,
                project_commit_sha=args.project_commit,
            )
        )
        _print_json(record)
    elif command == "list":
        _print_json(
            store.list_records(
                owner_id=args.owner,
                scope=_scope(args),
                project_path=args.project,
                task_id=args.task_id,
                record_type=MemoryRecordType(args.type) if args.type else None,
                statuses=[MemoryStatus(item) for item in args.status] if args.status else None,
                include_deleted=args.include_deleted,
                limit=args.limit,
            )
        )
    elif command == "search":
        _print_json(
            store.search_records(
                args.query,
                owner_id=args.owner,
                scope=_scope(args),
                project_path=args.project,
                task_id=args.task_id,
                record_type=MemoryRecordType(args.type) if args.type else None,
                statuses=[MemoryStatus(item) for item in args.status] if args.status else None,
                updated_after=_time(args.updated_after),
                updated_before=_time(args.updated_before),
                limit=args.limit,
            )
        )
    elif command == "show":
        _print_json(store.get(args.record_id, owner_id=args.owner, include_deleted=True))
    elif command == "confirm":
        _print_json(store.confirm(args.record_id, owner_id=args.owner, actor=args.actor))
    elif command == "reject":
        _print_json(store.reject(args.record_id, owner_id=args.owner, actor=args.actor))
    elif command == "delete":
        _print_json(store.soft_delete(args.record_id, owner_id=args.owner, actor=args.actor))
    elif command in {"edit", "supersede"}:
        _print_json(
            store.supersede(
                args.record_id,
                value=_read_value(args),
                source=_source(args),
                owner_id=args.owner,
                actor=args.actor,
            )
        )
    elif command == "purge":
        store.hard_purge(
            args.record_id,
            confirm_record_id=args.confirm_record_id,
            owner_id=args.owner,
            actor=args.actor,
        )
        _print_json({"purged": args.record_id})
    elif command == "export":
        output = store.export_records(
            owner_id=args.owner,
            scope=_scope(args),
            project_path=args.project,
            task_id=args.task_id,
            format=args.format,
            include_deleted=args.include_deleted,
        )
        if args.output:
            destination = Path(args.output).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                with destination.open("x", encoding="utf-8", newline="\n") as stream:
                    created = True
                    restrict_private_file(destination)
                    stream.write(output)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                if created:
                    destination.unlink(missing_ok=True)
                raise
            _print_json({"exported": str(destination), "records_scope": args.scope})
        else:
            print(output)
    elif command == "retrieve":
        _print_json(
            store.retrieve_safe(
                owner_id=args.owner,
                project_path=args.project,
                task_id=args.task_id,
                query=args.query,
                max_records=args.max_records,
                max_chars=args.max_chars,
                current_commit_sha=args.current_commit,
            )
        )
    elif command == "retention":
        if args.apply:
            _print_json({"stale_marked": store.sweep_retention(), "applied": True})
        else:
            connection = store._connect(readonly=True)
            try:
                count = int(
                    connection.execute(
                        "SELECT count(*) FROM memory_records WHERE retention_class='ttl' AND expires_at<=? AND status IN ('candidate','confirmed','conflicted')",
                        (datetime.now(timezone.utc).isoformat(),),
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            _print_json({"would_mark_stale": count, "applied": False})
    elif command == "backup":
        result = backup_database(database, args.output) if args.output else backup_database(database)
        _print_json({"backup_path": str(result.backup_path), "schema_version": result.user_version, "size_bytes": result.size_bytes})
    elif command == "legacy-purge":
        _print_json(_legacy_purge(store, args))
    else:
        raise MemoryStoreError("unsupported memory command")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return execute(build_parser().parse_args(argv))
    except Exception as exc:
        reason = getattr(exc, "reason_code", None)
        suffix = f" ({reason})" if reason else ""
        print(f"ERROR: {type(exc).__name__}{suffix}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
