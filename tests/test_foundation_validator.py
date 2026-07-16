from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.validate_foundation import (
    EXPECTED_STAGES,
    REQUIRED_DOCUMENTS,
    _is_forbidden_path,
    check_git_diff_secrets,
    check_manifest_contract,
    check_markdown_links,
    check_roadmap_stages,
    run_checks,
)


PUBLIC_CONFIG = """\
LOCAL_FAST_MODEL=local-fast
LOCAL_STRONG_MODEL=local-strong
LOCAL_AGENT_MODEL=local-strong
CODEX_MODEL=gpt-5.6-sol
OLLAMA_BASE_URL=http://127.0.0.1:11434
FAST_OLLAMA_BASE_URL=http://127.0.0.1:11435
GATEWAY_PORT=8787
VOICE_PORT=8788
OPEN_WEBUI_PORT=3737
N8N_PORT=5678
COMFYUI_URL=http://127.0.0.1:8388
WHISPER_MODEL=large-v3-turbo
"""

MANIFEST = """\
# System Manifest

| Component | Model/profile | Endpoint |
|---|---|---|
| Fast Ollama | local-fast / qwen3.5:4b | http://127.0.0.1:11435 |
| Strong Ollama | local-strong / qwen3.6:35b | http://127.0.0.1:11434 |
| Qwen Code local coding agent | local-strong | local OpenAI-compatible endpoint |
| Codex | gpt-5.6-sol | cloud |
| Gateway | local-agent-auto | http://127.0.0.1:8787 |
| Voice / Whisper | large-v3-turbo | http://127.0.0.1:8788 |
| Open WebUI | UI | http://127.0.0.1:3737 |
| n8n | automation | http://127.0.0.1:5678 |
| ComfyUI | image | http://127.0.0.1:8388 |
"""


def _write(root: Path, relative: str, text: str = "Owner: platform\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _valid_repo(tmp_path: Path) -> Path:
    for relative in REQUIRED_DOCUMENTS:
        _write(tmp_path, relative)
    _write(tmp_path, "SYSTEM_MANIFEST.md", MANIFEST)
    _write(tmp_path, ".env.example", PUBLIC_CONFIG)
    _write(tmp_path, "models/fast.Modelfile", "FROM qwen3.5:4b\n")
    _write(tmp_path, "models/strong.Modelfile", "FROM qwen3.6:35b\n")
    _write(
        tmp_path,
        "docs/ROADMAP.md",
        "| Stage | Result | Status |\n|---|---|---|\n"
        + "\n".join(f"| {stage} | Stage {stage} | Planned |" for stage in EXPECTED_STAGES)
        + "\n",
    )
    _write(
        tmp_path,
        "README.md",
        "See [the charter](docs/PROJECT_CHARTER.md) and [the web](https://example.com).\n",
    )
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "validator@example.invalid")
    _git(tmp_path, "config", "user.name", "Foundation Validator")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--quiet", "-m", "fixture")
    return tmp_path


def _codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def test_valid_foundation_fixture_passes(tmp_path):
    root = _valid_repo(tmp_path)
    assert all(not findings for _, findings in run_checks(root))


def test_stage_001_architecture_documents_are_required():
    assert {
        "docs/ARCHITECTURE.md",
        "docs/TARGET_ARCHITECTURE.md",
        "docs/CONTEXT_STRATEGY.md",
        "docs/MEMORY_STRATEGY.md",
        "docs/CODEX_HANDOFF.md",
        "docs/OPERATIONS.md",
        "docs/CONFIGURATION.md",
        "docs/CONTRACTS.md",
    }.issubset(REQUIRED_DOCUMENTS)


def test_roadmap_stage_check_detects_missing_duplicate_and_unexpected(tmp_path):
    root = _valid_repo(tmp_path)
    roadmap = root / "docs/ROADMAP.md"
    rows = [f"| {stage} | Stage | Planned |" for stage in EXPECTED_STAGES if stage != "012"]
    rows.extend(["| 000 | Duplicate | Planned |", "| 013 | Unexpected | Planned |"])
    roadmap.write_text("\n".join(rows), encoding="utf-8")

    findings = check_roadmap_stages(root)

    assert _codes(findings) == {
        "roadmap.stage_missing",
        "roadmap.stage_duplicate",
        "roadmap.stage_unexpected",
    }


def test_markdown_links_ignore_urls_anchors_and_code_but_report_missing_target(tmp_path):
    root = _valid_repo(tmp_path)
    _write(
        root,
        "docs/ROADMAP.md",
        """\
[valid](PROJECT_CHARTER.md)
[url](https://example.com/reference)
[anchor](#local)
`[inline sample](not-real.md)`
```md
[fenced sample](also-not-real.md)
```
[broken](missing.md)
""",
    )

    findings = check_markdown_links(root)

    assert len(findings) == 1
    assert findings[0].code == "link.missing"
    assert "missing.md" in findings[0].message


def test_markdown_links_reject_absolute_local_path(tmp_path):
    root = _valid_repo(tmp_path)
    _write(root, "docs/ROADMAP.md", r"[host file](C:\Users\ExampleUser\secret.md)" + "\n")

    findings = check_markdown_links(root)

    assert len(findings) == 1
    assert findings[0].code == "link.absolute"


def test_markdown_links_reject_file_uri_and_reference_style_absolute_path(tmp_path):
    root = _valid_repo(tmp_path)
    _write(
        root,
        "docs/ROADMAP.md",
        "[file URI](file:///C:/Users/ExampleUser/secret.md)\n"
        "[reference][private]\n"
        "[private]: C:\\Users\\ExampleUser\\secret.md\n",
    )

    findings = check_markdown_links(root)

    assert [finding.code for finding in findings] == ["link.absolute", "link.absolute"]


def test_markdown_links_support_balanced_parentheses_in_relative_destination(tmp_path):
    root = _valid_repo(tmp_path)
    _write(root, "docs/target(v2).md", "ok\n")
    _write(root, "docs/ROADMAP.md", "[valid](target(v2).md)\n")

    assert check_markdown_links(root) == []


def test_manifest_contract_detects_missing_endpoint_mapping(tmp_path):
    root = _valid_repo(tmp_path)
    manifest = (root / "SYSTEM_MANIFEST.md").read_text(encoding="utf-8")
    (root / "SYSTEM_MANIFEST.md").write_text(
        manifest.replace("http://127.0.0.1:8787", "not-verified"),
        encoding="utf-8",
    )

    findings = check_manifest_contract(root)

    assert "manifest.fact_missing" in _codes(findings)
    assert any("gateway endpoint" in finding.message for finding in findings)


def test_manifest_contract_uses_exact_boundaries(tmp_path):
    root = _valid_repo(tmp_path)
    manifest = (root / "SYSTEM_MANIFEST.md").read_text(encoding="utf-8")
    (root / "SYSTEM_MANIFEST.md").write_text(
        manifest.replace("127.0.0.1:8787", "127.0.0.1:87870"),
        encoding="utf-8",
    )

    findings = check_manifest_contract(root)

    assert any("gateway endpoint" in finding.message for finding in findings)


def test_manifest_contract_checks_full_endpoint_scheme_and_path(tmp_path):
    root = _valid_repo(tmp_path)
    public_config = (root / ".env.example").read_text(encoding="utf-8")
    (root / ".env.example").write_text(
        public_config.replace(
            "http://127.0.0.1:11434",
            "https://127.0.0.1:11434/v1",
        ),
        encoding="utf-8",
    )

    findings = check_manifest_contract(root)

    assert any("strong Ollama endpoint" in finding.message for finding in findings)


def test_manifest_contract_checks_cloud_voice_and_base_models(tmp_path):
    root = _valid_repo(tmp_path)
    manifest = (root / "SYSTEM_MANIFEST.md").read_text(encoding="utf-8")
    manifest = manifest.replace("gpt-5.6-sol", "wrong-codex-model")
    manifest = manifest.replace("large-v3-turbo", "wrong-whisper-model")
    manifest = manifest.replace("qwen3.5:4b", "wrong-fast-base")
    manifest = manifest.replace("qwen3.6:35b", "wrong-strong-base")
    (root / "SYSTEM_MANIFEST.md").write_text(manifest, encoding="utf-8")

    messages = [finding.message for finding in check_manifest_contract(root)]

    assert any("Codex model" in message for message in messages)
    assert any("Whisper model" in message for message in messages)
    assert any("base model for local-fast" in message for message in messages)
    assert any("base model for local-strong" in message for message in messages)


def test_git_diff_scan_rejects_forbidden_staged_file_without_printing_value(tmp_path):
    root = _valid_repo(tmp_path)
    secret_value = "sk-" + "A" * 40
    _write(root, ".env", f"API_KEY={secret_value}\n")
    _git(root, "add", ".env", "--force")

    findings = check_git_diff_secrets(root)

    assert _codes(findings) == {"git.forbidden_path"}
    assert all(secret_value not in finding.message for finding in findings)


def test_git_diff_scan_finds_secret_shaped_value_in_tracked_text(tmp_path):
    root = _valid_repo(tmp_path)
    secret_value = "ghp_" + "B" * 36
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write(f"credential={secret_value}\n")

    findings = check_git_diff_secrets(root)

    assert "git.secret_candidate" in _codes(findings)
    assert all(secret_value not in finding.message for finding in findings)


def test_git_diff_scan_finds_secret_left_only_in_index(tmp_path):
    root = _valid_repo(tmp_path)
    readme = root / "README.md"
    original = readme.read_text(encoding="utf-8")
    secret_value = "ghp_" + "C" * 36
    readme.write_text(original + f"credential={secret_value}\n", encoding="utf-8")
    _git(root, "add", "README.md")
    readme.write_text(original, encoding="utf-8")

    findings = check_git_diff_secrets(root)

    assert "git.secret_candidate" in _codes(findings)
    assert any("staged" in finding.message for finding in findings)
    assert all(secret_value not in finding.message for finding in findings)


def test_git_diff_scan_blocks_common_secret_stores_without_reading_values(tmp_path):
    root = _valid_repo(tmp_path)
    secret_value = "N" * 40
    _write(root, ".npmrc", f"//registry.example/:_authToken={secret_value}\n")
    _write(root, "prod.env", f"CLIENT_SECRET={secret_value}\n")
    _git(root, "add", ".npmrc", "prod.env", "--force")

    findings = check_git_diff_secrets(root)

    assert _codes(findings) == {"git.forbidden_path"}
    assert {finding.path for finding in findings} == {".npmrc", "prod.env"}
    assert all(secret_value not in finding.message for finding in findings)


def test_git_diff_scan_finds_telegram_token_and_explicit_untracked_foundation_secret(tmp_path):
    root = _valid_repo(tmp_path)
    telegram_value = "123456789:" + "T" * 35
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write(f"TELEGRAM_BOT_TOKEN={telegram_value}\n")
    untracked_value = "github_pat_" + "U" * 32
    _write(root, "scripts/validate_foundation.py", f"client_secret={untracked_value}\n")

    findings = check_git_diff_secrets(root)

    assert sum(finding.code == "git.secret_candidate" for finding in findings) == 2
    assert any(finding.path == "scripts/validate_foundation.py" for finding in findings)
    assert all(telegram_value not in finding.message for finding in findings)
    assert all(untracked_value not in finding.message for finding in findings)


def test_git_diff_scan_finds_bare_telegram_token_and_jwt(tmp_path):
    root = _valid_repo(tmp_path)
    telegram_value = "123456789:" + "B" * 35
    jwt_value = "eyJ" + "A" * 12 + "." + "B" * 16 + "." + "C" * 16
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write(f'credential = "{telegram_value}"\n')
        stream.write(f'session = "{jwt_value}"\n')

    findings = check_git_diff_secrets(root)

    assert sum(finding.code == "git.secret_candidate" for finding in findings) == 2
    assert all(telegram_value not in finding.message for finding in findings)
    assert all(jwt_value not in finding.message for finding in findings)


def test_git_diff_scan_rejects_runtime_data_and_handoff_paths(tmp_path):
    root = _valid_repo(tmp_path)
    _write(root, "inbox/codex-handoff.md", "private task\n")
    _write(root, "data/task-export.json", "{}\n")
    _write(root, "modules/model.bin", "not a real model\n")
    _git(
        root,
        "add",
        "--force",
        "inbox/codex-handoff.md",
        "data/task-export.json",
        "modules/model.bin",
    )

    findings = check_git_diff_secrets(root)

    assert _codes(findings) == {"git.forbidden_path"}
    assert {finding.path for finding in findings} == {
        "data/task-export.json",
        "inbox/codex-handoff.md",
        "modules/model.bin",
    }


def test_forbidden_runtime_directory_names_are_scoped_to_repo_root():
    assert not _is_forbidden_path("src/modules/router.py")
    assert not _is_forbidden_path("tests/data/fixture.json")
    assert not _is_forbidden_path("services/logs/parser.py")
    assert not _is_forbidden_path("docs/inbox/example.md")
    assert _is_forbidden_path("modules/model.bin")
    assert _is_forbidden_path("data/task-export.json")
    assert _is_forbidden_path("secrets.yaml")
    assert _is_forbidden_path("tokens.json")
    assert _is_forbidden_path("client-secrets.toml")


def test_git_diff_scan_does_not_read_arbitrary_untracked_scratch_file(tmp_path):
    root = _valid_repo(tmp_path)
    value = "github_pat_" + "S" * 32
    _write(root, "scratch/private-notes.md", f"token={value}\n")

    assert check_git_diff_secrets(root) == []


def test_git_diff_scan_allows_documented_placeholders(tmp_path):
    root = _valid_repo(tmp_path)
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write("api_key=${OPENAI_API_KEY}\npassword=<set-locally>\n")

    assert check_git_diff_secrets(root) == []
