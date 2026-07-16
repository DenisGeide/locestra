from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.memory.migrations import restrict_database_storage


APPLICATION_ID: Final = 0x4C41494B  # LAIK: Local Agent Index/Knowledge
CURRENT_SCHEMA_VERSION: Final = 1
DEFAULT_BUSY_TIMEOUT_MS: Final = 2_000


class KnowledgeMigrationError(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_projects (
    project_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    active_generation_id TEXT,
    mutation_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, canonical_path)
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES knowledge_projects(project_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_origin TEXT NOT NULL CHECK(source_origin IN ('manual','repository')),
    sensitivity TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    status TEXT NOT NULL,
    current_hash TEXT,
    current_size_bytes INTEGER,
    current_mtime_ns INTEGER,
    parser TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    project_commit_sha TEXT,
    renamed_from_source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(project_id, source_uri, source_kind, source_origin)
);

CREATE TABLE IF NOT EXISTS knowledge_source_versions (
    version_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(source_id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    source_origin TEXT NOT NULL CHECK(source_origin IN ('manual','repository')),
    sensitivity TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    parser TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    derivation_version TEXT NOT NULL,
    project_commit_sha TEXT,
    renamed_from_source_id TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(source_id, source_hash, derivation_version)
);

CREATE TABLE IF NOT EXISTS knowledge_generations (
    generation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES knowledge_projects(project_id) ON DELETE CASCADE,
    base_generation_id TEXT,
    base_mutation_epoch INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('building','active','superseded','failed')),
    git_commit_sha TEXT,
    worktree_revision TEXT,
    policy_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_generation_sources (
    generation_id TEXT NOT NULL REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(source_id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES knowledge_source_versions(version_id) ON DELETE CASCADE,
    source_origin TEXT NOT NULL CHECK(source_origin IN ('manual','repository')),
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public','internal','sensitive')),
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    project_commit_sha TEXT,
    worktree_revision TEXT,
    derivation_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    renamed_from_source_id TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(generation_id, source_id)
);

CREATE TABLE IF NOT EXISTS knowledge_fragments (
    fragment_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES knowledge_source_versions(version_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    title TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    conflict_flag INTEGER NOT NULL DEFAULT 0 CHECK(conflict_flag IN (0,1)),
    untrusted INTEGER NOT NULL DEFAULT 1 CHECK(untrusted IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(version_id, ordinal)
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fragments_fts USING fts5(
    fragment_id UNINDEXED,
    title,
    content,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS knowledge_facts (
    fact_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES knowledge_source_versions(version_id) ON DELETE CASCADE,
    fragment_id TEXT NOT NULL REFERENCES knowledge_fragments(fragment_id) ON DELETE CASCADE,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, fragment_id, fact_kind, fact_key, value_hash)
);

CREATE TABLE IF NOT EXISTS knowledge_conflicts (
    conflict_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES knowledge_projects(project_id) ON DELETE CASCADE,
    fact_key TEXT NOT NULL,
    fact_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(generation_id, fact_key)
);

CREATE TABLE IF NOT EXISTS repository_maps (
    generation_id TEXT PRIMARY KEY REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES knowledge_projects(project_id) ON DELETE CASCADE,
    map_version TEXT NOT NULL,
    status TEXT NOT NULL,
    map_json TEXT NOT NULL,
    map_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_audit_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    project_id TEXT,
    source_id TEXT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_sources_project_status
    ON knowledge_sources(project_id, status, source_kind);
CREATE INDEX IF NOT EXISTS idx_versions_source_hash
    ON knowledge_source_versions(source_id, source_hash);
CREATE INDEX IF NOT EXISTS idx_generation_sources_version
    ON knowledge_generation_sources(generation_id, version_id);
CREATE INDEX IF NOT EXISTS idx_fragments_version
    ON knowledge_fragments(version_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_facts_key
    ON knowledge_facts(fact_key, status);
INSERT INTO knowledge_fragments_fts(knowledge_fragments_fts, rank)
    VALUES('secure-delete', 1);
"""


@dataclass(frozen=True, slots=True)
class KnowledgeVerification:
    database_path: Path
    application_id: int
    schema_version: int
    integrity_check: str
    foreign_key_violations: int
    fts5_available: bool


def open_knowledge_database(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    database = Path(path).resolve()
    if read_only:
        uri = f"file:{database.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
    else:
        connection = sqlite3.connect(database, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    if not read_only:
        connection.execute("PRAGMA secure_delete = ON")
    return connection


def migrate_knowledge_database(
    path: str | Path,
    *,
    harden_permissions: bool = True,
) -> Path:
    database = Path(path).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_knowledge_database(database)) as connection:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id not in {0, APPLICATION_ID}:
            raise KnowledgeMigrationError("unexpected SQLite application_id")
        if version not in {0, CURRENT_SCHEMA_VERSION}:
            raise KnowledgeMigrationError("unsupported knowledge schema version")
        checksum = hashlib.sha256(_SCHEMA.encode("utf-8")).hexdigest()
        if application_id == 0 and version == 0:
            existing_objects = connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger') "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing_objects:
                raise KnowledgeMigrationError("refusing to adopt a nonempty unrelated SQLite database")
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                connection.execute(
                    "INSERT INTO knowledge_migrations(version,name,checksum,applied_at) "
                    "VALUES(1,'initial knowledge schema',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (checksum,),
                )
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        else:
            existing = connection.execute(
                "SELECT checksum FROM knowledge_migrations WHERE version = 1"
            ).fetchone()
            if existing is None or existing["checksum"] != checksum:
                raise KnowledgeMigrationError("knowledge migration checksum mismatch")
    if harden_permissions:
        restrict_database_storage(database)
    return database


def verify_knowledge_database(path: str | Path) -> KnowledgeVerification:
    database = Path(path).resolve()
    with closing(open_knowledge_database(database, read_only=True)) as connection:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        fk = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        try:
            fragment_count = int(connection.execute("SELECT count(*) FROM knowledge_fragments").fetchone()[0])
            fts_count = int(connection.execute("SELECT count(*) FROM knowledge_fragments_fts").fetchone()[0])
            missing = int(connection.execute(
                "SELECT count(*) FROM knowledge_fragments f LEFT JOIN knowledge_fragments_fts x "
                "ON x.fragment_id=f.fragment_id WHERE x.fragment_id IS NULL"
            ).fetchone()[0])
            orphaned = int(connection.execute(
                "SELECT count(*) FROM knowledge_fragments_fts x LEFT JOIN knowledge_fragments f "
                "ON f.fragment_id=x.fragment_id WHERE f.fragment_id IS NULL"
            ).fetchone()[0])
            fts = fragment_count == fts_count and missing == 0 and orphaned == 0
        except sqlite3.DatabaseError:
            fts = False
    if application_id != APPLICATION_ID or version != CURRENT_SCHEMA_VERSION:
        raise KnowledgeMigrationError("knowledge database identity mismatch")
    if integrity != "ok" or fk or not fts:
        raise KnowledgeMigrationError("knowledge database verification failed")
    return KnowledgeVerification(database, application_id, version, integrity, fk, fts)
