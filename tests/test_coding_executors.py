from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.coding import executors as coding_executors
from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, load_coding_policy
from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CommandStatus,
    DataClassification,
    ExecutorKind,
)
from services.coding.executors import (
    CodexExecutor,
    ExecutorFailure,
    ExecutorPolicyError,
    QwenExecutor,
    _event_evidence,
    _qwen_sandbox_proxy_source,
)
from services.coding.process import (
    ProcessOutcome,
    ProcessRunner,
    safe_child_environment,
)
from services.coding.public_preflight import (
    PublicDataPreflightError,
    PublicDataSnapshot,
    build_public_data_snapshot as real_build_public_data_snapshot,
)
from tests.coding_fixtures import coding_fixture, file_snapshot


def _request(repository: Path, *, mode: CodingMode) -> CodingTaskRequestV1:
    marker = repository / ".git"
    if not marker.exists():
        marker.write_text("gitdir: synthetic-unit-fixture\n", encoding="utf-8")
    return CodingTaskRequestV1(
        task_id=f"codex-{mode.value}",
        request_id=f"codex-{mode.value}-request",
        goal="Perform the bounded synthetic public-fixture task.",
        repository_path=str(repository),
        mode=mode,
        risk=CodingRisk.HIGH,
        constraints=["Do not commit or push."],
        acceptance_criteria=["The requested bounded result is complete."],
        verification_plan=["Review the resulting evidence."],
        permissions=CodingPermissionsV1(
            modify_files=(mode is CodingMode.WRITE),
            cloud_execution=True,
            data_classification=DataClassification.PUBLIC,
        ),
    )


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GIT_CONFIG_OVERLAY = {
    "core.fsmonitor": "false",
    "diff.external": "",
    "interactive.diffFilter": "",
    "credential.helper": "",
    "commit.gpgSign": "false",
    "tag.gpgSign": "false",
    "protocol.allow": "never",
    "protocol.file.allow": "never",
    "protocol.ext.allow": "never",
    "gc.auto": "0",
}
EXPECTED_CODEX_DISABLED = {
    "hooks",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "apps",
    "enable_mcp_apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "multi_agent",
    "multi_agent_v2",
    "enable_fanout",
    "in_app_browser",
    "standalone_web_search",
    "web_search_cached",
    "web_search_request",
    "auth_elicitation",
    "network_proxy",
    "plugin_sharing",
    "tool_call_mcp_elicitation",
    "memories",
    "code_mode_host",
    "workspace_dependencies",
    "request_permissions_tool",
    "tool_suggest",
}


def _public_snapshot(marker: str) -> PublicDataSnapshot:
    digest = sha256(marker.encode("utf-8")).hexdigest()
    return PublicDataSnapshot(
        head_sha="0" * 40,
        tracked_manifest_sha256=digest,
        changed_manifest_sha256=digest,
        git_object_manifest_sha256=digest,
        git_metadata_manifest_sha256=digest,
        tracked_files=1,
        tracked_bytes=1,
        git_objects=1,
        git_object_bytes=1,
        git_metadata_bytes=1,
        knowledge_blocked_files=0,
    )


def _docker_environment(argv: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, item in enumerate(argv[:-1]):
        if item != "--env":
            continue
        key, separator, value = argv[index + 1].partition("=")
        assert separator == "="
        values[key] = value
    return values


def _git_config_overlay(environment: dict[str, str]) -> dict[str, str]:
    count = int(environment["GIT_CONFIG_COUNT"])
    assert {key for key in environment if key.startswith("GIT_CONFIG_KEY_")} == {
        f"GIT_CONFIG_KEY_{index}" for index in range(count)
    }
    assert {key for key in environment if key.startswith("GIT_CONFIG_VALUE_")} == {
        f"GIT_CONFIG_VALUE_{index}" for index in range(count)
    }
    return {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
    }


@pytest.fixture(autouse=True)
def _executor_test_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.coding.resources._disk_usage",
        lambda path: SimpleNamespace(free=2**60),
    )
    marker = tmp_path / ".git"
    marker_payload = b"gitdir: synthetic-unit-fixture\n"
    marker.write_bytes(marker_payload)
    common_dir = tmp_path / "synthetic-source" / ".git"
    git_dir = common_dir / "worktrees" / "unit-worktree"
    git_dir.mkdir(parents=True)
    identity = coding_executors._QwenGitIdentity(
        repository=tmp_path.resolve(),
        marker_sha256=sha256(marker_payload).hexdigest(),
        git_dir=git_dir.resolve(),
        common_dir=common_dir.resolve(),
        git_dir_file_id=(0, 1),
        common_dir_file_id=(0, 2),
        symbolic_head="refs/heads/local-agent/unit",
        head_commit="0" * 40,
    )
    original_validator = coding_executors._validated_qwen_git_identity

    def synthetic_validator(repository: Path, *, expected=None):
        assert repository.resolve() == identity.repository
        if expected is not None and expected != identity:
            raise ExecutorPolicyError("synthetic Qwen Git identity changed")
        return identity

    monkeypatch.setattr(
        coding_executors,
        "_validated_qwen_git_identity",
        synthetic_validator,
    )

    stable_public_snapshot = _public_snapshot("stable-public-fixture")

    def synthetic_public_snapshot(repository: Path, *, knowledge_blocked_files: int):
        assert repository.resolve() == tmp_path.resolve()
        assert knowledge_blocked_files == 0
        return stable_public_snapshot

    monkeypatch.setattr(
        coding_executors,
        "build_public_data_snapshot",
        synthetic_public_snapshot,
    )
    return original_validator


class _CodexProcess:
    def __init__(
        self, *, review: bool = False, message: str | bytes | None = None
    ) -> None:
        self.review = review
        self.message = message
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[1:4] == ["mcp", "list", "--json"]:
            output = json.dumps([{"name": "browser"}, {"name": "context7"}])
            return ProcessOutcome(CommandStatus.PASSED, 0, output, "", 2)
        output_index = argv.index("--output-last-message") + 1
        output_file = Path(argv[output_index])
        output_file.parent.mkdir(parents=True, exist_ok=True)
        message = self.message or (
            json.dumps(
                {
                    "findings": [],
                    "overall_correctness": "patch is correct",
                    "overall_explanation": "The bounded fixture patch is correct.",
                    "overall_confidence_score": 0.99,
                }
            )
            if self.review
            else "Writable Codex execution completed."
        )
        if isinstance(message, bytes):
            output_file.write_bytes(message)
        else:
            output_file.write_text(message, encoding="utf-8")
        events = json.dumps(
            {"type": "thread.started", "thread_id": "codex-fixture-session"}
        )
        return ProcessOutcome(CommandStatus.PASSED, 0, events, "", 7)


def _is_timed_container_payload(argv: list[str], executable: str) -> bool:
    if "--entrypoint" not in argv:
        return False
    entrypoint = argv.index("--entrypoint")
    return (
        entrypoint + 2 < len(argv)
        and argv[entrypoint + 1] == "timeout"
        and executable in argv[entrypoint + 3 :]
    )


class _QwenProcess:
    def __init__(
        self,
        *,
        main_status: CommandStatus = CommandStatus.PASSED,
        raise_main: bool = False,
        leftover_inventory: bool = False,
        main_message: str = "Sandboxed Qwen execution completed.",
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.main_status = main_status
        self.raise_main = raise_main
        self.leftover_inventory = leftover_inventory
        self.main_message = main_message

    def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
        self.calls.append((list(argv), dict(kwargs)))
        if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "curl":
            denied = (
                "--noproxy" in argv
                or "http://ollama-proxy:8877/http://example.com/" in argv
                or "http://ollama-proxy:8877/api/tags" in argv
            )
            return ProcessOutcome(
                CommandStatus.FAILED if denied else CommandStatus.PASSED,
                22 if denied else 0,
                "" if denied else '{"version":"fixture"}',
                "denied" if denied else "",
                3,
            )
        if argv[1:3] == ["ps", "--all"] or argv[1:3] == ["network", "ls"]:
            return ProcessOutcome(
                CommandStatus.PASSED,
                0,
                "leftover-fixture\n" if self.leftover_inventory else "",
                "",
                1,
            )
        if not _is_timed_container_payload(argv, "qwen"):
            return ProcessOutcome(CommandStatus.PASSED, 0, "", "", 1)
        if self.raise_main:
            raise RuntimeError("synthetic docker client crash")
        if self.main_status is not CommandStatus.PASSED:
            return ProcessOutcome(
                self.main_status,
                1 if self.main_status is CommandStatus.FAILED else None,
                "",
                "synthetic main failure",
                9,
            )
        events = json.dumps(
            {
                "type": "message",
                "session_id": "qwen-fixture-session",
                "message": self.main_message,
            }
        )
        return ProcessOutcome(CommandStatus.PASSED, 0, events, "", 9)


class _RunawayQwenProcess(_QwenProcess):
    def __init__(self, target: Path, *, payload_bytes: int = 0) -> None:
        super().__init__()
        self.target = target
        self.payload_bytes = payload_bytes
        self.resource_cancelled = False

    def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
        if _is_timed_container_payload(argv, "qwen"):
            self.calls.append((list(argv), dict(kwargs)))
            if self.payload_bytes:
                self.target.write_bytes(b"X" * self.payload_bytes)
            cancellation = kwargs.get("cancel_event")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancellation is not None and cancellation.is_set():  # type: ignore[union-attr]
                    self.resource_cancelled = True
                    return ProcessOutcome(CommandStatus.CANCELLED, None, "", "", 1)
                time.sleep(0.01)
            raise AssertionError(
                "resource watchdog did not cancel the synthetic container"
            )
        return super().run(argv, **kwargs)


def test_container_workspace_event_paths_map_only_inside_owned_repository(
    tmp_path: Path,
):
    source = tmp_path / "src"
    source.mkdir()
    inside = source / "inside.py"
    inside.write_text("fixture\n", encoding="utf-8")
    events = [
        {"path": "/workspace/src/inside.py"},
        {"path": "/workspace"},
        {"path": "/workspace/../outside.py"},
        {"path": "/workspace/src/missing.py"},
    ]

    _, files, _, _, _ = _event_evidence(events, tmp_path)

    assert files == ("src/inside.py",)


def test_qwen_writable_execution_forces_task_scoped_official_docker_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SANDBOX_FLAGS", "--privileged")
    monkeypatch.setenv("SANDBOX_MOUNTS", "C:/secrets:/secrets")
    monkeypatch.setenv("QWEN_SANDBOX", "false")
    process = _QwenProcess()
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    artifact_root = tmp_path / "artifacts"
    executor = QwenExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="qwen.cmd",
        model="fixture-model",
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    result = executor.execute(
        request=CodingTaskRequestV1.model_validate(request.model_dump()),
        repository=tmp_path,
        prompt="Make the bounded fixture edit.",
        context_json="{}",
        artifact_store=ArtifactStore("qwen-sandbox", root=artifact_root),
    )

    assert result.executor is ExecutorKind.LOCAL_QWEN
    main_calls = [
        item for item in process.calls if _is_timed_container_payload(item[0], "qwen")
    ]
    assert len(main_calls) == 1
    command, call = main_calls[0]
    assert command[0] == str(fake_docker)
    assert command[1:4] == ["run", "--interactive", "--rm"]
    assert "--read-only" in command
    assert "1000:1000" in command
    assert "--cap-drop" in command and "ALL" in command
    assert "no-new-privileges:true" in command
    assert command[command.index("--entrypoint") + 1] == "timeout"
    image_index = command.index("--entrypoint") + 2
    assert command[image_index] == (
        "ghcr.io/qwenlm/qwen-code:0.19.10@"
        "sha256:03456a270da8d1bf1f1d5e6bf5e340718b595355b68649e0f6940cb7ff8dbeda"
    )
    assert command[image_index + 1 : image_index + 8] == [
        "--signal=TERM",
        "--kill-after=10s",
        f"{executor.policy.qwen_timeout_seconds + 30}s",
        "qwen",
        "--approval-mode",
        "yolo",
        "--model",
    ]
    proxy = next(argv for argv, _ in process.calls if argv[1:3] == ["run", "--detach"])
    proxy_image = proxy.index("--entrypoint") + 2
    assert proxy[proxy_image - 1] == "timeout"
    assert proxy[proxy_image] == command[image_index]
    assert proxy[proxy_image + 1 : proxy_image + 6] == [
        "--signal=TERM",
        "--kill-after=10s",
        f"{executor.policy.qwen_timeout_seconds + 120}s",
        "node",
        "-e",
    ]
    allowed_probe = next(
        argv
        for argv, _ in process.calls
        if "--entrypoint" in argv
        and argv[argv.index("--entrypoint") + 1] == "curl"
        and "http://ollama-proxy:8877/api/version" in argv
    )
    assert allowed_probe[allowed_probe.index("--connect-timeout") + 1] == "1"
    assert allowed_probe[allowed_probe.index("--max-time") + 1] == "10"
    assert allowed_probe[allowed_probe.index("--retry") + 1] == "10"
    assert "--retry-connrefused" in allowed_probe
    assert allowed_probe[allowed_probe.index("--retry-delay") + 1] == "0"
    git_probe = next(
        argv
        for argv, _ in process.calls
        if _is_timed_container_payload(argv, "sh")
        and "git rev-parse --verify HEAD" in argv[-1]
    )
    git_probe_script = git_probe[-1]
    assert "git status --porcelain" in git_probe_script
    assert "git diff --no-ext-diff --no-textconv" in git_probe_script
    assert "git diff --cached" in git_probe_script
    assert "git commit" not in git_probe_script
    assert "git push" not in git_probe_script
    assert git_probe[git_probe.index("--network") + 1] == "none"
    assert f"{tmp_path}:/workspace:rw" in command
    common_git = (tmp_path / "synthetic-source" / ".git").resolve()
    task_runtime = (
        artifact_root / "qwen-sandbox" / "runtime" / "qwen-sandbox"
    ).resolve()
    synthetic_marker = task_runtime / "workspace.git"
    assert f"{common_git}:/local-agent/repo-git:ro" in command
    assert f"{synthetic_marker}:/workspace/.git:ro" in command
    assert f"{tmp_path / '.git'}:/workspace/.git:ro" not in command
    assert f"{tmp_path}:/workspace:ro" in git_probe
    assert f"{common_git}:/local-agent/repo-git:ro" in git_probe
    assert f"{synthetic_marker}:/workspace/.git:ro" in git_probe
    volume_targets = {
        command[index + 1].rsplit(":", 1)[0].rsplit(":", 1)[-1]
        for index, item in enumerate(command[:-1])
        if item == "--volume"
    }
    assert volume_targets == {
        "/workspace",
        "/local-agent/repo-git",
        "/workspace/.git",
        "/local-agent/qwen-home",
        "/local-agent/qwen-runtime",
        "/home/local-agent",
        "/tmp",
        "/local-agent/git-guard",
    }
    assert not any("docker.sock" in item.casefold() for item in command)
    assert "HTTP_PROXY=" in command
    assert "HTTPS_PROXY=" in command
    assert "NO_PROXY=*" in command
    assert "OPENAI_BASE_URL=http://ollama-proxy:8877/v1" in command
    assert "GIT_OPTIONAL_LOCKS=0" in command
    assert "GIT_NO_REPLACE_OBJECTS=1" in command
    expected_container_git = {
        **EXPECTED_GIT_CONFIG_OVERLAY,
        "core.hooksPath": "/local-agent/git-guard",
        "safe.directory": "/workspace",
    }
    assert _git_config_overlay(_docker_environment(command)) == expected_container_git
    assert _git_config_overlay(_docker_environment(git_probe)) == expected_container_git
    git_guard = task_runtime.parent / "git-guard"
    assert f"{git_guard}:/local-agent/git-guard:ro" in command
    assert f"{git_guard}:/local-agent/git-guard:ro" in git_probe
    for argv, memory, swap, cpus in (
        (
            command,
            executor.policy.qwen_agent_memory_bytes,
            executor.policy.qwen_agent_memory_swap_bytes,
            executor.policy.qwen_agent_cpus,
        ),
        (
            proxy,
            executor.policy.qwen_proxy_memory_bytes,
            executor.policy.qwen_proxy_memory_swap_bytes,
            executor.policy.qwen_proxy_cpus,
        ),
        (
            allowed_probe,
            executor.policy.qwen_probe_memory_bytes,
            executor.policy.qwen_probe_memory_swap_bytes,
            executor.policy.qwen_probe_cpus,
        ),
    ):
        assert argv[argv.index("--memory") + 1] == str(memory)
        assert argv[argv.index("--memory-swap") + 1] == str(swap)
        assert argv[argv.index("--cpus") + 1] == str(cpus)
        assert "--pids-limit" in argv
        assert "--tmpfs" in argv or argv is command
    qwen_index = command.index("qwen")
    assert "--proxy" not in command[qwen_index:]
    environment = call["environment"]
    assert isinstance(environment, dict)
    assert not {
        "SANDBOX_FLAGS",
        "SANDBOX_MOUNTS",
        "SANDBOX_ENV",
        "SANDBOX_PORTS",
    }.intersection(environment)
    for key in ("USERPROFILE", "HOME", "TEMP", "TMP", "QWEN_HOME", "QWEN_RUNTIME_DIR"):
        assert Path(environment[key]).resolve().is_relative_to(task_runtime)
    settings = json.loads(
        (Path(environment["QWEN_HOME"]) / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["modelProviders"]["openai"][0]["baseUrl"] == (
        "http://ollama-proxy:8877/v1"
    )


def test_qwen_read_only_execution_mounts_owned_worktree_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = _QwenProcess()
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    request = _request(tmp_path, mode=CodingMode.READ_ONLY).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    QwenExecutor(
        process_runner=process,  # type: ignore[arg-type]
        model="fixture-model",
    ).execute(
        request=CodingTaskRequestV1.model_validate(request.model_dump()),
        repository=tmp_path,
        prompt="Inspect the bounded fixture.",
        context_json="{}",
        artifact_store=ArtifactStore("qwen-read-only", root=tmp_path / "artifacts"),
    )

    command = next(
        argv for argv, _ in process.calls if _is_timed_container_payload(argv, "qwen")
    )
    assert f"{tmp_path}:/workspace:ro" in command
    assert f"{tmp_path}:/workspace:rw" not in command
    assert not any("docker.sock" in item.casefold() for item in command)


def test_qwen_revalidates_git_identity_immediately_around_probe_and_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[str] = []
    base_validator = coding_executors._validated_qwen_git_identity

    def tracking_validator(repository: Path, *, expected=None):
        events.append("validate")
        return base_validator(repository, expected=expected)

    class OrderedProcess(_QwenProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            if _is_timed_container_payload(argv, "sh"):
                events.append("git-probe")
            elif _is_timed_container_payload(argv, "qwen"):
                events.append("agent")
            else:
                events.append("docker-control")
            return super().run(argv, **kwargs)

    monkeypatch.setattr(
        coding_executors,
        "_validated_qwen_git_identity",
        tracking_validator,
    )
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        coding_executors,
        "_trusted_docker",
        lambda repository: fake_docker,
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    QwenExecutor(
        process_runner=OrderedProcess(),  # type: ignore[arg-type]
        model="fixture-model",
    ).execute(
        request=CodingTaskRequestV1.model_validate(request.model_dump()),
        repository=tmp_path,
        prompt="Make the bounded fixture edit.",
        context_json="{}",
        artifact_store=ArtifactStore(
            "qwen-identity-order",
            root=tmp_path / "artifacts",
        ),
    )

    for boundary in ("git-probe", "agent"):
        index = events.index(boundary)
        assert events[index - 1] == "validate"
        assert events[index + 1] == "validate"


@pytest.mark.required_e2e
def test_live_qwen_git_contract_probe_reads_linked_worktree_and_keeps_metadata_ro(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _executor_test_boundary,
):
    original_validator = _executor_test_boundary
    monkeypatch.setattr(
        coding_executors,
        "_validated_qwen_git_identity",
        original_validator,
    )
    with coding_fixture(run_id="qwen-docker-git-probe") as fixture:
        linked = fixture.add_worktree("qwen-docker-git-probe")
        repository = linked.path
        policy = load_coding_policy(ROOT / "config" / "coding.json")
        artifact_store = ArtifactStore(
            "qwen-live-git-probe",
            root=tmp_path / "artifacts",
        )
        runtime = coding_executors._prepare_qwen_runtime(artifact_store)
        identity = original_validator(repository)
        marker = coding_executors._prepare_qwen_git_marker(runtime, identity)
        docker = coding_executors._trusted_docker(repository)
        common_before = file_snapshot(identity.common_dir)
        marker_before = (repository / ".git").read_bytes()
        head_before = fixture.git(
            ["rev-parse", "HEAD"],
            cwd=repository,
        ).stdout.strip()
        arguments = coding_executors._qwen_git_probe_arguments(
            policy=policy,
            repository=repository,
            runtime=runtime,
            identity=identity,
            git_guard=coding_executors._prepare_git_guard(artifact_store)[0],
            labels=[
                "--label",
                "local-agent.owner=coding-engine-test",
                "--label",
                "local-agent.run=qwen-live-git-probe",
            ],
        )

        outcome = ProcessRunner(policy).run(
            [str(docker), *arguments],
            cwd=repository,
            timeout_seconds=120,
            environment=runtime.host_environment,
        )

        assert outcome.status is CommandStatus.PASSED, outcome.stderr
        assert original_validator(repository, expected=identity) == identity
        assert marker.read_text(encoding="utf-8") == (
            f"gitdir: /local-agent/repo-git/worktrees/{identity.git_dir.name}\n"
        )
        assert (repository / ".git").read_bytes() == marker_before
        assert file_snapshot(identity.common_dir) == common_before
        assert (
            fixture.git(["rev-parse", "HEAD"], cwd=repository).stdout.strip()
            == head_before
        )
        assert fixture.git(["status", "--porcelain"], cwd=repository).stdout == ""
        fixture.assert_remote_unchanged()


def test_qwen_proxy_source_is_static_exact_target_allowlist():
    source = _qwen_sandbox_proxy_source()
    assert 'const HOST="host.docker.internal";' in source
    assert "const PORT=11434;" in source
    assert "const CLIENT_HOST='ollama-proxy:8877';" in source
    assert "server.listen(8877,'::')" in source
    assert "target.protocol!=='http:'" in source
    assert "host!==CLIENT_HOST" in source
    assert "server.on('connect'" in source
    assert "target.host!==CLIENT_HOST" in source
    assert "['GET',new Set(['/api/version'])]" in source
    assert "['POST',new Set(['/v1/chat/completions'])]" in source
    assert "target.search||!ALLOWED.get(req.method)?.has(target.pathname)" in source
    assert "net.connect" not in source
    assert "Connection Established" not in source
    assert "/api/delete" not in source
    assert "/api/pull" not in source
    assert "/api/create" not in source
    assert "process.env" not in source


@pytest.mark.parametrize(
    ("main_status", "raise_main", "expected_exception"),
    [
        (CommandStatus.CANCELLED, False, ExecutorFailure),
        (CommandStatus.TIMED_OUT, False, ExecutorFailure),
        (CommandStatus.FAILED, False, ExecutorFailure),
        (CommandStatus.PASSED, True, RuntimeError),
    ],
)
def test_qwen_docker_faults_always_remove_exact_task_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    main_status: CommandStatus,
    raise_main: bool,
    expected_exception: type[Exception],
):
    process = _QwenProcess(main_status=main_status, raise_main=raise_main)
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(expected_exception):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Make the bounded fixture edit.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "qwen-fault-cleanup", root=tmp_path / "artifacts"
            ),
        )

    main = next(
        argv for argv, _ in process.calls if _is_timed_container_payload(argv, "qwen")
    )
    agent_name = main[main.index("--name") + 1]
    proxy = next(argv for argv, _ in process.calls if argv[1:3] == ["run", "--detach"])
    proxy_name = proxy[proxy.index("--name") + 1]
    network = next(
        argv for argv, _ in process.calls if argv[1:3] == ["network", "create"]
    )[-1]
    commands = [argv for argv, _ in process.calls]
    assert [str(fake_docker), "rm", "--force", agent_name] in commands
    assert [str(fake_docker), "rm", "--force", proxy_name] in commands
    assert [str(fake_docker), "network", "rm", network] in commands
    assert any(argv[1:3] == ["ps", "--all"] for argv in commands)
    assert any(argv[1:3] == ["network", "ls"] for argv in commands)


def test_qwen_cleanup_inventory_failure_overrides_apparent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = _QwenProcess(leftover_inventory=True)
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(ExecutorPolicyError, match="could not be safely cleaned"):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Make the bounded fixture edit.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "qwen-leftover-cleanup", root=tmp_path / "artifacts"
            ),
        )


def test_qwen_writable_growth_watchdog_cancels_without_mutating_caller_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "runaway.bin"
    process = _RunawayQwenProcess(target, payload_bytes=2 * 1024 * 1024)
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    base_policy = load_coding_policy(ROOT / "config" / "coding.json")
    policy = CodingPolicy.model_validate(
        {
            **base_policy.model_dump(),
            "qwen_max_writable_bytes": 1024 * 1024,
            "host_free_space_reserve_bytes": 1024 * 1024,
            "writable_watchdog_poll_seconds": 0.05,
            "free_space_watchdog_poll_seconds": 0.05,
        }
    )
    caller_cancel = threading.Event()
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(ExecutorPolicyError, match="resource watchdog"):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            policy=policy,
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Create bounded output.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "qwen-growth-watchdog", root=tmp_path / "artifacts"
            ),
            cancel_event=caller_cancel,
        )

    assert process.resource_cancelled is True
    assert caller_cancel.is_set() is False
    commands = [argv for argv, _ in process.calls]
    assert sum(argv[1:3] == ["rm", "--force"] for argv in commands) == 2
    assert any(argv[1:3] == ["network", "rm"] for argv in commands)
    assert any(argv[1:3] == ["ps", "--all"] for argv in commands)
    assert any(argv[1:3] == ["network", "ls"] for argv in commands)


def test_qwen_writable_root_hardlink_is_rejected_before_agent_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    external = tmp_path.parent / f"{tmp_path.name}-external-hardlink.txt"
    external.write_text("outside ownership boundary\n", encoding="utf-8")
    linked = tmp_path / "linked-payload.txt"
    try:
        os.link(external, linked)
    except OSError as exc:
        pytest.skip(f"hardlink fixture unavailable: {exc}")

    process = _QwenProcess()
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(ExecutorPolicyError, match="resource watchdog"):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Do not cross the writable ownership boundary.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "qwen-hardlink-watchdog",
                root=tmp_path.parent / f"{tmp_path.name}-artifacts",
            ),
        )

    assert external.read_text(encoding="utf-8") == "outside ownership boundary\n"
    assert not any(
        _is_timed_container_payload(argv, "qwen") for argv, _ in process.calls
    )
    commands = [argv for argv, _ in process.calls]
    assert sum(argv[1:3] == ["rm", "--force"] for argv in commands) == 2
    assert any(argv[1:3] == ["network", "rm"] for argv in commands)


def test_qwen_free_space_watchdog_cancels_and_cleans_exact_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = _RunawayQwenProcess(tmp_path / "unused")
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    observations = 0

    def falling_free_space(path: Path):
        nonlocal observations
        observations += 1
        return SimpleNamespace(free=2**60 if observations == 1 else 0)

    monkeypatch.setattr("services.coding.resources._disk_usage", falling_free_space)
    base_policy = load_coding_policy(ROOT / "config" / "coding.json")
    policy = CodingPolicy.model_validate(
        {
            **base_policy.model_dump(),
            "host_free_space_reserve_bytes": 1024 * 1024,
            "free_space_watchdog_poll_seconds": 0.05,
        }
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(ExecutorPolicyError, match="resource watchdog"):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            policy=policy,
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Observe free space.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "qwen-free-watchdog", root=tmp_path / "artifacts"
            ),
        )

    assert process.resource_cancelled is True
    commands = [argv for argv, _ in process.calls]
    assert sum(argv[1:3] == ["rm", "--force"] for argv in commands) == 2
    assert any(argv[1:3] == ["network", "rm"] for argv in commands)


def test_qwen_api_error_event_fails_closed_after_sandbox_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = _QwenProcess(main_message="[API Error: Connection error]")
    fake_docker = tmp_path / ("docker.exe" if os.name == "nt" else "docker")
    fake_docker.write_bytes(b"fixture")
    monkeypatch.setattr(
        "services.coding.executors._trusted_docker", lambda repository: fake_docker
    )
    request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={"risk": CodingRisk.LOW}
    )

    with pytest.raises(ExecutorFailure, match="model API error"):
        QwenExecutor(
            process_runner=process,  # type: ignore[arg-type]
            model="fixture-model",
        ).execute(
            request=CodingTaskRequestV1.model_validate(request.model_dump()),
            repository=tmp_path,
            prompt="Make the bounded fixture edit.",
            context_json="{}",
            artifact_store=ArtifactStore("qwen-api-error", root=tmp_path / "artifacts"),
        )

    commands = [argv for argv, _ in process.calls]
    assert sum(argv[1:3] == ["rm", "--force"] for argv in commands) == 2
    assert any(argv[1:3] == ["network", "rm"] for argv in commands)
    assert any(argv[1:3] == ["ps", "--all"] for argv in commands)
    assert any(argv[1:3] == ["network", "ls"] for argv in commands)


def test_writable_codex_keeps_windows_sandbox_config_but_disables_extensions(
    tmp_path: Path,
):
    process = _CodexProcess()
    artifacts = ArtifactStore("codex-write", root=tmp_path / "artifacts")
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
        reasoning_effort="high",
    )

    result = executor.execute(
        request=_request(tmp_path, mode=CodingMode.WRITE),
        repository=tmp_path,
        prompt="Make the synthetic fix.",
        context_json="{}",
        artifact_store=artifacts,
    )

    assert result.executor is ExecutorKind.CODEX_EXEC
    assert len(process.calls) == 2
    command, call = process.calls[1]
    assert command[:9] == [
        "codex.cmd",
        "-a",
        "never",
        "-s",
        "workspace-write",
        "exec",
        "--ephemeral",
        "--strict-config",
        "-C",
    ]
    assert command.count("--ephemeral") == 1
    assert "shell_environment_policy.inherit=all" in command
    assert "--enable" not in command
    assert "--ignore-user-config" not in command
    disabled = {
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--disable"
    }
    assert disabled == EXPECTED_CODEX_DISABLED
    assert command.count("--disable") == len(EXPECTED_CODEX_DISABLED)
    assert "mcp_servers.browser.enabled=false" in command
    assert "mcp_servers.context7.enabled=false" in command
    assert "skills.bundled.enabled=false" in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "sandbox_workspace_write.writable_roots=[]" in command
    developer_override = next(
        item for item in command if item.startswith("developer_instructions=")
    )
    assert "untrusted data" in developer_override
    assert "owned worktree" in developer_override
    assert "smallest diff" in developer_override
    assert "Never commit, push" in developer_override
    assert "access the network" in developer_override
    assert "MCP, apps" in developer_override
    environment = call["environment"]
    assert isinstance(environment, dict)
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    codex_git_overlay = _git_config_overlay(environment)
    hooks_path = codex_git_overlay.pop("core.hooksPath")
    assert Path(hooks_path).resolve().is_relative_to(artifacts.task_root.resolve())
    assert codex_git_overlay == EXPECTED_GIT_CONFIG_OVERLAY


def test_codex_reclassifies_after_writable_isolation_before_main_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class IsolationMutationProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            if argv[1:4] == ["mcp", "list", "--json"]:
                (tmp_path / ".env.cloud-secret").write_text(
                    "API_TOKEN=unsafe\n",
                    encoding="utf-8",
                )
                return super().run(argv, **kwargs)
            raise AssertionError(
                "Codex main runner must not receive unsafe repository data"
            )

    classifications = 0

    def classify(repository: Path, *, knowledge_blocked_files: int):
        nonlocal classifications
        classifications += 1
        assert repository == tmp_path
        assert knowledge_blocked_files == 0
        if (repository / ".env.cloud-secret").exists():
            raise PublicDataPreflightError("synthetic private mutation")
        return _public_snapshot("safe")

    monkeypatch.setattr(coding_executors, "build_public_data_snapshot", classify)
    process = IsolationMutationProcess()
    artifacts = ArtifactStore("codex-isolation-race", root=tmp_path / "artifacts")

    with pytest.raises(ExecutorPolicyError, match="before cloud execution"):
        CodexExecutor(
            process_runner=process,  # type: ignore[arg-type]
            executable="codex.cmd",
            model="fixture-model",
        ).execute(
            request=_request(tmp_path, mode=CodingMode.WRITE),
            repository=tmp_path,
            prompt="Make the bounded synthetic fix.",
            context_json="{}",
            artifact_store=artifacts,
        )

    assert classifications == 1
    assert len(process.calls) == 1
    assert process.calls[0][0][1:4] == ["mcp", "list", "--json"]
    assert list(artifacts.artifact_root.iterdir()) == []


@pytest.mark.parametrize(
    ("request_mode", "review_only"),
    [
        (CodingMode.READ_ONLY, False),
        (CodingMode.WRITE, True),
    ],
)
def test_codex_read_only_and_review_reject_snapshot_mutation_before_output_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_mode: CodingMode,
    review_only: bool,
):
    public_file = tmp_path / "public.txt"
    public_file.write_text("before\n", encoding="utf-8")

    class ReadOnlyMutationProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            outcome = super().run(argv, **kwargs)
            public_file.write_text("after\n", encoding="utf-8")
            return outcome

    def classify(repository: Path, *, knowledge_blocked_files: int):
        assert repository == tmp_path
        assert knowledge_blocked_files == 0
        return _public_snapshot(public_file.read_text(encoding="utf-8"))

    monkeypatch.setattr(coding_executors, "build_public_data_snapshot", classify)
    process = ReadOnlyMutationProcess(review=review_only)
    artifacts = ArtifactStore(
        f"codex-read-only-race-{str(review_only).casefold()}",
        root=tmp_path / "artifacts",
    )

    with pytest.raises(ExecutorPolicyError, match="snapshot changed"):
        CodexExecutor(
            process_runner=process,  # type: ignore[arg-type]
            executable="codex.cmd",
            model="fixture-model",
        ).execute(
            request=_request(tmp_path, mode=request_mode),
            repository=tmp_path,
            prompt="Inspect the bounded public fixture.",
            context_json="{}",
            artifact_store=artifacts,
            review_only=review_only,
        )

    assert len(process.calls) == 1
    assert list(artifacts.artifact_root.iterdir()) == []


def test_read_only_codex_git_inspection_keeps_exact_public_snapshot_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    snapshots: list[PublicDataSnapshot] = []

    def classify(repository: Path, *, knowledge_blocked_files: int):
        snapshot = real_build_public_data_snapshot(
            repository,
            knowledge_blocked_files=knowledge_blocked_files,
        )
        snapshots.append(snapshot)
        return snapshot

    class InspectingProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            environment = kwargs.get("environment")
            assert isinstance(environment, dict)
            inspected = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=kwargs["cwd"],
                env=safe_child_environment(environment),  # type: ignore[arg-type]
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            assert inspected.returncode == 0, inspected.stderr.decode(
                "utf-8", errors="replace"
            )
            return super().run(argv, **kwargs)

    monkeypatch.setattr(coding_executors, "build_public_data_snapshot", classify)
    with coding_fixture(run_id="codex-read-only-stable-snapshot") as fixture:
        linked = fixture.add_worktree("codex-read-only-stable-snapshot")
        process = InspectingProcess()

        result = CodexExecutor(
            process_runner=process,  # type: ignore[arg-type]
            executable="codex.cmd",
            model="fixture-model",
        ).execute(
            request=_request(linked.path, mode=CodingMode.READ_ONLY),
            repository=linked.path,
            prompt="Inspect the bounded public fixture.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "codex-read-only-stable-snapshot",
                root=fixture.artifacts_root,
            ),
        )

        assert result.executor is ExecutorKind.CODEX_EXEC
        assert len(snapshots) == 2
        assert snapshots[0] == snapshots[1]
        assert fixture.git(["status", "--porcelain"], cwd=linked.path).stdout == ""


def test_writable_codex_accepts_safe_reclassified_worktree_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    public_file = tmp_path / "public.txt"
    public_file.write_text("before\n", encoding="utf-8")

    class SafeMutationProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            outcome = super().run(argv, **kwargs)
            if argv[1:4] != ["mcp", "list", "--json"]:
                public_file.write_text("safe after\n", encoding="utf-8")
            return outcome

    snapshots: list[str] = []

    def classify(repository: Path, *, knowledge_blocked_files: int):
        assert repository == tmp_path
        assert knowledge_blocked_files == 0
        content = public_file.read_text(encoding="utf-8")
        snapshots.append(content)
        return _public_snapshot(content)

    monkeypatch.setattr(coding_executors, "build_public_data_snapshot", classify)
    process = SafeMutationProcess()

    result = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    ).execute(
        request=_request(tmp_path, mode=CodingMode.WRITE),
        repository=tmp_path,
        prompt="Make the safe bounded public change.",
        context_json="{}",
        artifact_store=ArtifactStore(
            "codex-safe-write-race",
            root=tmp_path / "artifacts",
        ),
    )

    assert result.executor is ExecutorKind.CODEX_EXEC
    assert snapshots == ["before\n", "safe after\n"]
    assert len(process.calls) == 2


def test_writable_codex_rejects_unsafe_post_state_without_persisting_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class UnsafeMutationProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            outcome = super().run(argv, **kwargs)
            if argv[1:4] != ["mcp", "list", "--json"]:
                (tmp_path / ".env.cloud-secret").write_text(
                    "API_TOKEN=unsafe\n",
                    encoding="utf-8",
                )
            return outcome

    def classify(repository: Path, *, knowledge_blocked_files: int):
        assert repository == tmp_path
        assert knowledge_blocked_files == 0
        if (repository / ".env.cloud-secret").exists():
            raise PublicDataPreflightError("synthetic private mutation")
        return _public_snapshot("safe")

    monkeypatch.setattr(coding_executors, "build_public_data_snapshot", classify)
    process = UnsafeMutationProcess()
    artifacts = ArtifactStore("codex-unsafe-post-race", root=tmp_path / "artifacts")

    with pytest.raises(ExecutorPolicyError, match="after cloud execution"):
        CodexExecutor(
            process_runner=process,  # type: ignore[arg-type]
            executable="codex.cmd",
            model="fixture-model",
        ).execute(
            request=_request(tmp_path, mode=CodingMode.WRITE),
            repository=tmp_path,
            prompt="Make the bounded public change.",
            context_json="{}",
            artifact_store=artifacts,
        )

    assert len(process.calls) == 2
    assert list(artifacts.artifact_root.iterdir()) == []


def test_codex_safe_postcheck_preserves_cancelled_executor_failure(
    tmp_path: Path,
):
    class CancelledProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            completed = super().run(argv, **kwargs)
            return ProcessOutcome(
                CommandStatus.CANCELLED,
                None,
                completed.stdout,
                completed.stderr,
                completed.duration_ms,
            )

    artifacts = ArtifactStore("codex-cancelled", root=tmp_path / "artifacts")
    with pytest.raises(ExecutorFailure, match="cancelled") as failure:
        CodexExecutor(
            process_runner=CancelledProcess(),  # type: ignore[arg-type]
            executable="codex.cmd",
            model="fixture-model",
        ).execute(
            request=_request(tmp_path, mode=CodingMode.READ_ONLY),
            repository=tmp_path,
            prompt="Inspect the bounded public fixture.",
            context_json="{}",
            artifact_store=artifacts,
            cancel_event=threading.Event(),
        )

    assert failure.value.output_artifact is not None
    assert artifacts.read_verified(failure.value.output_artifact)


def test_read_only_codex_review_uses_isolated_config_and_never_lists_user_mcp(
    tmp_path: Path,
):
    process = _CodexProcess(review=True)
    artifacts = ArtifactStore("codex-review", root=tmp_path / "artifacts")
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    )

    review_request = _request(tmp_path, mode=CodingMode.WRITE).model_copy(
        update={
            "acceptance_criteria": [
                "The requested bounded result is complete.",
                "Criterion two.",
                "Criterion three.",
                "Criterion four.",
                "Criterion five must reach the reviewer.",
            ],
            "rule_scope_paths": ["src"],
        }
    )
    result = executor.execute(
        request=review_request,
        repository=tmp_path,
        prompt="Review the synthetic fixture.",
        context_json="{}",
        artifact_store=artifacts,
        review_only=True,
    )

    assert result.executor is ExecutorKind.CODEX_REVIEW
    assert len(process.calls) == 1
    command = process.calls[0][0]
    assert command[:8] == [
        "codex.cmd",
        "-a",
        "never",
        "-s",
        "read-only",
        "exec",
        "--ephemeral",
        "--strict-config",
    ]
    assert command.count("--ephemeral") == 1
    assert "shell_environment_policy.inherit=all" in command
    assert "--enable" not in command
    assert "review" not in command
    assert "--uncommitted" not in command
    assert "--ignore-user-config" in command
    disabled = {
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--disable"
    }
    assert disabled == EXPECTED_CODEX_DISABLED
    schema_index = command.index("--output-schema")
    assert Path(command[schema_index + 1]).name == "codex-review.schema.json"
    developer_override = next(
        item for item in command if item.startswith("developer_instructions=")
    )
    developer_instructions = json.loads(developer_override.split("=", 1)[1])
    assert "overall_correctness" in developer_instructions
    assert "absolute_file_path" in developer_instructions
    assert "untrusted evidence" in developer_instructions
    assert command[-1] == "-"
    assert process.calls[0][1]["input_text"] == (
        "Perform a specialized read-only review of the current uncommitted diff. Inspect it "
        "with read-only Git/file commands and return only the configured JSON schema."
    )
    assert "<task-contract>" in developer_instructions
    assert (
        '"goal":"Perform the bounded synthetic public-fixture task."'
        in developer_instructions
    )
    assert (
        '"acceptance_criteria":["The requested bounded result is complete."'
        in developer_instructions
    )
    assert "Criterion five must reach the reviewer." in developer_instructions
    assert '"rule_scope_paths":["src"]' in developer_instructions
    assert '"mode":"write"' in developer_instructions
    assert '"risk":"high"' in developer_instructions
    assert not any(item.startswith("mcp_servers.") for item in command)
    assert json.loads(result.summary)["overall_correctness"] == "patch is correct"


def test_read_only_codex_review_uses_structured_exec_prompt(
    tmp_path: Path,
):
    process = _CodexProcess(review=True)
    result = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    ).execute(
        request=_request(tmp_path, mode=CodingMode.READ_ONLY),
        repository=tmp_path,
        prompt="Review the synthetic fixture.",
        context_json="{}",
        artifact_store=ArtifactStore(
            "codex-read-review-shape", root=tmp_path / "read-review-artifacts"
        ),
        review_only=True,
    )

    command, call = process.calls[0]
    assert "review" not in command
    assert "--uncommitted" not in command
    assert command[-1] == "-"
    assert call["input_text"] == (
        "Perform a specialized read-only review of the current uncommitted diff. Inspect it "
        "with read-only Git/file commands and return only the configured JSON schema."
    )
    assert json.loads(result.summary)["findings"] == []


def test_codex_real_resume_preserves_session_and_omits_ephemeral(
    tmp_path: Path,
):
    process = _CodexProcess()
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    )

    result = executor.execute(
        request=_request(tmp_path, mode=CodingMode.WRITE),
        repository=tmp_path,
        prompt="Continue the bounded synthetic fix.",
        context_json="{}",
        artifact_store=ArtifactStore("codex-resume", root=tmp_path / "artifacts"),
        resume_session_id="codex-fixture-session",
    )

    assert result.executor is ExecutorKind.CODEX_EXEC
    command = process.calls[1][0]
    assert "--ephemeral" not in command
    resume_index = command.index("resume")
    assert command[resume_index : resume_index + 2] == [
        "resume",
        "codex-fixture-session",
    ]
    assert "shell_environment_policy.inherit=all" in command
    assert "--enable" not in command
    disabled = {
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--disable"
    }
    assert disabled == EXPECTED_CODEX_DISABLED


def test_codex_review_rejects_resume_instead_of_reusing_mutating_session(
    tmp_path: Path,
):
    process = _CodexProcess(review=True)
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    )

    with pytest.raises(ExecutorPolicyError, match="review cannot resume"):
        executor.execute(
            request=_request(tmp_path, mode=CodingMode.WRITE),
            repository=tmp_path,
            prompt="Review the synthetic fixture.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "codex-review-resume",
                root=tmp_path / "review-resume-artifacts",
            ),
            resume_session_id="codex-fixture-session",
            review_only=True,
        )

    assert process.calls == []


@pytest.mark.parametrize(
    "inventory",
    [
        "not-json",
        json.dumps({"name": "browser"}),
        json.dumps([{"name": "browser.name"}]),
        json.dumps(["browser"]),
    ],
)
def test_writable_codex_fails_closed_when_mcp_inventory_cannot_be_safely_disabled(
    tmp_path: Path, inventory: str
):
    class InvalidInventoryProcess(_CodexProcess):
        def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
            self.calls.append((list(argv), dict(kwargs)))
            return ProcessOutcome(CommandStatus.PASSED, 0, inventory, "", 1)

    process = InvalidInventoryProcess()
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    )

    with pytest.raises(ExecutorPolicyError):
        executor.execute(
            request=_request(tmp_path, mode=CodingMode.WRITE),
            repository=tmp_path,
            prompt="Make the synthetic fix.",
            context_json="{}",
            artifact_store=ArtifactStore(
                "codex-invalid-mcp", root=tmp_path / "invalid-mcp-artifacts"
            ),
        )

    assert len(process.calls) == 1
    assert process.calls[0][0][1:4] == ["mcp", "list", "--json"]


def test_codex_review_rejects_oversized_output_before_protocol_parsing(
    tmp_path: Path,
):
    policy = CodingPolicy.model_validate(
        load_coding_policy(ROOT / "config" / "coding.json")
        .model_copy(update={"max_artifact_bytes": 1024})
        .model_dump()
    )
    process = _CodexProcess(
        review=True,
        message=(
            "- [P1] hidden finding — src/security.py:1\nConcrete exploit.\n"
            + (" " * 2048)
            + "NO_FINDINGS"
        ),
    )
    executor = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        policy=policy,
        executable="codex.cmd",
        model="fixture-model",
    )

    result = executor.execute(
        request=_request(tmp_path, mode=CodingMode.WRITE),
        repository=tmp_path,
        prompt="Review the synthetic fixture.",
        context_json="{}",
        artifact_store=ArtifactStore(
            "codex-review-oversized", root=tmp_path / "oversized-artifacts"
        ),
        review_only=True,
    )

    assert result.summary == "CODEX_REVIEW_OUTPUT_OVERSIZE"


def test_codex_review_rejects_invalid_utf8_before_protocol_parsing(
    tmp_path: Path,
):
    process = _CodexProcess(
        review=True,
        message=(
            b'{"findings":[],"overall_correctness":"patch is correct",'
            b'"overall_explanation":"invalid-\xff",'
            b'"overall_confidence_score":0.9}'
        ),
    )

    result = CodexExecutor(
        process_runner=process,  # type: ignore[arg-type]
        executable="codex.cmd",
        model="fixture-model",
    ).execute(
        request=_request(tmp_path, mode=CodingMode.WRITE),
        repository=tmp_path,
        prompt="Review the synthetic fixture.",
        context_json="{}",
        artifact_store=ArtifactStore(
            "codex-review-invalid-utf8", root=tmp_path / "invalid-utf8-artifacts"
        ),
        review_only=True,
    )

    assert result.summary == "CODEX_REVIEW_OUTPUT_INVALID_UTF8"
