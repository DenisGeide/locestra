from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from services.coding.config import load_coding_policy
from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    DataClassification,
)


ROOT = Path(__file__).resolve().parents[1]


def _request(
    repository: Path,
    *,
    mode: CodingMode,
    permissions: CodingPermissionsV1,
    commit_message: str | None = None,
    rule_scope_paths: list[str] | None = None,
    expected_diff_paths: list[str] | None = None,
    forbidden_diff_paths: list[str] | None = None,
) -> CodingTaskRequestV1:
    return CodingTaskRequestV1(
        task_id="contract-task",
        request_id="contract-request",
        goal="Make the bounded synthetic fixture change.",
        repository_path=str(repository),
        mode=mode,
        risk=CodingRisk.MEDIUM,
        constraints=["Never push."],
        acceptance_criteria=["The requested behavior is verified."],
        verification_plan=["Run the relevant standard-library test."],
        permissions=permissions,
        rule_scope_paths=rule_scope_paths or [],
        expected_diff_paths=expected_diff_paths or [],
        forbidden_diff_paths=forbidden_diff_paths or [],
        commit_message=commit_message,
    )


def test_committed_coding_policy_is_bounded_and_explicitly_denies_git_side_effects():
    policy = load_coding_policy(ROOT / "config" / "coding.json")

    assert policy.schema_version == "1.0"
    assert policy.policy_version == "2026-07-15.4"
    assert policy.context_token_budget == 4096
    assert policy.max_local_attempts == 2
    assert policy.branch_prefix.endswith("-")
    assert policy.local_semantic_expected_executable_path == "auto"
    assert policy.local_semantic_expected_executable_sha256 == "auto"
    assert policy.max_diff_bytes <= 64 * 1024 * 1024
    assert {"python", "uv", "git"}.issubset(policy.allowed_verification_programs)
    assert {"push", "commit", "deploy", "reset", "clean"}.issubset(
        policy.denied_verification_tokens
    )


def test_coding_policy_rejects_unknown_fields_duplicate_tokens_and_unsafe_prefix(
    tmp_path: Path,
):
    payload = json.loads((ROOT / "config" / "coding.json").read_text(encoding="utf-8"))
    candidate = tmp_path / "coding.json"

    payload["unexpected"] = "forbidden"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_coding_policy(candidate)

    payload.pop("unexpected")
    payload["allowed_verification_programs"] = ["python", "PYTHON"]
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="unique"):
        load_coding_policy(candidate)

    payload["allowed_verification_programs"] = ["python"]
    payload["branch_prefix"] = "task/../../escape-"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="prefix"):
        load_coding_policy(candidate)

    payload["branch_prefix"] = "local-agent/task-"
    payload["local_semantic_expected_executable_path"] = "relative/ollama.exe"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="absolute path"):
        load_coding_policy(candidate)

    payload["local_semantic_expected_executable_path"] = "auto"
    payload["local_semantic_expected_executable_sha256"] = "not-a-digest"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="digest"):
        load_coding_policy(candidate)


def test_doctor_supports_portable_ollama_identity_and_strict_runtime_pin():
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "LOCESTRA_OLLAMA_EXECUTABLE" in doctor
    assert "LOCESTRA_OLLAMA_EXECUTABLE_SHA256" in doctor
    assert "$configuredSemanticExecutable -eq 'auto'" in doctor
    assert "$configuredSemanticDigest -eq 'auto'" in doctor
    assert "runtime-derived" in doctor
    assert "Get-FileHash" in doctor
    assert "digest mismatch" in doctor


def test_codex_is_optional_by_default_and_lifecycle_checks_follow_enable_flag():
    platform = json.loads((ROOT / "config" / "platform.json").read_text(encoding="utf-8"))
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert platform["settings"]["ENABLE_CODEX_EXEC"] is False
    assert "Get-PlatformBooleanSetting" in bootstrap
    assert "ENABLE_CODEX_EXEC=true requires an authenticated Codex CLI" in bootstrap
    assert "Codex CLI and login are optional" in bootstrap
    assert "(?im)^\\s*Logged in(?:\\s|$)" in bootstrap
    assert "if ($script:CodexExecutionRequired) { Check-Command 'codex' }" in doctor
    assert "else { Check-OptionalCommand 'codex' }" in doctor
    assert "Optional Codex is not authenticated" in doctor
    assert "Required Codex cloud authentication" in doctor
    assert "(?im)^\\s*Logged in(?:\\s|$)" in doctor


def test_bootstrap_fails_closed_on_strong_base_and_alias_model_drift():
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")

    assert "07d35212591fc27746f0a317c975a6d68754fb38e9053d82e25f06057af28522" in bootstrap
    assert "local_semantic_expected_model_digest" in bootstrap
    assert "Ollama model drift" in bootstrap

    base_check = bootstrap.index("-Name 'qwen3.6:35b'")
    alias_create = bootstrap.index("ollama create local-strong")
    alias_check = bootstrap.index("-Name 'local-strong'")
    assert base_check < alias_create < alias_check


def test_powershell_codex_enable_resolver_obeys_precedence_and_rejects_invalid_values(
    tmp_path: Path,
):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to execute the lifecycle settings resolver")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "platform.json").write_text(
        json.dumps({"schema_version": "1.0", "settings": {"ENABLE_CODEX_EXEC": False}}),
        encoding="utf-8",
    )
    resolver = ROOT / "scripts" / "lib" / "platform-settings.ps1"

    def resolve(*, env_file: str = "", process_value: str | None = None):
        (tmp_path / ".env").write_text(env_file, encoding="utf-8")
        command = (
            f". '{resolver}'; "
            f"[Environment]::SetEnvironmentVariable('ENABLE_CODEX_EXEC', "
            f"{repr(process_value) if process_value is not None else '$null'}, 'Process'); "
            f"Get-PlatformBooleanSetting -Root '{tmp_path}' "
            "-Name 'ENABLE_CODEX_EXEC' -Default $false"
        )
        return subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            capture_output=True,
            check=False,
        )

    committed = resolve()
    assert committed.returncode == 0
    assert committed.stdout.strip().casefold() == "false"

    env_override = resolve(env_file="ENABLE_CODEX_EXEC=true\n")
    assert env_override.returncode == 0
    assert env_override.stdout.strip().casefold() == "true"

    process_override = resolve(
        env_file="ENABLE_CODEX_EXEC=true\n",
        process_value="false",
    )
    assert process_override.returncode == 0
    assert process_override.stdout.strip().casefold() == "false"

    invalid = resolve(process_value="sometimes")
    assert invalid.returncode != 0
    assert "must be exactly true or false" in invalid.stderr


def test_cloud_execution_requires_public_fixture_and_push_deploy_are_unrepresentable():
    with pytest.raises(ValidationError, match="public fixture"):
        CodingPermissionsV1(
            cloud_execution=True,
            data_classification=DataClassification.INTERNAL,
        )

    allowed = CodingPermissionsV1(
        cloud_execution=True,
        data_classification=DataClassification.PUBLIC,
    )
    assert allowed.cloud_execution is True
    assert allowed.push is False and allowed.deploy is False

    with pytest.raises(ValidationError):
        CodingPermissionsV1.model_validate({"push": True})
    with pytest.raises(ValidationError):
        CodingPermissionsV1.model_validate({"deploy": True})


def test_commit_and_mode_permissions_fail_closed(tmp_path: Path):
    with pytest.raises(ValidationError, match="local commit requires"):
        CodingPermissionsV1(local_commit=True)

    with pytest.raises(ValidationError, match="read-only"):
        _request(
            tmp_path,
            mode=CodingMode.READ_ONLY,
            permissions=CodingPermissionsV1(modify_files=True),
        )

    with pytest.raises(ValidationError, match="write task requires"):
        _request(
            tmp_path,
            mode=CodingMode.WRITE,
            permissions=CodingPermissionsV1(),
        )

    with pytest.raises(ValidationError, match="commit_message"):
        _request(
            tmp_path,
            mode=CodingMode.WRITE,
            permissions=CodingPermissionsV1(modify_files=True),
            commit_message="fixture commit",
        )

    request = _request(
        tmp_path,
        mode=CodingMode.WRITE,
        permissions=CodingPermissionsV1(modify_files=True, local_commit=True),
        commit_message="fixture commit",
    )
    assert request.permissions.local_commit is True
    assert request.permissions.push is False


@pytest.mark.parametrize(
    "field", ["rule_scope_paths", "expected_diff_paths", "forbidden_diff_paths"]
)
@pytest.mark.parametrize(
    "unsafe",
    [".", "/src", "../src", "C:\\outside", "src/../../outside", "src/file.py\nother"],
)
def test_coding_scope_paths_cannot_escape_repository(
    tmp_path: Path,
    field: str,
    unsafe: str,
):
    with pytest.raises(ValidationError, match="inside the repository"):
        payload = dict(
            task_id="unsafe-scope",
            request_id="unsafe-scope-request",
            goal="Synthetic scope validation.",
            repository_path=str(tmp_path),
            mode=CodingMode.WRITE,
            risk=CodingRisk.LOW,
            acceptance_criteria=["The scope is bounded."],
            verification_plan=["Review the diff."],
            permissions=CodingPermissionsV1(modify_files=True),
        )
        payload[field] = [unsafe]
        CodingTaskRequestV1(**payload)


def test_read_only_rule_scope_does_not_grant_write_scope(tmp_path: Path):
    request = _request(
        tmp_path,
        mode=CodingMode.READ_ONLY,
        permissions=CodingPermissionsV1(),
        rule_scope_paths=["src/calculator.py"],
    )

    assert request.rule_scope_paths == ["src/calculator.py"]
    assert request.expected_diff_paths == []
    assert request.permissions.modify_files is False


def test_forbidden_diff_scope_is_backward_compatible_and_fails_on_conflict(
    tmp_path: Path,
):
    legacy = _request(
        tmp_path,
        mode=CodingMode.WRITE,
        permissions=CodingPermissionsV1(modify_files=True),
        expected_diff_paths=["src"],
    )
    assert legacy.forbidden_diff_paths == []

    carved_out = _request(
        tmp_path,
        mode=CodingMode.WRITE,
        permissions=CodingPermissionsV1(modify_files=True),
        expected_diff_paths=["src"],
        forbidden_diff_paths=["src/generated"],
    )
    assert carved_out.forbidden_diff_paths == ["src/generated"]

    with pytest.raises(ValidationError, match="conflicts with a forbidden"):
        _request(
            tmp_path,
            mode=CodingMode.WRITE,
            permissions=CodingPermissionsV1(modify_files=True),
            expected_diff_paths=["src/generated/file.py"],
            forbidden_diff_paths=["src/generated"],
        )
