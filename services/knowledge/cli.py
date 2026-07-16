from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.knowledge.contracts import (
    FreshnessRequirement,
    ImportRequestV1,
    RetrievalRequestV1,
    SourceKind,
    SourceRegistrationV1,
)
from services.knowledge.engine import KnowledgeEngine
from services.knowledge.migrations import KnowledgeMigrationError
from services.knowledge.privacy import KnowledgePolicyError
from services.knowledge.repository import RepositoryError
from services.knowledge.store import DEFAULT_DATABASE, KnowledgeStore, KnowledgeStoreError
from services.memory.store import (
    DEFAULT_DATABASE as DEFAULT_MEMORY_DATABASE,
    MemoryStore,
    MemoryStoreError,
)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _registration(args: argparse.Namespace) -> SourceRegistrationV1:
    return SourceRegistrationV1(
        owner_id=args.owner,
        project_path=args.project,
        consent=bool(args.approved),
        sensitivity_ceiling=args.sensitivity,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scoped local Knowledge Engine operator CLI")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--memory-database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")
    subparsers.add_parser("compact")

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--project", required=True)
    import_parser.add_argument("--source", required=True)
    import_parser.add_argument("--kind", choices=[item.value for item in SourceKind])
    import_parser.add_argument("--owner", default="local-user")
    import_parser.add_argument("--sensitivity", choices=["public", "internal", "sensitive"], default="internal")
    import_parser.add_argument("--approved", action="store_true")
    import_parser.add_argument("--dry-run", action="store_true")

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--project", required=True)
    index_parser.add_argument("--owner", default="local-user")
    index_parser.add_argument("--sensitivity", choices=["public", "internal", "sensitive"], default="internal")
    index_parser.add_argument("--approved", action="store_true")
    index_parser.add_argument("--dry-run", action="store_true")

    retrieve = subparsers.add_parser("retrieve")
    retrieve.add_argument("--project", required=True)
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--owner", default="local-user")
    retrieve.add_argument("--source-type", action="append", choices=[item.value for item in SourceKind], default=[])
    retrieve.add_argument("--token-budget", type=int, default=2000)
    retrieve.add_argument("--max-fragments", type=int, default=8)
    retrieve.add_argument("--include-stale", action="store_true")

    context = subparsers.add_parser("context")
    context.add_argument("--project", required=True)
    context.add_argument("--goal", required=True)
    context.add_argument("--owner", default="local-user")
    context.add_argument("--token-budget", type=int, default=2000)
    context.add_argument("--constraint", action="append", default=[])
    context.add_argument("--modified-file", action="append", default=[])
    context.add_argument("--error", action="append", default=[])
    context.add_argument("--verification", action="append", default=[])
    context.add_argument("--tool-result", action="append", default=[])

    repo_map = subparsers.add_parser("map")
    repo_map.add_argument("--project", required=True)
    repo_map.add_argument("--owner", default="local-user")

    rg_search = subparsers.add_parser("rg-search")
    rg_search.add_argument("--project", required=True)
    rg_search.add_argument("--query", required=True)
    rg_search.add_argument("--owner", default="local-user")
    rg_search.add_argument("--max-matches", type=int, default=100)

    candidates = subparsers.add_parser("candidates")
    candidates.add_argument("--project", required=True)
    candidates.add_argument("--owner", default="local-user")

    purge = subparsers.add_parser("purge-source")
    purge.add_argument("--source-id", required=True)
    purge.add_argument("--project", required=True)
    purge.add_argument("--owner", default="local-user")
    purge.add_argument("--confirm")

    propose = subparsers.add_parser("propose-memory")
    propose.add_argument("--fact-id", required=True)
    propose.add_argument("--project", required=True)
    propose.add_argument("--owner", default="local-user")
    propose.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = KnowledgeStore(Path(args.database))
        knowledge_database = Path(args.database).resolve()
        memory_database = (
            Path(args.memory_database).resolve()
            if args.memory_database
            else (
                Path(DEFAULT_MEMORY_DATABASE).resolve()
                if knowledge_database == Path(DEFAULT_DATABASE).resolve()
                else knowledge_database.with_name("memory.sqlite3")
            )
        )
        memory_store = MemoryStore(memory_database)
        engine = KnowledgeEngine(store, memory_store=memory_store)
        if args.command == "status":
            _print(store.status())
        elif args.command == "compact":
            result = engine.compact_storage()
            _print(result)
            return 0 if result["physical_purge_complete"] else 3
        elif args.command == "import":
            request = ImportRequestV1(
                registration=_registration(args),
                source_path=args.source,
                source_kind=SourceKind(args.kind) if args.kind else None,
                dry_run=args.dry_run,
            )
            _print(engine.import_source(request).model_dump(mode="json"))
        elif args.command == "index":
            _print(engine.index_repository(_registration(args), dry_run=args.dry_run))
        elif args.command == "retrieve":
            result = engine.retrieve(
                RetrievalRequestV1(
                    owner_id=args.owner,
                    project_path=args.project,
                    query=args.query,
                    allowed_source_types=(
                        [SourceKind(value) for value in args.source_type]
                        if args.source_type
                        else None
                    ),
                    token_budget=args.token_budget,
                    max_fragments=args.max_fragments,
                    freshness=(
                        FreshnessRequirement.INCLUDE_STALE
                        if args.include_stale
                        else FreshnessRequirement.ACTIVE_ONLY
                    ),
                )
            )
            _print(result.model_dump(mode="json"))
        elif args.command == "context":
            result = engine.build_context(
                project_path=args.project,
                goal=args.goal,
                token_budget=args.token_budget,
                constraints=args.constraint,
                modified_files=args.modified_file,
                unresolved_errors=args.error,
                verification_plan=args.verification,
                fresh_tool_results=args.tool_result,
                owner_id=args.owner,
            )
            _print(result.model_dump(mode="json"))
        elif args.command == "map":
            result = engine.repository_map(args.project, owner_id=args.owner)
            _print(result.model_dump(mode="json") if result else {"status": "not_indexed"})
        elif args.command == "rg-search":
            _print(
                engine.search_repository_text(
                    project_path=args.project,
                    query=args.query,
                    owner_id=args.owner,
                    max_matches=args.max_matches,
                )
            )
        elif args.command == "candidates":
            project = str(Path(args.project).resolve(strict=True))
            _print({"candidates": store.list_candidates(args.owner, project)})
        elif args.command == "purge-source":
            apply = args.confirm == args.source_id
            if args.confirm and not apply:
                raise KnowledgeStoreError("purge confirmation must exactly equal source_id")
            result = engine.purge_source(
                args.source_id,
                owner_id=args.owner,
                project_path=args.project,
                apply=apply,
            )
            _print(result)
            if apply and not result.get("complete"):
                return 3
        elif args.command == "propose-memory":
            record_id = engine.propose_memory_candidate(
                args.fact_id,
                project_path=args.project,
                owner_id=args.owner,
                confirmation=args.confirm,
            )
            _print(
                {
                    "memory_record_id": record_id,
                    "status": "candidate",
                    "next_confirmation_boundary": "python -m services.memory.cli confirm",
                }
            )
        else:
            raise KnowledgeStoreError("unsupported knowledge command")
        return 0
    except (
        KnowledgeMigrationError,
        KnowledgePolicyError,
        KnowledgeStoreError,
        MemoryStoreError,
        RepositoryError,
        ValueError,
        OSError,
    ) as exc:
        _print(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "reason_code": getattr(exc, "reason_code", "knowledge.operation_failed"),
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
