from __future__ import annotations

import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

import psutil
import pytest

from services.coding.artifacts import ArtifactPolicyError, ArtifactStore
from services.coding.contracts import ArtifactKind, CommandStatus
from services.coding.process import (
    ProcessPolicyError,
    ProcessRunner,
    safe_child_environment,
)


def _process_is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def test_child_environment_is_allowlisted_and_secret_overrides_are_rejected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden-parent-secret")
    monkeypatch.setenv("STAGE005_UNLISTED", "must-not-cross")

    environment = safe_child_environment({"STAGE005_SAFE_OVERRIDE": "visible"})

    assert "OPENAI_API_KEY" not in environment
    assert "STAGE005_UNLISTED" not in environment
    assert environment["STAGE005_SAFE_OVERRIDE"] == "visible"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"

    with pytest.raises(ProcessPolicyError, match="secret-shaped"):
        safe_child_environment({"SERVICE_TOKEN": "synthetic"})
    with pytest.raises(ProcessPolicyError, match="invalid"):
        safe_child_environment({"BAD=KEY": "value"})


def test_process_runner_passes_only_explicit_safe_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden-parent-secret")
    runner = ProcessRunner()
    script = (
        "import os; "
        "print(os.environ.get('STAGE005_SAFE_OVERRIDE', 'missing')); "
        "print(os.environ.get('OPENAI_API_KEY', 'absent'))"
    )

    outcome = runner.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout_seconds=10,
        environment={"STAGE005_SAFE_OVERRIDE": "visible"},
    )

    assert outcome.status is CommandStatus.PASSED
    assert outcome.exit_code == 0
    assert outcome.stdout.splitlines() == ["visible", "absent"]
    assert "forbidden-parent-secret" not in outcome.stdout + outcome.stderr


def test_process_runner_pre_cancel_does_not_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cancel = threading.Event()
    cancel.set()

    def forbidden_spawn(*args: object, **kwargs: object):
        raise AssertionError("a pre-cancelled process must not be spawned")

    monkeypatch.setattr("services.coding.process.subprocess.Popen", forbidden_spawn)
    outcome = ProcessRunner().run(
        [sys.executable, "-c", "raise SystemExit(99)"],
        cwd=tmp_path,
        timeout_seconds=5,
        input_text="bounded prompt",
        cancel_event=cancel,
    )

    assert outcome.status is CommandStatus.CANCELLED
    assert outcome.exit_code is None
    assert outcome.stdout == ""
    assert outcome.stderr == ""


def test_process_stdin_does_not_bypass_timeout_when_child_never_reads(
    tmp_path: Path,
):
    runner = ProcessRunner()
    started = time.monotonic()

    outcome = runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.2,
        input_text="x" * (1024 * 1024),
    )

    elapsed = time.monotonic() - started
    assert outcome.status is CommandStatus.TIMED_OUT
    assert outcome.exit_code is None
    assert elapsed < 2.5


def test_process_streams_output_while_delivering_stdin_without_deadlock(
    tmp_path: Path,
):
    payload = "y" * 65_536
    child = (
        "import sys; "
        "sys.stdout.buffer.write(b'x' * 65536); "
        "sys.stdout.buffer.flush(); "
        "data=sys.stdin.buffer.read(); "
        "sys.stderr.write(str(len(data)))"
    )

    outcome = ProcessRunner().run(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        timeout_seconds=5,
        input_text=payload,
    )

    assert outcome.status is CommandStatus.PASSED
    assert outcome.exit_code == 0
    assert outcome.stderr == str(len(payload.encode("utf-8")))
    assert outcome.stdout == "x" * 20_000


def test_process_rejects_success_after_child_closes_partially_consumed_stdin(
    tmp_path: Path,
):
    child = "import sys; sys.stdin.buffer.read(1); sys.stdin.close()"

    outcome = ProcessRunner().run(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        timeout_seconds=5,
        input_text="z" * (1024 * 1024),
    )

    assert outcome.status is CommandStatus.FAILED
    assert "stdin was not delivered completely" in outcome.stderr


@pytest.mark.required_e2e
def test_process_timeout_terminates_spawned_child_tree(tmp_path: Path):
    runner = ProcessRunner()
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print(child.pid, flush=True); time.sleep(30)"
    )

    outcome = runner.run(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        timeout_seconds=0.3,
    )

    assert outcome.status is CommandStatus.TIMED_OUT
    assert outcome.exit_code is None
    child_pid = int(outcome.stdout.strip().splitlines()[0])
    deadline = time.monotonic() + 5
    while _process_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_is_running(child_pid)


def test_process_success_terminates_descendant_before_it_can_mutate_worktree(
    tmp_path: Path,
):
    runner = ProcessRunner()
    delayed_write = tmp_path / "MUTATED_AFTER_PARENT_EXIT.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.0); "
        "pathlib.Path(sys.argv[1]).write_text('unsafe', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r},sys.argv[1]]); "
        "print(child.pid, flush=True); time.sleep(0.35)"
    )

    outcome = runner.run(
        [sys.executable, "-c", parent_code, str(delayed_write)],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert outcome.status is CommandStatus.PASSED
    child_pid = int(outcome.stdout.strip().splitlines()[0])
    deadline = time.monotonic() + 5
    while _process_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_is_running(child_pid)
    time.sleep(1.1)
    assert not delayed_write.exists()


def test_artifact_hash_verification_and_scope_are_exact(tmp_path: Path):
    store = ArtifactStore("artifact-task", root=tmp_path)
    payload = b"bounded artifact evidence\n"
    reference = store.write_bytes(
        kind=ArtifactKind.DIFF,
        payload=payload,
        suffix=".patch",
        media_type="text/x-diff",
        producer="unit-test",
    )

    assert reference.sha256 == sha256(payload).hexdigest()
    assert reference.size_bytes == len(payload)
    assert Path(reference.path).read_bytes() == payload
    assert store.verify(reference) is True

    Path(reference.path).write_bytes(payload + b"tampered")
    assert store.verify(reference) is False

    outside = tmp_path / "outside.txt"
    outside.write_bytes(payload)
    escaped = reference.model_copy(update={"path": str(outside)})
    assert store.verify(escaped) is False


def test_artifact_identity_binds_provenance_media_and_logical_occurrence(
    tmp_path: Path,
):
    store = ArtifactStore("artifact-identity-task", root=tmp_path)
    payload = b"[no output]"

    first = store.write_bytes(
        kind=ArtifactKind.COMMAND_OUTPUT,
        payload=payload,
        suffix=".txt",
        media_type="text/plain",
        producer="coding-verification",
        occurrence_id="a1-verify-1",
    )
    repeated = store.write_bytes(
        kind=ArtifactKind.COMMAND_OUTPUT,
        payload=payload,
        suffix=".TXT",
        media_type="text/plain",
        producer="coding-verification",
        occurrence_id="a1-verify-1",
    )
    second_occurrence = store.write_bytes(
        kind=ArtifactKind.COMMAND_OUTPUT,
        payload=payload,
        suffix=".txt",
        media_type="text/plain",
        producer="coding-verification",
        occurrence_id="a1-verify-2",
    )
    different_producer = store.write_bytes(
        kind=ArtifactKind.COMMAND_OUTPUT,
        payload=payload,
        suffix=".txt",
        media_type="text/plain",
        producer="qwen-code",
        occurrence_id="a1-verify-1",
    )
    different_media = store.write_bytes(
        kind=ArtifactKind.COMMAND_OUTPUT,
        payload=payload,
        suffix=".json",
        media_type="application/json",
        producer="coding-verification",
        occurrence_id="a1-verify-1",
    )

    assert first.artifact_id == repeated.artifact_id
    assert len(first.artifact_id.rsplit("-", 1)[1]) == 32
    assert len(
        {
            first.artifact_id,
            second_occurrence.artifact_id,
            different_producer.artifact_id,
            different_media.artifact_id,
        }
    ) == 4
    assert {
        first.sha256,
        second_occurrence.sha256,
        different_producer.sha256,
        different_media.sha256,
    } == {sha256(payload).hexdigest()}
    assert all(
        store.verify(reference)
        for reference in (
            first,
            repeated,
            second_occurrence,
            different_producer,
            different_media,
        )
    )

    with pytest.raises(ArtifactPolicyError, match="producer"):
        store.write_bytes(
            kind=ArtifactKind.COMMAND_OUTPUT,
            payload=payload,
            suffix=".txt",
            media_type="text/plain",
            producer="unsafe producer",
        )
    with pytest.raises(ArtifactPolicyError, match="occurrence"):
        store.write_bytes(
            kind=ArtifactKind.COMMAND_OUTPUT,
            payload=payload,
            suffix=".txt",
            media_type="text/plain",
            producer="coding-verification",
            occurrence_id="../unsafe",
        )


def test_artifact_secret_and_path_shaped_suffix_are_rejected_before_publication(tmp_path: Path):
    store = ArtifactStore("privacy-task", root=tmp_path)
    before = list(store.artifact_root.iterdir())
    synthetic = b'authorization = "Bearer fixtureSecret1234567890"'

    with pytest.raises(ArtifactPolicyError, match="privacy policy"):
        store.write_bytes(
            kind=ArtifactKind.COMMAND_OUTPUT,
            payload=synthetic,
            suffix=".txt",
            media_type="text/plain",
            producer="unit-test",
        )
    assert list(store.artifact_root.iterdir()) == before

    with pytest.raises(ArtifactPolicyError, match="suffix"):
        store.write_bytes(
            kind=ArtifactKind.DIFF,
            payload=b"safe",
            suffix="../escape",
            media_type="text/plain",
            producer="unit-test",
        )
