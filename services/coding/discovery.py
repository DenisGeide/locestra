from __future__ import annotations

import json
import re
from pathlib import Path

from services.coding.contracts import CodingMode, VerificationCommandV1
from services.coding.git import is_regular_repository_file, run_git


class VerificationCapabilityError(RuntimeError):
    pass


def _tracked(repository: Path, pathspec: str) -> tuple[str, ...]:
    raw = run_git(
        repository,
        ["ls-files", "-z", "--", pathspec],
        timeout=30,
        max_output_bytes=8 * 1024 * 1024,
    ).stdout
    result: list[str] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        try:
            relative = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        candidate = repository / relative
        if is_regular_repository_file(repository, candidate):
            result.append(relative.replace("\\", "/"))
    return tuple(result)


def discover_verification_commands(repository: Path) -> list[VerificationCommandV1]:
    """Infer only conventional, non-installing verifiers from repository manifests."""

    root = repository.resolve(strict=True)
    commands: list[VerificationCommandV1] = []
    has_python_tests = any(
        Path(relative).name.startswith("test") and Path(relative).suffix.casefold() == ".py"
        for relative in _tracked(root, "tests")
    )
    pytest_configured = any(
        is_regular_repository_file(root, root / name)
        for name in ("pytest.ini", "tox.ini", "conftest.py")
    )
    pyproject = root / "pyproject.toml"
    if is_regular_repository_file(root, pyproject):
        try:
            head = pyproject.read_text(encoding="utf-8", errors="strict")[:256_000].casefold()
            pytest_configured = pytest_configured or "pytest" in head
        except (OSError, UnicodeDecodeError):
            pass
    if has_python_tests:
        commands.append(
            VerificationCommandV1(
                argv=(
                    ["python", "-m", "pytest", "-q"]
                    if pytest_configured
                    else ["python", "-m", "unittest", "discover", "-s", "tests"]
                ),
                purpose="Run the repository's conventional Python test suite.",
                timeout_seconds=900,
                required=True,
            )
        )

    package = root / "package.json"
    if is_regular_repository_file(root, package):
        try:
            value = json.loads(package.read_text(encoding="utf-8", errors="strict"))
            scripts = value.get("scripts", {}) if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            commands.append(
                VerificationCommandV1(
                    argv=["npm.cmd", "test"],
                    purpose="Run the existing package test script without installing dependencies.",
                    timeout_seconds=900,
                    required=True,
                )
            )
    return commands[:8]


def _has_python_dependency_materialization(repository: Path) -> bool:
    for pattern in (
        "requirements*.txt",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "pdm.lock",
    ):
        if _tracked(repository, pattern):
            return True
    pyproject = repository / "pyproject.toml"
    if not is_regular_repository_file(repository, pyproject):
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="strict")[:512_000]
    except (OSError, UnicodeDecodeError):
        return True
    return bool(
        re.search(r"(?m)^\s*dependencies\s*=\s*\[(?!\s*\])", text)
        or re.search(r"(?m)^\s*\[tool\.(?:poetry|pdm)\.dependencies\]\s*$", text)
    )


def _has_node_dependency_materialization(repository: Path) -> bool:
    package = repository / "package.json"
    if not is_regular_repository_file(repository, package):
        return False
    try:
        value = json.loads(package.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(value, dict):
        return True
    return any(
        isinstance(value.get(key), dict) and bool(value[key])
        for key in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        )
    )


def validate_verification_capabilities(
    repository: Path,
    commands: list[VerificationCommandV1],
    *,
    mode: CodingMode,
) -> None:
    """Fail before model execution when the pinned sandbox cannot verify."""

    if mode is CodingMode.READ_ONLY:
        return
    root = repository.resolve(strict=True)
    supported = {"python", "pytest", "uv", "node", "npm", "yarn", "git"}
    programs = {
        Path(command.argv[0]).name.casefold().removesuffix(".exe").removesuffix(".cmd")
        for command in commands
    }
    unsupported = sorted(programs.difference(supported))
    if unsupported:
        raise VerificationCapabilityError(
            "pinned verification sandbox does not support: " + ", ".join(unsupported)
        )
    if programs.intersection({"python", "pytest", "uv"}) and _has_python_dependency_materialization(root):
        raise VerificationCapabilityError(
            "Python dependency materialization is unavailable in the networkless verifier"
        )
    if programs.intersection({"node", "npm", "yarn"}) and _has_node_dependency_materialization(root):
        raise VerificationCapabilityError(
            "Node dependency materialization is unavailable in the networkless verifier"
        )
    unsupported_manifests = [
        name
        for name in ("Cargo.toml", "go.mod")
        if is_regular_repository_file(root, root / name)
    ]
    unsupported_manifests.extend(
        item for item in _tracked(root, "*.sln") if "/" not in item
    )
    # A repository may contain more than one ecosystem.  The presence of a
    # supported, unrelated command does not make an unsupported Cargo/Go/.NET
    # subtree verifiable in the pinned sandbox.
    if unsupported_manifests:
        raise VerificationCapabilityError(
            "repository requires an unsupported pinned verifier recipe: "
            + ", ".join(sorted(unsupported_manifests))
        )


__all__ = [
    "VerificationCapabilityError",
    "discover_verification_commands",
    "validate_verification_capabilities",
]
