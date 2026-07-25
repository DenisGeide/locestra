from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.coding import ui as coding_ui
from services.coding.artifacts import ArtifactStore
from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CommandStatus,
)
from services.coding.process import ProcessOutcome
from services.coding.ui import UIVerificationError, UIVerificationRunner


def _node_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _executable(path: Path) -> Path:
    path.write_bytes(b"synthetic Node executable fixture")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _request(repository: Path) -> CodingTaskRequestV1:
    return CodingTaskRequestV1(
        task_id="ui-security-task",
        request_id="ui-security-request",
        goal="Verify the bounded loopback UI.",
        repository_path=str(repository),
        mode=CodingMode.READ_ONLY,
        risk=CodingRisk.LOW,
        acceptance_criteria=["The expected UI text is visible."],
        verification_plan=["Use the bounded Playwright adapter."],
        permissions=CodingPermissionsV1(),
        ui_url="http://127.0.0.1:8765/",
        ui_selector="#status",
        ui_expected_text="READY",
    )


def test_trusted_node_skips_repository_local_path_hijack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted-bin"
    repository.mkdir()
    trusted_bin.mkdir()
    _executable(repository / _node_name())
    trusted = _executable(trusted_bin / _node_name())
    monkeypatch.setenv("PATH", os.pathsep.join((str(repository), str(trusted_bin))))

    resolved = coding_ui._trusted_node(repository)

    assert resolved == trusted.resolve(strict=True)
    assert resolved.is_absolute()


def test_trusted_node_fails_closed_when_only_path_hit_is_repository_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _executable(repository / _node_name())
    monkeypatch.setenv("PATH", str(repository))

    with pytest.raises(UIVerificationError, match="trusted Node executable"):
        coding_ui._trusted_node(repository)


class _RecordingProcessRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: list[str], **kwargs: object) -> ProcessOutcome:
        self.calls.append((list(argv), dict(kwargs)))
        return ProcessOutcome(
            status=CommandStatus.PASSED,
            exit_code=0,
            stdout='{"matched":true}',
            stderr="",
            duration_ms=1,
        )


def test_ui_runner_passes_only_canonical_trusted_node_to_process_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted-bin"
    repository.mkdir()
    trusted_bin.mkdir()
    trusted = _executable(trusted_bin / _node_name())
    monkeypatch.setenv("PATH", str(trusted_bin))
    process = _RecordingProcessRunner()
    artifacts = ArtifactStore("ui-trusted-node", root=tmp_path / "artifacts")

    result, _screenshot, evidence = UIVerificationRunner(
        artifact_store=artifacts,
        process_runner=process,  # type: ignore[arg-type]
    ).run(
        _request(repository),
        command_id="ui-trusted-node",
        repository=repository,
    )

    assert result.status is CommandStatus.PASSED
    assert artifacts.verify(evidence)
    assert len(process.calls) == 1
    argv, call = process.calls[0]
    assert Path(argv[0]) == trusted.resolve(strict=True)
    assert Path(argv[0]).is_absolute()
    assert call["cwd"] == repository
