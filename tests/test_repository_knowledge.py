from __future__ import annotations

import subprocess
from pathlib import Path

from services.knowledge.contracts import RetrievalRequestV1, SourceKind, SourceRegistrationV1
from services.knowledge.engine import KnowledgeEngine
from services.knowledge.store import KnowledgeStore, conservative_token_estimate


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)


def _fixture_repo(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "fixture@example.invalid")
    _git(project, "config", "user.name", "Knowledge Fixture")
    _write(project / "AGENTS.md", "# Agent rules\n")
    _write(project / "README.md", "# Fixture repository\nRun the calculator tests.\n")
    _write(
        project / "pyproject.toml",
        "[project]\nname='fixture'\nversion='1.0.0'\n[tool.pytest.ini_options]\ntestpaths=['tests']\n",
    )
    _write(project / "services" / "app.py", "def calculate_total(value: int) -> int:\n    return value + 1\n")
    _write(project / "tests" / "test_app.py", "def deleted_only_symbol():\n    assert True\n")
    _write(project / ".env", "SHOULD_NEVER_BE_INDEXED=1\n")
    _git(project, "add", ".")
    _git(project, "commit", "-qm", "fixture baseline")
    return project


def _engine(tmp_path: Path) -> KnowledgeEngine:
    return KnowledgeEngine(KnowledgeStore(tmp_path / "knowledge.sqlite3", harden_permissions=False))


def _registration(project: Path) -> SourceRegistrationV1:
    return SourceRegistrationV1(project_path=str(project), consent=True)


def test_repository_map_and_budgeted_retrieval(tmp_path: Path) -> None:
    project = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    indexed = engine.index_repository(_registration(project))
    assert indexed["blocked_files"] >= 1
    repository_map = engine.repository_map(str(project))
    assert repository_map is not None
    assert repository_map.languages["Python"] == 2
    assert "pyproject.toml" in repository_map.manifests
    assert "services/app.py" in repository_map.entry_points
    assert "services" in repository_map.modules
    assert "tests/test_app.py" in repository_map.tests
    assert "uv run pytest" in repository_map.commands
    assert repository_map.agents_hierarchy == ["AGENTS.md"]
    app_file = next(item for item in repository_map.files if item.path == "services/app.py")
    assert "calculate_total" in app_file.symbols
    assert all(item.path != ".env" for item in repository_map.files)

    result = engine.retrieve(
        RetrievalRequestV1(
            project_path=str(project),
            query="calculate total value",
            token_budget=256,
            max_fragments=2,
        )
    )
    assert result.fragments
    assert result.estimated_tokens <= 256
    fragment = result.fragments[0]
    assert fragment.provenance.source_uri == "project://services/app.py"
    assert fragment.provenance.start_line == 1
    assert fragment.provenance.project_commit_sha
    assert fragment.score > 0
    assert fragment.reason.startswith("SQLite FTS5")


def test_duplicate_index_is_idempotent(tmp_path: Path) -> None:
    project = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    first = engine.index_repository(_registration(project))
    before = engine.store.status()["counts"].copy()
    second = engine.index_repository(_registration(project))
    after = engine.store.status()["counts"].copy()
    assert first["unchanged"] is False
    assert second["unchanged"] is True
    assert before == after


def test_changed_deleted_and_renamed_files_invalidate_active_generation(tmp_path: Path) -> None:
    project = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    engine.index_repository(_registration(project))

    _write(project / "services" / "app.py", "def replacement_logic() -> str:\n    return 'fresh'\n")
    (project / "docs").mkdir()
    _git(project, "mv", "README.md", "docs/OVERVIEW.md")
    (project / "tests" / "test_app.py").unlink()
    changed = engine.index_repository(_registration(project))
    assert changed["unchanged"] is False
    assert changed["renamed"]

    old_content = engine.retrieve(
        RetrievalRequestV1(project_path=str(project), query="calculate total", token_budget=512)
    )
    deleted_content = engine.retrieve(
        RetrievalRequestV1(project_path=str(project), query="deleted only symbol", token_budget=512)
    )
    new_content = engine.retrieve(
        RetrievalRequestV1(project_path=str(project), query="replacement logic fresh", token_budget=512)
    )
    assert old_content.fragments == []
    assert deleted_content.fragments == []
    assert new_content.fragments[0].provenance.source_uri == "project://services/app.py"
    repository_map = engine.repository_map(str(project))
    assert repository_map is not None
    paths = {item.path for item in repository_map.files}
    assert "README.md" not in paths
    assert "docs/OVERVIEW.md" in paths
    assert "tests/test_app.py" not in paths


def test_context_builder_selects_map_and_bounded_evidence(tmp_path: Path) -> None:
    project = _fixture_repo(tmp_path)
    engine = _engine(tmp_path)
    engine.index_repository(_registration(project))
    context = engine.build_context(
        project_path=str(project),
        goal="understand calculate total",
        token_budget=2_048,
        constraints=["do not modify files"],
        modified_files=["services/app.py"],
        unresolved_errors=["none"],
        verification_plan=["run fixture tests"],
    )
    assert context.repository_summary["languages"]["Python"] == 2
    assert context.estimated_tokens <= 2_048
    assert conservative_token_estimate(context.model_dump_json()) == context.estimated_tokens
    assert context.evidence.fragments
    assert context.untrusted_evidence is True


def test_secret_shaped_git_subject_is_not_indexed(tmp_path: Path) -> None:
    project = _fixture_repo(tmp_path)
    secret_subject = "pass" + "word=history_fixture_credential"
    _write(project / "docs" / "later.md", "ordinary tracked change\n")
    _git(project, "add", "docs/later.md")
    _git(project, "commit", "-qm", secret_subject)
    engine = _engine(tmp_path)
    indexed = engine.index_repository(_registration(project))
    assert any(item["path"] == "git://history" for item in indexed["blocked"])
    result = engine.retrieve(
        RetrievalRequestV1(project_path=str(project), query="history fixture credential", token_budget=512)
    )
    assert all(fragment.source_kind is not SourceKind.GIT_HISTORY for fragment in result.fragments)
    assert all("history_fixture_credential" not in fragment.content for fragment in result.fragments)
