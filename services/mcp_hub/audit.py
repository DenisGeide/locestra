from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from services.common import ROOT
from services.knowledge.privacy import detect_secret


AUDIT_LOG_PATH = ROOT / "logs" / "mcp-calls.jsonl"
_AUDIT_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SECRET_ID = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|bearer|token|secret|password|credential)"
)


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if not _SAFE_ID.fullmatch(value) or _SECRET_ID.search(value):
        return "redacted"
    try:
        secret_reason = detect_secret(value.encode("utf-8", errors="strict"))
    except Exception:
        # Audit metadata is an egress boundary. Scanner failure must never
        # permit an unchecked identifier to cross it.
        return "redacted"
    return "redacted" if secret_reason is not None else value


def write_call_audit(
    *,
    server_id: str,
    tool_name: str,
    duration_ms: int,
    status: Literal["ok", "failed", "timed_out", "cancelled", "rejected"],
    attempt: int,
    reason_code: str,
    request_id: str | None = None,
    task_id: str | None = None,
    path: Path = AUDIT_LOG_PATH,
) -> None:
    """Append metadata only. Arguments, results, commands and exceptions are forbidden."""

    event = {
        "schema_version": "1.0",
        "event": "mcp_tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server_id": _safe_identifier(server_id),
        "tool": _safe_identifier(tool_name),
        "duration_ms": max(0, min(int(duration_ms), 86_400_000)),
        "status": status,
        "attempt": max(1, min(int(attempt), 2)),
        "reason_code": _safe_identifier(reason_code) or "invalid",
        "request_id": _safe_identifier(request_id),
        "task_id": _safe_identifier(task_id),
    }
    encoded = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > 2_048:
        raise ValueError("MCP audit event exceeded its metadata-only bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            _lock_descriptor(descriptor)
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if not isinstance(written, int) or written <= 0:
                        raise OSError("MCP audit append made no progress")
                    remaining = remaining[written:]
            finally:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)
