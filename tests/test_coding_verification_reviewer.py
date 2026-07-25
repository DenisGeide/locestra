from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.coding.artifacts import ArtifactStore
from services.coding.contracts import (
    ArtifactKind,
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CommandStatus,
    DataClassification,
    ExecutorKind,
    ReviewResultV1,
    ReviewSeverity,
    ReviewVerdict,
    VerificationCommandV1,
    is_successful_review_delivery,
)
from services.coding.process import ProcessOutcome
from services.coding.reviewer import merge_codex_review
from services.coding.verification import (
    VerificationPolicyError,
    VerificationRunner,
    is_semantic_verification_argv,
    resolve_verification_argv,
    validate_verification_argv,
)


@pytest.fixture(autouse=True)
def _ample_watchdog_free_space(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.coding.resources._disk_usage",
        lambda path: SimpleNamespace(free=2**60),
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["powershell", "-Command", "pytest"],
        ["cmd.exe", "/c", "pytest"],
        ["git", "push"],
        ["git", "commit", "-m", "forbidden"],
        ["git", "status"],
        ["npm", "install"],
        ["pnpm", "add", "pytest"],
        ["python", "script.py"],
        ["python", "-m", "pip", "install", "pytest"],
        ["uv", "pip", "install", "pytest"],
        ["uv", "tool", "run", "pytest"],
        ["uv", "run", "pytest"],
        ["cargo", "publish"],
        ["go", "install", "example.invalid/tool"],
        ["pytest", "--deploy=production"],
        ["pytest", "--rootdir", ".."],
        ["cargo", "test", "--manifest-path", "C:\\outside\\Cargo.toml"],
        ["dotnet", "test", "--output", "..\\outside"],
    ],
)
def test_verification_argv_rejects_shell_install_git_mutation_and_denied_lifecycle(
    argv: list[str],
):
    with pytest.raises(VerificationPolicyError):
        validate_verification_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-q"],
        ["python", "-m", "unittest", "discover", "-s", "tests"],
        ["uv", "run", "--no-sync", "python", "-m", "pytest", "-q"],
        ["npm", "run", "lint"],
        ["cargo", "clippy", "--all-targets"],
        ["go", "test", "./..."],
        ["dotnet", "build"],
        ["node", "tests/smoke.mjs"],
        ["git", "diff", "--check"],
        ["git", "status", "--porcelain=v1", "-z"],
    ],
)
def test_verification_argv_accepts_only_documented_non_destructive_shapes(
    argv: list[str],
):
    assert validate_verification_argv(argv) == argv


@pytest.mark.parametrize(
    ("argv", "semantic"),
    [
        (["pytest", "-q"], True),
        (["python", "-m", "unittest", "discover"], True),
        (["npm.cmd", "run", "typecheck"], True),
        (["node.exe", "tests/smoke.mjs"], True),
        (["git", "diff", "--check"], False),
        (["git.exe", "status", "--porcelain=v1", "-z"], False),
    ],
)
def test_semantic_verifier_classifier_never_treats_git_inspection_as_acceptance_evidence(
    argv: list[str], semantic: bool
):
    assert is_semantic_verification_argv(argv) is semantic


def test_verification_rejects_arbitrary_absolute_program_with_trusted_basename(
    tmp_path: Path,
):
    attacker = tmp_path / ("pytest.exe" if os.name == "nt" else "pytest")
    attacker.write_bytes(b"not a trusted executable")
    if os.name != "nt":
        attacker.chmod(0o700)

    with pytest.raises(VerificationPolicyError, match="trusted|canonical"):
        validate_verification_argv([str(attacker), "-q"])


def test_verification_resolution_skips_repository_local_path_hijack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted-bin"
    repository.mkdir()
    trusted_bin.mkdir()
    executable_name = "pytest.exe" if os.name == "nt" else "pytest"
    local_hijack = repository / executable_name
    trusted = trusted_bin / executable_name
    for path in (local_hijack, trusted):
        path.write_bytes(b"fixture")
        if os.name != "nt":
            path.chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join([str(repository), str(trusted_bin)]))

    resolved = resolve_verification_argv(["pytest", "-q"], cwd=repository)

    assert Path(resolved[0]).resolve(strict=True) == trusted.resolve(strict=True)
    assert resolved[1:] == ["-q"]


def test_verification_resolution_fails_closed_when_only_path_hit_is_in_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    executable = repository / ("pytest.exe" if os.name == "nt" else "pytest")
    executable.write_bytes(b"fixture")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(repository))

    with pytest.raises(
        VerificationPolicyError, match="trusted verification executable"
    ):
        resolve_verification_argv(["pytest", "-q"], cwd=repository)


def test_verification_rejects_relative_repository_local_executable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted-bin"
    repository.mkdir()
    trusted_bin.mkdir()
    executable_name = "pytest.exe" if os.name == "nt" else "pytest"
    local_hijack = repository / executable_name
    trusted = trusted_bin / executable_name
    for path in (local_hijack, trusted):
        path.write_bytes(b"fixture")
        if os.name != "nt":
            path.chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join([str(repository), str(trusted_bin)]))
    relative = f".{os.sep}{executable_name}"

    with pytest.raises(VerificationPolicyError, match="canonical trusted"):
        resolve_verification_argv([relative, "-q"], cwd=repository)


class _RecordingProcessRunner:
    def __init__(self, outcome: ProcessOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
        self.calls.append({"argv": list(argv), **kwargs})
        return self.outcome


def test_verification_runner_uses_argv_without_shell_and_persists_bounded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    process = _RecordingProcessRunner(
        ProcessOutcome(
            status=CommandStatus.PASSED,
            exit_code=0,
            stdout="one test passed\n",
            stderr="synthetic warning\n",
            duration_ms=17,
        )
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    artifacts = ArtifactStore("verification-runner", root=tmp_path / "artifacts")
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.verification._trusted_docker", lambda repository: fake_docker
    )
    monkeypatch.setattr(
        "services.coding.verification._ensure_python_verifier_image",
        lambda *args, **kwargs: "sha256:" + "1" * 64,
    )
    monkeypatch.setattr(
        "services.coding.verification._run_docker_control",
        lambda *args, **kwargs: ProcessOutcome(
            status=CommandStatus.PASSED,
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
        ),
    )
    runner = VerificationRunner(artifact_store=artifacts, process_runner=process)
    command = VerificationCommandV1(
        argv=["python", "-m", "unittest", "discover", "-s", "tests"],
        purpose="Run the materialized fixture tests.",
        timeout_seconds=31,
    )

    result = runner.run(command, command_id="verify-1", cwd=repository)

    assert result.status is CommandStatus.PASSED
    assert result.exit_code == 0
    assert result.argv == [
        str(Path(sys.executable).resolve(strict=True)),
        *command.argv[1:],
    ]
    assert result.duration_ms == 17
    run_call = next(
        item
        for item in process.calls
        if item["argv"][1:3] == ["run", "--rm"]  # type: ignore[index]
    )
    docker_argv = run_call["argv"]
    assert docker_argv[0] == str(fake_docker)  # type: ignore[index]
    assert "--network" in docker_argv and "none" in docker_argv  # type: ignore[operator]
    assert "--read-only" in docker_argv  # type: ignore[operator]
    assert "--cap-drop" in docker_argv and "ALL" in docker_argv  # type: ignore[operator]
    assert "no-new-privileges:true" in docker_argv  # type: ignore[operator]
    assert "1000:1000" in docker_argv  # type: ignore[operator]
    assert "--pids-limit" in docker_argv  # type: ignore[operator]
    assert "--tmpfs" in docker_argv  # type: ignore[operator]
    policy = runner.policy
    assert docker_argv[docker_argv.index("--memory") + 1] == str(  # type: ignore[union-attr]
        policy.verifier_memory_bytes
    )
    assert docker_argv[docker_argv.index("--memory-swap") + 1] == str(  # type: ignore[union-attr]
        policy.verifier_memory_swap_bytes
    )
    assert docker_argv[docker_argv.index("--cpus") + 1] == str(  # type: ignore[union-attr]
        policy.verifier_cpus
    )
    assert "/usr/local/bin/python" in docker_argv  # type: ignore[operator]
    assert not any(
        "docker.sock" in str(item).casefold() or "HOST_ONLY_SENTINEL" in str(item)
        for item in docker_argv  # type: ignore[union-attr]
    )
    volume_sources = {
        str(docker_argv[index + 1]).rsplit(":/", 1)[0]
        for index, item in enumerate(docker_argv)  # type: ignore[arg-type]
        if item == "--volume"
    }
    task_root = artifacts.task_root.resolve(strict=True)
    assert str(repository.resolve(strict=True)) in volume_sources
    assert f"{repository / '.git'}:/workspace/.git:ro" not in docker_argv
    assert all(
        Path(source).resolve(strict=True) == repository.resolve(strict=True)
        or task_root in Path(source).resolve(strict=True).parents
        for source in volume_sources
    )
    assert run_call["cwd"] == repository.resolve(strict=True)
    assert run_call["timeout_seconds"] == 31
    assert "shell" not in run_call
    reference = artifacts.reference(result.output_artifact_id or "")
    assert reference.kind is ArtifactKind.COMMAND_OUTPUT
    assert Path(reference.path).read_text(encoding="utf-8") == (
        "one test passed\n\nsynthetic warning\n"
    )


def test_silent_verification_commands_receive_distinct_occurrence_bound_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    artifacts = ArtifactStore("silent-verification", root=tmp_path / "artifacts")
    process = _RecordingProcessRunner(
        ProcessOutcome(
            status=CommandStatus.PASSED,
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
        )
    )
    monkeypatch.setattr(
        "services.coding.verification._run_verification_in_docker",
        lambda **kwargs: process.outcome,
    )
    runner = VerificationRunner(artifact_store=artifacts, process_runner=process)
    command = VerificationCommandV1(
        argv=[sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        purpose="Run a silent deterministic verifier.",
        timeout_seconds=30,
    )

    first = runner.run(command, command_id="a1-verify-1", cwd=repository)
    second = runner.run(command, command_id="a1-verify-2", cwd=repository)

    assert first.status is CommandStatus.PASSED
    assert second.status is CommandStatus.PASSED
    assert first.output_artifact_id != second.output_artifact_id
    assert artifacts.reference(first.output_artifact_id or "").sha256 == artifacts.reference(
        second.output_artifact_id or ""
    ).sha256
    assert Path(
        artifacts.reference(first.output_artifact_id or "").path
    ).read_text(encoding="utf-8") == "[no output]"
    assert Path(
        artifacts.reference(second.output_artifact_id or "").path
    ).read_text(encoding="utf-8") == "[no output]"


def test_verification_sandbox_unavailable_fails_closed_without_host_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    process = _RecordingProcessRunner(
        ProcessOutcome(
            status=CommandStatus.PASSED,
            exit_code=0,
            stdout="HOST_FALLBACK_MUST_NOT_RUN",
            stderr="",
            duration_ms=1,
        )
    )
    artifacts = ArtifactStore("verification-no-fallback", root=tmp_path / "artifacts")

    def unavailable(**kwargs: object):
        raise VerificationPolicyError("pinned image unavailable")

    monkeypatch.setattr(
        "services.coding.verification._run_verification_in_docker", unavailable
    )
    result = VerificationRunner(artifact_store=artifacts, process_runner=process).run(
        VerificationCommandV1(
            argv=[sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            purpose="Fail closed when the sandbox cannot start.",
            timeout_seconds=30,
        ),
        command_id="no-fallback",
        cwd=repository,
    )

    assert result.status is CommandStatus.FAILED
    assert result.exit_code == 125
    assert process.calls == []
    evidence = artifacts.reference(result.output_artifact_id or "")
    output = Path(evidence.path).read_text(encoding="utf-8")
    assert "failed closed" in output
    assert "HOST_FALLBACK_MUST_NOT_RUN" not in output


def test_live_verification_sandbox_denies_host_profile_network_docker_and_descendant_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if not docker:
        pytest.skip("Docker is unavailable")
    available = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    repository = tmp_path / "owned-worktree"
    tests = repository / "tests"
    tests.mkdir(parents=True)
    host_profile = tmp_path / "real-host-profile"
    host_profile.mkdir()
    host_only_path = host_profile / "never-mounted.txt"
    host_only_path.write_text("HOST_ONLY_SENTINEL", encoding="utf-8")
    docker_desktop_host_path = (
        "/run/desktop/mnt/host/"
        + host_only_path.drive.rstrip(":").casefold()
        + host_only_path.as_posix().split(":", 1)[-1]
    )
    test_source = f"""\
import os
import socket
import subprocess
import sys
from pathlib import Path


def test_isolation_and_spawn_delayed_descendant():
    assert os.environ["HOME"] == "/home/local-agent"
    assert os.environ["USERPROFILE"] == "/home/local-agent"
    assert "LOCAL_AGENT_VERIFIER_SECRET" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    assert not Path({docker_desktop_host_path!r}).exists()
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.25)
    except OSError:
        pass
    else:
        raise AssertionError("network unexpectedly available")
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; "
            "time.sleep(1.5); "
            "Path('/local-agent/cache/escaped-marker').write_text('escaped')",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
"""
    (tests / "test_sandbox.py").write_text(test_source, encoding="utf-8")
    monkeypatch.setenv("LOCAL_AGENT_VERIFIER_SECRET", "HOST_ONLY_SENTINEL")
    artifacts = ArtifactStore("verification-docker-live", root=tmp_path / "task-state")
    runner = VerificationRunner(artifact_store=artifacts)
    command = VerificationCommandV1(
        argv=[sys.executable, "-m", "pytest", "-q", "tests/test_sandbox.py"],
        purpose="Exercise the isolated verifier runtime.",
        timeout_seconds=60,
    )

    result = runner.run(command, command_id="sandbox-security", cwd=repository)

    assert result.status is CommandStatus.PASSED
    assert result.exit_code == 0
    time.sleep(2)
    runtime = artifacts.task_root / "runtime" / "verification" / "sandbox-security"
    assert not (runtime / "cache" / "escaped-marker").exists()
    inventory = subprocess.run(
        [
            docker,
            "ps",
            "--all",
            "--filter",
            "label=local-agent.component=coding-verification",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inventory.returncode == 0
    assert inventory.stdout.strip() == ""


def test_live_git_diff_check_is_evaluated_in_the_same_bounded_sandbox(tmp_path: Path):
    docker = shutil.which("docker.exe") or shutil.which("docker")
    git = shutil.which("git.exe") or shutil.which("git")
    if not docker or not git:
        pytest.skip("Docker or Git is unavailable")
    available = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    repository = tmp_path / "owned-worktree"
    repository.mkdir()

    def run_git(*arguments: str) -> None:
        subprocess.run(
            [git, *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=30,
        )

    run_git("init")
    run_git("config", "user.name", "Coding Fixture")
    run_git("config", "user.email", "fixture@example.invalid")
    target = repository / "sample.txt"
    target.write_text("base\n", encoding="utf-8")
    run_git("add", "sample.txt")
    run_git("commit", "-m", "fixture")
    command = VerificationCommandV1(
        argv=["git", "diff", "--check"],
        purpose="Reject whitespace errors.",
        timeout_seconds=30,
    )

    target.write_text("good\n", encoding="utf-8")
    passing_store = ArtifactStore("git-diff-pass", root=tmp_path / "task-state")
    passing = VerificationRunner(artifact_store=passing_store).run(
        command, command_id="git-check", cwd=repository
    )
    assert passing.status is CommandStatus.PASSED

    target.write_bytes(b"bad trailing whitespace \n")
    failing_store = ArtifactStore("git-diff-fail", root=tmp_path / "task-state")
    failing = VerificationRunner(artifact_store=failing_store).run(
        command, command_id="git-check", cwd=repository
    )
    assert failing.status is CommandStatus.FAILED
    evidence = failing_store.reference(failing.output_artifact_id or "")
    assert "trailing whitespace" in Path(evidence.path).read_text(encoding="utf-8")


def test_live_node_verifier_uses_pinned_networkless_runtime(tmp_path: Path):
    docker = shutil.which("docker.exe") or shutil.which("docker")
    node = shutil.which("node.exe") or shutil.which("node")
    if not docker or not node:
        pytest.skip("Docker or Node is unavailable")
    available = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    repository = tmp_path / "owned-worktree"
    tests = repository / "tests"
    tests.mkdir(parents=True)
    (tests / "smoke.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import fs from 'node:fs';\n"
        "assert.equal(process.env.HOME, '/home/local-agent');\n"
        "assert.equal(process.env.USERPROFILE, '/home/local-agent');\n"
        "assert.equal(fs.existsSync('/var/run/docker.sock'), false);\n"
        "console.log('NODE_SANDBOX_OK');\n",
        encoding="utf-8",
    )
    artifacts = ArtifactStore("node-verifier-live", root=tmp_path / "task-state")
    result = VerificationRunner(artifact_store=artifacts).run(
        VerificationCommandV1(
            argv=["node", "tests/smoke.mjs"],
            purpose="Run the dependency-free Node sandbox fixture.",
            timeout_seconds=30,
        ),
        command_id="node-check",
        cwd=repository,
    )

    assert result.status is CommandStatus.PASSED
    evidence = artifacts.reference(result.output_artifact_id or "")
    assert "NODE_SANDBOX_OK" in Path(evidence.path).read_text(encoding="utf-8")


def _approved_deterministic_review() -> ReviewResultV1:
    return ReviewResultV1(
        reviewer_id="deterministic-review",
        reviewer=ExecutorKind.DETERMINISTIC,
        verdict=ReviewVerdict.APPROVED,
        findings=[],
        checked_requirements=True,
        checked_tests=True,
        checked_diff_scope=True,
        checked_secrets=True,
        checked_constitution=True,
        summary="Deterministic gates passed.",
        reviewed_at=datetime.now(timezone.utc),
    )


def test_standard_codex_finding_is_merged_without_weakening_deterministic_gates():
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "- [P2] ambiguous operation name — src/calculator.py:4\n"
            "Adding another operation makes this name ambiguous at runtime."
        ),
        worktree_unchanged=True,
    )

    assert merged.reviewer is ExecutorKind.CODEX_REVIEW
    assert merged.verdict is ReviewVerdict.REJECTED
    assert [item.code for item in merged.findings] == [
        "codex.p2.ambiguous.operation.name"
    ]
    assert merged.findings[0].severity is ReviewSeverity.MEDIUM
    assert merged.findings[0].file == "src/calculator.py"
    assert merged.checked_constitution is True

    inline = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "- [P1] literal argument required — src/security_runner.py:10 — "
            "An attacker-controlled name executes a second shell command."
        ),
        worktree_unchanged=True,
    )
    assert inline.verdict is ReviewVerdict.REJECTED
    assert inline.findings[0].file == "src/security_runner.py"
    assert inline.findings[0].line == 10
    assert "attacker-controlled" in inline.findings[0].failure_scenario

    backticked = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "[P1] literal argument required — `src/security_runner.py:10-11` — "
            "A shell metacharacter executes a second command."
        ),
        worktree_unchanged=True,
    )
    assert backticked.verdict is ReviewVerdict.REJECTED
    assert backticked.findings[0].file == "src/security_runner.py"

    colon_scenario = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "- [P1] literal argument required — src/security_runner.py:10: "
            "A shell metacharacter executes a second command."
        ),
        worktree_unchanged=True,
    )
    assert colon_scenario.verdict is ReviewVerdict.REJECTED
    assert colon_scenario.findings[0].line == 10

    markdown_variant = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "Review comment:\n[P1]: Avoid a shell sink "
            "([src/security_runner.py:10](src/security_runner.py#L10)); "
            "attacker input executes another command."
        ),
        worktree_unchanged=True,
    )
    assert markdown_variant.verdict is ReviewVerdict.REJECTED
    assert markdown_variant.findings[0].file == "src/security_runner.py"
    assert markdown_variant.findings[0].line == 10


@pytest.mark.parametrize(
    ("summary", "unchanged", "expected_code"),
    [
        ("not protocol", True, "codex.review_unstructured"),
        (
            "NO_FINDINGS",
            False,
            "codex.review_mutated_worktree",
        ),
        (
            "- [P1] runtime failure — src/runtime.py:7\n"
            "A concrete input reaches the failing runtime branch.",
            True,
            "codex.p1.runtime.failure",
        ),
    ],
)
def test_codex_review_fails_closed_on_unstructured_rejected_or_mutating_output(
    summary: str,
    unchanged: bool,
    expected_code: str,
):
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=summary,
        worktree_unchanged=unchanged,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert expected_code in {item.code for item in merged.findings}


def test_codex_approval_cannot_raise_a_rejected_deterministic_verdict():
    deterministic = _approved_deterministic_review().model_copy(
        update={"verdict": ReviewVerdict.REJECTED, "summary": "Local gate rejected."}
    )
    merged = merge_codex_review(
        ReviewResultV1.model_validate(deterministic.model_dump()),
        codex_summary="NO_FINDINGS",
        worktree_unchanged=True,
    )

    assert merged.verdict is ReviewVerdict.REJECTED


def test_codex_review_accepts_only_the_exact_no_findings_sentinel():
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary="NO_FINDINGS",
        worktree_unchanged=True,
    )

    assert merged.verdict is ReviewVerdict.APPROVED
    assert merged.findings == []

    prose = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=(
            "The change removes shell interpretation and passes the value as a literal "
            "argument, addressing the command-injection flaw without changing the interface."
        ),
        worktree_unchanged=True,
    )
    assert prose.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in prose.findings} == {"codex.review_unstructured"}


def _codex_review_json(
    *,
    findings: list[dict[str, object]] | None = None,
    correctness: str | None = None,
) -> str:
    values = findings or []
    return json.dumps(
        {
            "findings": values,
            "overall_correctness": correctness
            or ("patch is incorrect" if values else "patch is correct"),
            "overall_explanation": (
                "The patch has actionable findings."
                if values
                else "The patch satisfies the bounded task."
            ),
            "overall_confidence_score": 0.98,
        }
    )


def _codex_json_finding(path: Path) -> dict[str, object]:
    return {
        "title": "[P1] Keep report names as literal arguments",
        "body": "A shell metacharacter in the report name would execute another command.",
        "confidence_score": 0.99,
        "priority": 1,
        "code_location": {
            "absolute_file_path": str(path.resolve()),
            "line_range": {"start": 10, "end": 10},
        },
    }


def test_codex_review_accepts_strict_builtin_json_approval(tmp_path: Path):
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=_codex_review_json(),
        worktree_unchanged=True,
        repository=tmp_path,
    )

    assert merged.verdict is ReviewVerdict.APPROVED
    assert merged.findings == []
    assert merged.checked_requirements is True


def test_codex_review_maps_strict_builtin_json_finding(tmp_path: Path):
    target = tmp_path / "src" / "security_runner.py"
    target.parent.mkdir()
    target.write_text("pass\n", encoding="utf-8")

    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=_codex_review_json(findings=[_codex_json_finding(target)]),
        worktree_unchanged=True,
        repository=tmp_path,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert [item.code for item in merged.findings] == [
        "codex.p1.keep.report.names.as.literal.arguments"
    ]
    assert merged.findings[0].file == "src/security_runner.py"
    assert merged.findings[0].line == 10
    assert "metacharacter" in merged.findings[0].failure_scenario


def test_codex_json_review_rejects_duplicate_keys_and_non_finite_numbers(
    tmp_path: Path,
):
    duplicate = (
        '{"findings":[],"findings":[],"overall_correctness":"patch is correct",'
        '"overall_explanation":"ok","overall_confidence_score":0.9}'
    )
    non_finite = (
        '{"findings":[],"overall_correctness":"patch is correct",'
        '"overall_explanation":"ok","overall_confidence_score":NaN}'
    )

    for summary in (duplicate, non_finite):
        merged = merge_codex_review(
            _approved_deterministic_review(),
            codex_summary=summary,
            worktree_unchanged=True,
            repository=tmp_path,
        )
        assert merged.verdict is ReviewVerdict.REJECTED
        assert {item.code for item in merged.findings} == {"codex.review_unstructured"}


@pytest.mark.parametrize(
    "mutation",
    [
        "boolean_confidence",
        "overflow_confidence",
        "boolean_priority",
        "priority_mismatch",
        "relative_path",
        "escaping_path",
        "reversed_range",
        "boolean_line",
        "wide_range",
        "extra_field",
        "surrogate_path",
        "verdict_inconsistent",
    ],
)
def test_codex_json_review_fails_closed_on_schema_and_boundary_attacks(
    tmp_path: Path,
    mutation: str,
):
    target = tmp_path / "src" / "security_runner.py"
    target.parent.mkdir()
    target.write_text("pass\n", encoding="utf-8")
    envelope = json.loads(_codex_review_json(findings=[_codex_json_finding(target)]))
    finding = envelope["findings"][0]

    if mutation == "boolean_confidence":
        envelope["overall_confidence_score"] = True
    elif mutation == "overflow_confidence":
        envelope["overall_confidence_score"] = 10**400
    elif mutation == "boolean_priority":
        finding["priority"] = True
    elif mutation == "priority_mismatch":
        finding["priority"] = 2
    elif mutation == "relative_path":
        finding["code_location"]["absolute_file_path"] = "src/security_runner.py"  # type: ignore[index]
    elif mutation == "escaping_path":
        finding["code_location"]["absolute_file_path"] = str(  # type: ignore[index]
            (tmp_path.parent / "outside.py").resolve()
        )
    elif mutation == "reversed_range":
        finding["code_location"]["line_range"] = {"start": 10, "end": 9}  # type: ignore[index]
    elif mutation == "boolean_line":
        finding["code_location"]["line_range"] = {"start": True, "end": 1}  # type: ignore[index]
    elif mutation == "wide_range":
        finding["code_location"]["line_range"] = {"start": 1, "end": 11}  # type: ignore[index]
    elif mutation == "extra_field":
        finding["unexpected"] = "not allowed"
    elif mutation == "surrogate_path":
        finding["code_location"]["absolute_file_path"] = "C:/fixture/\ud800.py"
    elif mutation == "verdict_inconsistent":
        envelope["overall_correctness"] = "patch is correct"
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=json.dumps(envelope),
        worktree_unchanged=True,
        repository=tmp_path,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in merged.findings} == {"codex.review_unstructured"}


def test_codex_json_review_rejects_mixed_prose(tmp_path: Path):
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=_codex_review_json() + "\nNO_FINDINGS",
        worktree_unchanged=True,
        repository=tmp_path,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in merged.findings} == {"codex.review_unstructured"}


def test_standard_codex_negative_review_prose_stays_fail_closed():
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary="The change does not address the command-injection flaw.",
        worktree_unchanged=True,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in merged.findings} == {"codex.review_unstructured"}

    introduced = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary="The change introduces a path traversal vulnerability.",
        worktree_unchanged=True,
    )
    assert introduced.verdict is ReviewVerdict.REJECTED

    contains = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary="The code contains a critical command-injection vulnerability.",
        worktree_unchanged=True,
    )
    assert contains.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in contains.findings} == {"codex.review_unstructured"}


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.py", "C:/Windows/System32/drivers/etc/hosts", "/etc/passwd"],
)
def test_codex_finding_paths_cannot_escape_reviewed_repository(
    tmp_path: Path,
    unsafe_path: str,
):
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=f"- [P1] escaped evidence — {unsafe_path}:9\nOutside scope.",
        worktree_unchanged=True,
        repository=tmp_path,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in merged.findings} == {"codex.review_unstructured"}


def test_codex_reviewer_identity_binds_deterministic_evidence():
    first = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary="NO_FINDINGS",
        worktree_unchanged=True,
    )
    other_gate = _approved_deterministic_review().model_copy(
        update={"reviewer_id": "deterministic-other-diff"}
    )
    second = merge_codex_review(
        ReviewResultV1.model_validate(other_gate.model_dump()),
        codex_summary="NO_FINDINGS",
        worktree_unchanged=True,
    )

    assert first.reviewer_id != second.reviewer_id


@pytest.mark.parametrize(
    "summary",
    [
        "The patch correctly allows arbitrary command execution.",
        "The code safely logs plaintext passwords.",
        "The implementation correctly disables authentication.",
        "The fix preserves the path traversal flaw.",
        "The patch passes tests but is vulnerable to SQL injection.",
        "No findings because I could not inspect the diff.",
        "The change addresses formatting while leaking credentials.",
        "NO_FINDINGS.",
        "no_findings",
    ],
)
def test_codex_review_never_infers_approval_from_free_prose(summary: str):
    merged = merge_codex_review(
        _approved_deterministic_review(),
        codex_summary=summary,
        worktree_unchanged=True,
    )

    assert merged.verdict is ReviewVerdict.REJECTED
    assert {item.code for item in merged.findings} == {"codex.review_unstructured"}


def _delivery_request(*, codex_required: bool) -> CodingTaskRequestV1:
    return CodingTaskRequestV1(
        task_id="delivery-task",
        request_id="delivery-request",
        goal="Review the supplied implementation.",
        repository_path="C:/synthetic/repository",
        mode=CodingMode.READ_ONLY,
        risk=CodingRisk.HIGH if codex_required else CodingRisk.MEDIUM,
        constraints=["Use only authenticated evidence."],
        acceptance_criteria=["Every finding is evidence-backed."],
        verification_plan=["Perform independent review."],
        permissions=CodingPermissionsV1(
            cloud_execution=codex_required,
            data_classification=(
                DataClassification.PUBLIC
                if codex_required
                else DataClassification.INTERNAL
            ),
        ),
    )


def _approved_delivery_review(reviewer: ExecutorKind) -> ReviewResultV1:
    semantic = reviewer is ExecutorKind.LOCAL_SEMANTIC_REVIEW
    return ReviewResultV1(
        reviewer_id="delivery-review",
        reviewer=reviewer,
        verdict=ReviewVerdict.APPROVED,
        findings=[],
        checked_requirements=True,
        checked_tests=True,
        checked_diff_scope=True,
        checked_secrets=True,
        checked_constitution=True,
        subject_sha256="1" * 64 if semantic else None,
        evidence_artifact_id="semantic-artifact" if semantic else None,
        evidence_artifact_sha256="2" * 64 if semantic else None,
        summary="All independent gates passed.",
        reviewed_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "missing_gate",
    [
        "checked_requirements",
        "checked_tests",
        "checked_diff_scope",
        "checked_secrets",
        "checked_constitution",
    ],
)
def test_approved_delivery_requires_every_review_gate(missing_gate: str):
    request = _delivery_request(codex_required=False)
    review = _approved_delivery_review(ExecutorKind.LOCAL_SEMANTIC_REVIEW).model_copy(
        update={missing_gate: False}
    )
    assert not is_successful_review_delivery(request, review)


def test_local_semantic_review_cannot_satisfy_high_risk_codex_delivery():
    request = _delivery_request(codex_required=True)
    local = _approved_delivery_review(ExecutorKind.LOCAL_SEMANTIC_REVIEW)
    codex = _approved_delivery_review(ExecutorKind.CODEX_REVIEW)

    assert not is_successful_review_delivery(request, local)
    assert is_successful_review_delivery(request, codex)
