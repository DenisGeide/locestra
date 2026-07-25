from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.common import ROOT
from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import (
    ArtifactKind,
    CommandResultV1,
    CommandStatus,
    VerificationCommandV1,
)
from services.coding.git import ensure_regular_owned_file, git_diff
from services.coding.process import ProcessOutcome, ProcessRunner
from services.coding.resources import WritableMountWatchdog, WritableResourceLimitError


class VerificationPolicyError(RuntimeError):
    """A requested verification command is outside the non-destructive policy."""


_SAFE_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHELL_PROGRAMS = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
}
_NODE_SAFE_TARGETS = {"test", "lint", "typecheck", "build", "check"}
_EXTERNAL_PATH_OPTIONS = {
    "--artifacts-path",
    "--basetemp",
    "--confcutdir",
    "--config-file",
    "--cov-config",
    "--cov-report",
    "--html",
    "--junitxml",
    "--manifest-path",
    "--output",
    "--prefix",
    "--rootdir",
    "--target-dir",
    "-c",
    "-exec",
    "-o",
    "-toolexec",
}
_PYTHON_VERIFIER_BASE_IMAGE = (
    "python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)
_PYTHON_VERIFIER_IMAGE = "local-agent/verifier-python:3.12.11-pytest8.4.1-v2"
_PYTHON_VERIFIER_RECIPE = "python-3.12.11-pytest-8.4.1-v2"
_NODE_VERIFIER_IMAGE = (
    "ghcr.io/qwenlm/qwen-code:0.19.10@"
    "sha256:03456a270da8d1bf1f1d5e6bf5e340718b595355b68649e0f6940cb7ff8dbeda"
)
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _VerificationContainerSpec:
    image: str
    argv: tuple[str, ...]
    workspace_writable: bool = False
    runtime_read_only: bool = False


def _program(value: str) -> str:
    # Treat both separators as path syntax even when policy tests for Windows
    # command lines run on a POSIX host (or vice versa).
    return re.split(r"[\\/]", value)[-1].casefold()


def _program_is_path_qualified(value: str) -> bool:
    return (
        value != re.split(r"[\\/]", value)[-1]
        or "/" in value
        or "\\" in value
        or bool(re.match(r"^[A-Za-z]:", value))
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=True)))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _trusted_search_directories(cwd: Path | None) -> tuple[Path, ...]:
    directories: list[Path] = []
    seen: set[str] = set()
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            # Empty/relative PATH entries make Windows and POSIX search the
            # caller's cwd.  They are never a trust anchor.
            continue
        try:
            canonical = candidate.resolve(strict=True)
        except OSError:
            continue
        if not canonical.is_dir() or (cwd is not None and _is_within(canonical, cwd)):
            continue
        key = os.path.normcase(str(canonical))
        if key not in seen:
            seen.add(key)
            directories.append(canonical)
    return tuple(directories)


def _candidate_names(program: str) -> tuple[str, ...]:
    if os.name != "nt":
        return (program,)
    extensions = tuple(
        item.casefold()
        for item in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        if item
    )
    suffix = Path(program).suffix.casefold()
    if suffix in extensions:
        return (program,)
    return tuple(dict.fromkeys([program, *(program + extension for extension in extensions)]))


def _discover_trusted_program(program: str, *, cwd: Path | None) -> Path | None:
    for directory in _trusted_search_directories(cwd):
        for name in _candidate_names(program):
            candidate = directory / name
            try:
                canonical = candidate.resolve(strict=True)
            except OSError:
                continue
            if not canonical.is_file():
                continue
            if os.name != "nt" and not os.access(canonical, os.X_OK):
                continue
            return canonical
    return None


def _resolve_trusted_program(
    value: str, *, cwd: Path | None, policy: CodingPolicy
) -> Path:
    allowed = {item.casefold() for item in policy.allowed_verification_programs}
    requested_name = _program(value)
    requested_bare = requested_name.removesuffix(".exe").removesuffix(".cmd")

    # The interpreter hosting the Coding Engine is an explicit trust anchor.
    # This supports version-pinned virtual environments without permitting an
    # arbitrary repository-local python.exe with the same basename.
    try:
        engine_python = Path(sys.executable).resolve(strict=True)
    except OSError:
        engine_python = None
    explicit: Path | None = None
    if _program_is_path_qualified(value):
        raw_explicit = Path(value)
        base = cwd or Path.cwd()
        candidate = raw_explicit if raw_explicit.is_absolute() else base / raw_explicit
        try:
            explicit = candidate.resolve(strict=True)
        except OSError as exc:
            raise VerificationPolicyError("verification executable path is unavailable") from exc
    if requested_bare == "python" and engine_python is not None:
        if explicit is None or _path_key(explicit) == _path_key(engine_python):
            if _program(engine_python.name) in allowed:
                return engine_python

    discovered = _discover_trusted_program(requested_name, cwd=cwd)
    if discovered is None and requested_name.endswith((".exe", ".cmd")):
        discovered = _discover_trusted_program(requested_bare, cwd=cwd)
    if discovered is None:
        raise VerificationPolicyError("trusted verification executable is unavailable")

    if explicit is not None:
        if _path_key(explicit) != _path_key(discovered):
            raise VerificationPolicyError(
                "verification executable path is not the canonical trusted discovery result"
            )
    if _program(discovered.name) not in allowed:
        raise VerificationPolicyError("resolved verification executable is not allowlisted")
    return discovered


def _tokens(argv: list[str]) -> list[str]:
    return [item.casefold().strip() for item in argv]


def is_semantic_verification_argv(argv: list[str]) -> bool:
    """Return whether an allowlisted verifier exercises project behaviour.

    Git status and whitespace checks are structural evidence only.  Keeping
    this classifier next to the command-shape policy prevents the independent
    reviewer from accidentally treating a newly allowed Git inspection as a
    semantic test/build/typecheck/lint/UI gate.
    """

    if not argv:
        return False
    return _program(argv[0]).removesuffix(".exe").removesuffix(".cmd") != "git"


def _reject_external_paths(arguments: list[str]) -> None:
    for token in arguments:
        lowered = token.casefold()
        option = lowered.split("=", 1)[0]
        if option in _EXTERNAL_PATH_OPTIONS:
            raise VerificationPolicyError("verification path/output override is forbidden")
        if token.startswith("-"):
            continue
        normalized = token.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("//")
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in normalized.split("/")
        ):
            raise VerificationPolicyError("verification argument escapes the task worktree")


def validate_verification_argv(
    argv: list[str], *, policy: CodingPolicy | None = None, cwd: Path | None = None
) -> list[str]:
    """Validate a shell-free, non-installing project verification invocation."""

    effective = policy or get_coding_policy()
    if not argv or len(argv) > 64:
        raise VerificationPolicyError("verification argv must contain 1-64 arguments")
    if any(
        not isinstance(item, str)
        or not item
        or "\x00" in item
        or "\r" in item
        or "\n" in item
        for item in argv
    ):
        raise VerificationPolicyError("verification argv contains an invalid argument")
    program = _program(argv[0])
    allowed = {item.casefold() for item in effective.allowed_verification_programs}
    if program not in allowed or program in _SHELL_PROGRAMS:
        raise VerificationPolicyError("verification program is not allowlisted")
    if _program_is_path_qualified(argv[0]):
        _resolve_trusted_program(argv[0], cwd=cwd, policy=effective)
    lowered = _tokens(argv)
    denied = {item.casefold() for item in effective.denied_verification_tokens}
    for token in lowered[1:]:
        # Options such as --no-deploy must not smuggle an explicitly denied
        # lifecycle operation into a supposedly read-only verifier.
        words = {part for part in re.split(r"[^a-z0-9]+", token.lstrip("-")) if part}
        if token in denied or words.intersection(denied):
            raise VerificationPolicyError("verification command contains a denied operation")

    arguments = lowered[1:]
    _reject_external_paths(argv[1:])
    bare = program.removesuffix(".exe").removesuffix(".cmd")
    if bare == "pytest":
        return list(argv)
    if bare == "python":
        if len(arguments) < 2 or arguments[0] != "-m" or arguments[1] not in {"pytest", "unittest"}:
            raise VerificationPolicyError("python verification must use -m pytest or -m unittest")
        return list(argv)
    if bare == "uv":
        if not arguments or arguments[0] != "run":
            raise VerificationPolicyError("uv verification must start with uv run")
        nested_index = next(
            (index for index, item in enumerate(arguments[1:], start=1) if not item.startswith("-")),
            None,
        )
        if nested_index is None:
            raise VerificationPolicyError("uv run requires an allowlisted nested verifier")
        uv_options = set(arguments[1:nested_index])
        if "--no-sync" not in uv_options or not uv_options.issubset(
            {"--no-sync", "--offline", "--frozen"}
        ):
            raise VerificationPolicyError("uv run must be non-syncing and use only safe runtime options")
        nested = argv[nested_index + 1 :]
        if not nested:
            raise VerificationPolicyError("uv run requires an allowlisted nested verifier")
        nested_program = _program(nested[0]).removesuffix(".exe").removesuffix(".cmd")
        if nested_program == "pytest":
            return list(argv)
        if nested_program == "python":
            nested_lower = _tokens(nested[1:])
            if len(nested_lower) >= 2 and nested_lower[0] == "-m" and nested_lower[1] in {"pytest", "unittest"}:
                return list(argv)
        raise VerificationPolicyError("uv run nested verifier is not allowlisted")
    if bare in {"npm", "pnpm", "yarn"}:
        positional = [item for item in arguments if not item.startswith("-")]
        if positional == ["test"] or (
            len(positional) == 2
            and positional[0] in {"run", "run-script"}
            and positional[1] in _NODE_SAFE_TARGETS
        ):
            return list(argv)
        raise VerificationPolicyError("package verification must run an existing safe script")
    if bare == "cargo":
        positional = [item for item in arguments if not item.startswith("-")]
        if positional and positional[0] in {"test", "check", "clippy"}:
            return list(argv)
        raise VerificationPolicyError("cargo verification action is not allowlisted")
    if bare == "go":
        if arguments and arguments[0] == "test":
            return list(argv)
        raise VerificationPolicyError("go verification must use go test")
    if bare == "dotnet":
        if arguments and arguments[0] in {"test", "build"}:
            return list(argv)
        raise VerificationPolicyError("dotnet verification action is not allowlisted")
    if bare == "node":
        if len(argv) != 2:
            raise VerificationPolicyError("node verification requires one repository test file")
        candidate = Path(argv[1].replace("\\", "/"))
        if (
            not candidate.parts
            or candidate.parts[0].casefold() not in {"test", "tests"}
            or candidate.suffix.casefold() not in {".js", ".cjs", ".mjs"}
        ):
            raise VerificationPolicyError("node verification target must be a repository test file")
        return list(argv)
    if bare == "git":
        if arguments == ["diff", "--check"] or (
            len(arguments) >= 2
            and arguments[0] == "status"
            and any(item in {"--porcelain", "--porcelain=v1"} for item in arguments[1:])
            and all(item in {"--porcelain", "--porcelain=v1", "-z", "--untracked-files=all"} for item in arguments[1:])
        ):
            return list(argv)
        raise VerificationPolicyError("only git diff --check or porcelain status is a verifier")
    raise VerificationPolicyError("verification command shape is not allowlisted")


def resolve_verification_argv(
    argv: list[str], *, cwd: Path, policy: CodingPolicy | None = None
) -> list[str]:
    """Bind a validated command to one canonical, non-repository executable.

    Validation by basename is insufficient because both an absolute
    ``C:\\attacker\\pytest.exe`` and a PATH entry inside the target repository
    would otherwise satisfy the textual allowlist.  The returned argv always
    starts with the exact trusted executable that the engine discovered.
    """

    effective = policy or get_coding_policy()
    try:
        canonical_cwd = cwd.resolve(strict=True)
    except OSError as exc:
        raise VerificationPolicyError("verification cwd is unavailable") from exc
    validated = validate_verification_argv(
        argv, policy=effective, cwd=canonical_cwd
    )
    executable = _resolve_trusted_program(
        validated[0], cwd=canonical_cwd, policy=effective
    )
    return [str(executable), *validated[1:]]


def _trusted_docker(repository: Path) -> Path:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if not executable:
        raise VerificationPolicyError("Docker is required for coding verification")
    try:
        canonical = Path(executable).resolve(strict=True)
        canonical.relative_to(repository.resolve(strict=True))
    except ValueError:
        pass
    except OSError as exc:
        raise VerificationPolicyError("trusted Docker executable is unavailable") from exc
    else:
        raise VerificationPolicyError("repository-local Docker executable is forbidden")
    if canonical.name.casefold() not in {"docker", "docker.exe"} or not canonical.is_file():
        raise VerificationPolicyError("trusted Docker executable is invalid")
    return canonical


def _run_docker_control(
    process_runner: ProcessRunner,
    docker: Path,
    arguments: list[str],
    *,
    cwd: Path,
    timeout_seconds: float = 120,
    cancel_event: threading.Event | None = None,
):
    return process_runner.run(
        [str(docker), *arguments],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _ensure_remote_image(
    process_runner: ProcessRunner,
    docker: Path,
    image: str,
    *,
    cwd: Path,
    cancel_event: threading.Event | None,
) -> str:
    inspected = _run_docker_control(
        process_runner,
        docker,
        ["image", "inspect", "--format", "{{.Id}}", image],
        cwd=cwd,
        cancel_event=cancel_event,
    )
    image_id = inspected.stdout.strip()
    if inspected.status.value != "passed" or not _IMAGE_ID.fullmatch(image_id):
        pulled = _run_docker_control(
            process_runner,
            docker,
            ["pull", image],
            cwd=cwd,
            timeout_seconds=600,
            cancel_event=cancel_event,
        )
        if pulled.status.value != "passed":
            raise VerificationPolicyError("pinned verification image is unavailable")
        inspected = _run_docker_control(
            process_runner,
            docker,
            ["image", "inspect", "--format", "{{.Id}}", image],
            cwd=cwd,
            cancel_event=cancel_event,
        )
        image_id = inspected.stdout.strip()
    if inspected.status.value != "passed" or not _IMAGE_ID.fullmatch(image_id):
        raise VerificationPolicyError("pinned verification image identity is invalid")
    expected_digest = image.rsplit("@sha256:", 1)[-1]
    if image_id != f"sha256:{expected_digest}":
        raise VerificationPolicyError("pinned verification image digest mismatch")
    return image_id


def _ensure_python_verifier_image(
    process_runner: ProcessRunner,
    docker: Path,
    *,
    cwd: Path,
    cancel_event: threading.Event | None,
) -> str:
    format_string = "{{.Id}} {{json .Config.Labels}}"

    def inspect():
        return _run_docker_control(
            process_runner,
            docker,
            ["image", "inspect", "--format", format_string, _PYTHON_VERIFIER_IMAGE],
            cwd=cwd,
            cancel_event=cancel_event,
        )

    inspected = inspect()
    fields = inspected.stdout.strip().split(" ", 1)
    try:
        labels = json.loads(fields[1]) if len(fields) == 2 else {}
    except json.JSONDecodeError:
        labels = {}
    valid = (
        inspected.status.value == "passed"
        and len(fields) == 2
        and _IMAGE_ID.fullmatch(fields[0]) is not None
        and labels.get("local-agent.component") == "coding-verifier-python"
        and labels.get("local-agent.recipe") == _PYTHON_VERIFIER_RECIPE
    )
    if not valid:
        _ensure_remote_image(
            process_runner,
            docker,
            _PYTHON_VERIFIER_BASE_IMAGE,
            cwd=cwd,
            cancel_event=cancel_event,
        )
        context = (ROOT / "config" / "coding-verifier").resolve(strict=True)
        dockerfile = (context / "Dockerfile").resolve(strict=True)
        try:
            dockerfile.relative_to(context)
        except ValueError as exc:
            raise VerificationPolicyError("verification image recipe escapes trusted config") from exc
        built = _run_docker_control(
            process_runner,
            docker,
            [
                "build",
                "--pull=false",
                "--network=default",
                "--tag",
                _PYTHON_VERIFIER_IMAGE,
                "--file",
                str(dockerfile),
                str(context),
            ],
            cwd=cwd,
            timeout_seconds=900,
            cancel_event=cancel_event,
        )
        if built.status.value != "passed":
            raise VerificationPolicyError("trusted Python verifier image build failed")
        inspected = inspect()
        fields = inspected.stdout.strip().split(" ", 1)
        try:
            labels = json.loads(fields[1]) if len(fields) == 2 else {}
        except json.JSONDecodeError:
            labels = {}
        valid = (
            inspected.status.value == "passed"
            and len(fields) == 2
            and _IMAGE_ID.fullmatch(fields[0]) is not None
            and labels.get("local-agent.component") == "coding-verifier-python"
            and labels.get("local-agent.recipe") == _PYTHON_VERIFIER_RECIPE
        )
    if not valid:
        raise VerificationPolicyError("trusted Python verifier image identity is invalid")
    # Always execute by immutable image ID, never by the locally mutable tag.
    return fields[0]


def _safe_runtime_directory(artifact_store: ArtifactStore, command_id: str) -> Path:
    task_root = artifact_store.task_root.resolve(strict=True)
    runtime = task_root / "runtime" / "verification" / command_id
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        canonical = runtime.resolve(strict=True)
        canonical.relative_to(task_root)
    except (OSError, ValueError) as exc:
        raise VerificationPolicyError("verification runtime ownership mismatch") from exc
    if canonical != runtime.absolute():
        raise VerificationPolicyError("verification runtime may not be a link")
    for name in ("home", "temp", "cache", "empty-hooks"):
        directory = canonical / name
        directory.mkdir(exist_ok=True)
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(canonical)
        except (OSError, ValueError) as exc:
            raise VerificationPolicyError("verification task directory escapes runtime") from exc
        if resolved != directory.absolute():
            raise VerificationPolicyError("verification task directory may not be a link")
    return canonical


def _write_runtime_bytes(path: Path, payload: bytes, *, root: Path) -> None:
    try:
        canonical_parent = path.parent.resolve(strict=True)
        canonical_parent.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VerificationPolicyError("verification runtime file escapes task scope") from exc
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise VerificationPolicyError("verification runtime file ownership mismatch")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _python_inner_argv(requested: list[str]) -> tuple[str, ...]:
    bare = _program(requested[0]).removesuffix(".exe").removesuffix(".cmd")
    if bare == "pytest":
        return ("/usr/local/bin/python", "-m", "pytest", *requested[1:])
    if bare == "python":
        return ("/usr/local/bin/python", *requested[1:])
    if bare == "uv":
        arguments = requested[1:]
        nested_index = next(
            (
                index
                for index, item in enumerate(arguments[1:], start=1)
                if not item.startswith("-")
            ),
            None,
        )
        if nested_index is None:
            raise VerificationPolicyError("uv verifier has no nested command")
        nested = arguments[nested_index:]
        nested_bare = _program(nested[0]).removesuffix(".exe").removesuffix(".cmd")
        if nested_bare == "pytest":
            return ("/usr/local/bin/python", "-m", "pytest", *nested[1:])
        if nested_bare == "python":
            return ("/usr/local/bin/python", *nested[1:])
    raise VerificationPolicyError("Python verification adapter rejected the command")


def _verification_container_spec(
    requested: list[str],
    *,
    python_image: str,
    runtime: Path,
    repository: Path,
    policy: CodingPolicy,
) -> _VerificationContainerSpec:
    bare = _program(requested[0]).removesuffix(".exe").removesuffix(".cmd")
    if bare in {"python", "pytest", "uv"}:
        return _VerificationContainerSpec(
            image=python_image,
            argv=_python_inner_argv(requested),
        )
    if bare in {"node", "npm", "yarn"}:
        executable = {
            "node": "/usr/local/bin/node",
            "npm": "/usr/local/bin/npm",
            "yarn": "/usr/local/bin/yarn",
        }[bare]
        writable = bare in {"npm", "yarn"} and any(
            item.casefold() == "build" for item in requested[1:]
        )
        return _VerificationContainerSpec(
            image=_NODE_VERIFIER_IMAGE,
            argv=(executable, *requested[1:]),
            workspace_writable=writable,
        )
    if bare == "git":
        patch = git_diff(repository, max_bytes=policy.max_diff_bytes)
        checker_source = (ROOT / "config" / "coding-verifier" / "check_diff.py").read_bytes()
        _write_runtime_bytes(runtime / "input.diff", patch, root=runtime)
        _write_runtime_bytes(runtime / "check_diff.py", checker_source, root=runtime)
        return _VerificationContainerSpec(
            image=python_image,
            argv=(
                "/usr/local/bin/python",
                "/local-agent/runtime/check_diff.py",
                "/local-agent/runtime/input.diff",
            ),
            runtime_read_only=True,
        )
    raise VerificationPolicyError(
        f"the pinned verification sandbox does not provide {bare} yet"
    )


def _run_verification_in_docker(
    *,
    process_runner: ProcessRunner,
    artifact_store: ArtifactStore,
    policy: CodingPolicy,
    requested_argv: list[str],
    repository: Path,
    command_id: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
):
    docker = _trusted_docker(repository)
    runtime = _safe_runtime_directory(artifact_store, command_id)
    task_root = artifact_store.task_root.resolve(strict=True)
    try:
        task_root.relative_to(repository)
    except ValueError:
        pass
    else:
        raise VerificationPolicyError(
            "verification task directories may not be nested in the worktree"
        )
    try:
        repository.relative_to(task_root)
    except ValueError:
        pass
    else:
        raise VerificationPolicyError(
            "verification worktree may not be nested in task directories"
        )
    python_image = _ensure_python_verifier_image(
        process_runner,
        docker,
        cwd=repository,
        cancel_event=cancel_event,
    )
    spec = _verification_container_spec(
        requested_argv,
        python_image=python_image,
        runtime=runtime,
        repository=repository,
        policy=policy,
    )
    if spec.image == _NODE_VERIFIER_IMAGE:
        _ensure_remote_image(
            process_runner,
            docker,
            _NODE_VERIFIER_IMAGE,
            cwd=repository,
            cancel_event=cancel_event,
        )
    git_marker_mount: list[str] = []
    if spec.workspace_writable:
        git_marker = repository / ".git"
        try:
            ensure_regular_owned_file(git_marker)
            if git_marker.parent.resolve(strict=True) != repository.resolve(strict=True):
                raise VerificationPolicyError(
                    "verification worktree Git marker escapes the owned worktree"
                )
        except (OSError, RuntimeError) as exc:
            raise VerificationPolicyError(
                "writable verification requires an owned linked-worktree Git marker"
            ) from exc
        git_marker_mount = [
            "--volume",
            f"{git_marker}:/workspace/.git:ro",
        ]
    run_nonce = secrets.token_hex(8)
    container_name = f"local-agent-verify-{run_nonce}"
    run_label = f"local-agent.run={run_nonce}"
    workspace_mode = "rw" if spec.workspace_writable else "ro"
    runtime_mode = "ro" if spec.runtime_read_only else "rw"
    home = (runtime / "home").resolve(strict=True)
    cache = (runtime / "cache").resolve(strict=True)
    hooks = (runtime / "empty-hooks").resolve(strict=True)
    environment = {
        "CI": "1",
        "HOME": "/home/local-agent",
        "USERPROFILE": "/home/local-agent",
        "TEMP": "/tmp",
        "TMP": "/tmp",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/local-agent/cache",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "NPM_CONFIG_CACHE": "/local-agent/cache/npm",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/workspace",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "/local-agent/empty-hooks",
        "GIT_CONFIG_KEY_2": "credential.helper",
        "GIT_CONFIG_VALUE_2": "",
        "GIT_TERMINAL_PROMPT": "0",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
    }
    environment_args = [
        item
        for key, value in environment.items()
        for item in ("--env", f"{key}={value}")
    ]
    docker_argv = [
        str(docker),
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        str(policy.verifier_memory_bytes),
        "--memory-swap",
        str(policy.verifier_memory_swap_bytes),
        "--cpus",
        str(policy.verifier_cpus),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
        "--name",
        container_name,
        "--label",
        "local-agent.owner=coding-engine",
        "--label",
        "local-agent.component=coding-verification",
        "--label",
        run_label,
        "--label",
        f"local-agent.task={artifact_store.task_id}",
        "--workdir",
        "/workspace",
        "--volume",
        f"{repository}:/workspace:{workspace_mode}",
        *git_marker_mount,
        "--volume",
        f"{home}:/home/local-agent:rw",
        "--volume",
        f"{cache}:/local-agent/cache:rw",
        "--volume",
        f"{hooks}:/local-agent/empty-hooks:ro",
        "--volume",
        f"{runtime}:/local-agent/runtime:{runtime_mode}",
        *environment_args,
        "--entrypoint",
        "/usr/bin/timeout",
        spec.image,
        "--signal=TERM",
        "--kill-after=10s",
        f"{int(timeout_seconds) + 30}s",
        *spec.argv,
    ]
    outcome = None
    cleanup_failed = False
    try:
        writable_roots = (
            runtime,
            *((repository,) if spec.workspace_writable else ()),
        )
        with WritableMountWatchdog(
            writable_roots,
            max_growth_bytes=policy.verifier_max_writable_bytes,
            free_space_reserve_bytes=policy.host_free_space_reserve_bytes,
            max_entries=policy.writable_watchdog_max_entries,
            scan_timeout_seconds=policy.writable_watchdog_scan_timeout_seconds,
            scan_poll_seconds=policy.writable_watchdog_poll_seconds,
            free_space_poll_seconds=policy.free_space_watchdog_poll_seconds,
            caller_cancel_event=cancel_event,
        ) as watchdog:
            outcome = process_runner.run(
                docker_argv,
                cwd=repository,
                timeout_seconds=timeout_seconds,
                cancel_event=watchdog.cancellation,  # type: ignore[arg-type]
            )
    except WritableResourceLimitError as exc:
        raise VerificationPolicyError(
            "verification writable resource watchdog blocked execution"
        ) from exc
    finally:
        try:
            _run_docker_control(
                process_runner,
                docker,
                ["rm", "--force", container_name],
                cwd=repository,
                timeout_seconds=30,
            )
        except BaseException:
            cleanup_failed = True
        try:
            remaining = _run_docker_control(
                process_runner,
                docker,
                [
                    "ps",
                    "--all",
                    "--filter",
                    f"label={run_label}",
                    "--format",
                    "{{.Names}}",
                ],
                cwd=repository,
                timeout_seconds=30,
            )
        except BaseException:
            remaining = None
            cleanup_failed = True
        cleanup_failed = cleanup_failed or (
            remaining is None
            or remaining.status.value != "passed"
            or bool(remaining.stdout.strip())
        )
        for name in ("input.diff", "check_diff.py"):
            target = runtime / name
            if target.exists() and target.is_file() and not target.is_symlink():
                target.unlink(missing_ok=True)
        if cleanup_failed:
            raise VerificationPolicyError(
                "verification sandbox resources could not be cleaned up"
            )
    if outcome is None:
        raise VerificationPolicyError("verification sandbox ended without an outcome")
    return outcome


class VerificationRunner:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        process_runner: ProcessRunner | None = None,
        policy: CodingPolicy | None = None,
    ) -> None:
        self.policy = policy or get_coding_policy()
        self.artifact_store = artifact_store
        self.process_runner = process_runner or ProcessRunner(self.policy)

    def run(
        self,
        command: VerificationCommandV1,
        *,
        command_id: str,
        cwd: Path,
        cancel_event: threading.Event | None = None,
    ) -> CommandResultV1:
        if not _SAFE_COMMAND_ID.fullmatch(command_id):
            raise VerificationPolicyError("invalid command id")
        try:
            canonical_cwd = cwd.resolve(strict=True)
        except OSError as exc:
            raise VerificationPolicyError("verification cwd is unavailable") from exc
        argv = resolve_verification_argv(
            command.argv, cwd=canonical_cwd, policy=self.policy
        )
        started_at = datetime.now(timezone.utc)
        sandbox_started = time.monotonic()
        try:
            outcome = _run_verification_in_docker(
                process_runner=self.process_runner,
                artifact_store=self.artifact_store,
                policy=self.policy,
                requested_argv=command.argv,
                repository=canonical_cwd,
                command_id=command_id,
                timeout_seconds=min(
                    command.timeout_seconds, self.policy.verification_timeout_seconds
                ),
                cancel_event=cancel_event,
            )
        except VerificationPolicyError as exc:
            cancelled = cancel_event is not None and cancel_event.is_set()
            outcome = ProcessOutcome(
                status=(CommandStatus.CANCELLED if cancelled else CommandStatus.FAILED),
                exit_code=(None if cancelled else 125),
                stdout="",
                stderr=f"verification sandbox failed closed: {exc}",
                duration_ms=int((time.monotonic() - sandbox_started) * 1000),
            )
        finished_at = datetime.now(timezone.utc)
        combined = outcome.stdout
        if outcome.stderr:
            combined = f"{combined}\n{outcome.stderr}" if combined else outcome.stderr
        output = self.artifact_store.write_text(
            kind=ArtifactKind.COMMAND_OUTPUT,
            text=combined or "[no output]",
            producer="coding-verification",
            occurrence_id=command_id,
            redact=True,
        )
        status = outcome.status
        summary = {
            CommandStatus.PASSED: "verification passed",
            CommandStatus.FAILED: "verification failed",
            CommandStatus.TIMED_OUT: "verification timed out",
            CommandStatus.CANCELLED: "verification cancelled",
            CommandStatus.NOT_RUN: "verification was not run",
        }[status]
        return CommandResultV1(
            command_id=command_id,
            argv=argv,
            cwd=str(canonical_cwd),
            purpose=command.purpose,
            status=status,
            exit_code=outcome.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=outcome.duration_ms,
            output_artifact_id=output.artifact_id,
            summary=summary,
        )


__all__ = [
    "is_semantic_verification_argv",
    "VerificationPolicyError",
    "VerificationRunner",
    "resolve_verification_argv",
    "validate_verification_argv",
]
