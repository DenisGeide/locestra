from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.knowledge.contracts import ImportRequestV1, SourceRegistrationV1
from services.knowledge.engine import KnowledgeEngine
from services.knowledge.store import KnowledgeStore


def _engine(tmp_path: Path) -> KnowledgeEngine:
    return KnowledgeEngine(KnowledgeStore(tmp_path / "knowledge.sqlite3", harden_permissions=False))


def _request(project: Path, source: str) -> ImportRequestV1:
    return ImportRequestV1(
        registration=SourceRegistrationV1(project_path=str(project), consent=True),
        source_path=source,
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.example",
        "docs/private.key",
        "docs/cookies.json",
        "data/history.txt",
        "logs/session.txt",
    ],
)
def test_secret_private_and_runtime_paths_are_blocked(tmp_path: Path, relative: str) -> None:
    project = tmp_path / "repo"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-real-secret", encoding="utf-8")
    result = _engine(tmp_path).import_source(_request(project, relative))
    assert result.status == "blocked"
    assert result.reason_code.startswith("path.")


def test_secret_payload_rejects_entire_revision(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    path = project / "docs" / "unsafe.md"
    path.parent.mkdir(parents=True)
    private_key_marker = "-----BEGIN PRIVATE " + "KEY-----"
    path.write_text(f"{private_key_marker}\nfixture\n", encoding="utf-8")
    engine = _engine(tmp_path)
    result = engine.import_source(_request(project, "docs/unsafe.md"))
    assert result.status == "blocked"
    assert result.reason_code == "secret.private_key"
    assert engine.store.status()["counts"]["knowledge_fragments"] == 0


@pytest.mark.parametrize("source", ["../outside.md", "docs/../../outside.md"])
def test_scope_escape_is_rejected(tmp_path: Path, source: str) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    result = _engine(tmp_path).import_source(_request(project, source))
    assert result.status == "blocked"
    assert result.reason_code == "scope.escape"


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    docs = project / "docs"
    docs.mkdir(parents=True)
    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    link = docs / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows account")
    result = _engine(tmp_path).import_source(_request(project, "docs/link.md"))
    assert result.status == "blocked"
    assert result.reason_code in {"scope.escape", "path.reparse"}


def test_ntfs_ads_and_percent_aliases_are_rejected_before_read(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    engine = _engine(tmp_path)
    ads = engine.import_source(_request(project, "docs/file.md:secret"))
    encoded = engine.import_source(_request(project, "docs/%2e%2e/secret.md"))
    assert ads.reason_code == "path.alternate_data_stream"
    assert encoded.reason_code == "path.encoded_or_unicode_alias"


def test_owner_and_project_scope_do_not_leak(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for project in (first, second):
        (project / "docs").mkdir(parents=True)
    (first / "docs" / "note.md").write_text("alpha private fixture", encoding="utf-8")
    (second / "docs" / "note.md").write_text("beta private fixture", encoding="utf-8")
    engine = _engine(tmp_path)
    engine.import_source(_request(first, "docs/note.md"))
    engine.import_source(_request(second, "docs/note.md"))
    from services.knowledge.contracts import RetrievalRequestV1

    result = engine.retrieve(
        RetrievalRequestV1(project_path=str(second), query="alpha private", token_budget=512)
    )
    assert all("alpha" not in item.content for item in result.fragments)
