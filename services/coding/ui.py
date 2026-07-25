from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from services.common import ROOT
from services.coding.artifacts import ArtifactStore
from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    CodingTaskRequestV1,
    CommandResultV1,
    CommandStatus,
)
from services.coding.process import ProcessRunner


class UIVerificationError(RuntimeError):
    pass


def _trusted_node(repository: Path) -> Path:
    try:
        repository_root = repository.resolve(strict=True)
    except OSError as exc:
        raise UIVerificationError("UI verification repository is unavailable") from exc

    executable_name = "node.exe" if os.name == "nt" else "node"
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        if not directory.is_absolute():
            continue
        candidate = directory / executable_name
        try:
            absolute = Path(os.path.abspath(candidate))
            info = candidate.lstat()
            canonical = candidate.resolve(strict=True)
            canonical.relative_to(repository_root)
        except ValueError:
            pass
        except OSError:
            continue
        else:
            # A repository-local executable is untrusted even when the
            # repository itself was inserted into the host PATH.
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            os.path.normcase(str(absolute)) != os.path.normcase(str(canonical))
            or candidate.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) > 1
            or (os.name != "nt" and not os.access(canonical, os.X_OK))
        ):
            continue
        return canonical
    raise UIVerificationError("trusted Node executable is unavailable")


class UIVerificationRunner:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        process_runner: ProcessRunner | None = None,
        policy: CodingPolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or get_coding_policy()
        self.process_runner = process_runner or ProcessRunner(self.policy)

    @staticmethod
    def _validate(request: CodingTaskRequestV1) -> tuple[str, str, str]:
        if not request.ui_url or not request.ui_selector or request.ui_expected_text is None:
            raise UIVerificationError("UI verification contract is incomplete")
        parsed = urlsplit(request.ui_url)
        host = (parsed.hostname or "").casefold()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or host not in {"127.0.0.1", "::1", "localhost"}
            or not (1024 <= port <= 65535)
        ):
            raise UIVerificationError("coding UI verification permits loopback high ports only")
        return request.ui_url, request.ui_selector, request.ui_expected_text

    def run(
        self,
        request: CodingTaskRequestV1,
        *,
        command_id: str,
        repository: Path,
        cancel_event: threading.Event | None = None,
    ) -> tuple[CommandResultV1, ArtifactReferenceV1 | None, ArtifactReferenceV1]:
        url, selector, expected = self._validate(request)
        node = _trusted_node(repository)
        script = ROOT / "services" / "coding" / "ui_check.mjs"
        screenshot = self.artifact_store.task_root / "runtime" / f"ui-{secrets.token_hex(8)}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        argv = [str(node), str(script), url, selector, expected, str(screenshot)]
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        outcome = self.process_runner.run(
            argv,
            cwd=repository,
            timeout_seconds=min(120, self.policy.verification_timeout_seconds),
            cancel_event=cancel_event,
        )
        finished_at = datetime.now(timezone.utc)
        screenshot_artifact: ArtifactReferenceV1 | None = None
        try:
            if outcome.status is CommandStatus.PASSED and screenshot.is_file():
                screenshot_artifact = self.artifact_store.write_bytes(
                    kind=ArtifactKind.SCREENSHOT,
                    payload=screenshot.read_bytes(),
                    suffix=".png",
                    media_type="image/png",
                    producer="coding-playwright",
                    secret_scan=False,
                    maximum=8 * 1024 * 1024,
                )
        finally:
            screenshot.unlink(missing_ok=True)
        try:
            evidence_value = json.loads(outcome.stdout.strip()) if outcome.stdout.strip() else {}
        except json.JSONDecodeError:
            evidence_value = {"status": outcome.status.value, "output": "unstructured"}
        evidence = self.artifact_store.write_json(
            kind=ArtifactKind.UI_EVIDENCE,
            value={
                "status": outcome.status.value,
                "evidence": evidence_value,
                "screenshot_artifact_id": screenshot_artifact.artifact_id if screenshot_artifact else None,
            },
            producer="coding-playwright",
        )
        result = CommandResultV1(
            command_id=command_id,
            argv=[
                "node",
                "services/coding/ui_check.mjs",
                "[loopback-url]",
                "[selector]",
                "[expected-text]",
                "[task-screenshot]",
            ],
            cwd=str(repository.resolve(strict=True)),
            purpose="Verify the loopback UI with Playwright and capture screenshot evidence.",
            status=outcome.status,
            exit_code=outcome.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_artifact_id=evidence.artifact_id,
            summary=("UI verification passed" if outcome.status is CommandStatus.PASSED else "UI verification failed"),
        )
        return result, screenshot_artifact, evidence


__all__ = ["UIVerificationError", "UIVerificationRunner"]
