from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

from services.contracts import PlanV1, RouteDecisionV1

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:authorization|api[_-]?key|token|password|secret)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{12,}\b"),
)


def redact_bounded(value: str, limit: int = 2_000) -> str:
    redacted = value.replace("\x00", "")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = redacted.strip()
    if len(redacted) > limit:
        redacted = redacted[: limit - 18] + "… [truncated]"
    return redacted or "unspecified"


def collect_modified_files(project: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=project,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    files: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        candidate = line[3:].strip()
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        if candidate and candidate not in files:
            files.append(candidate[:4_096])
    return files[:10_000]


def _bullets(values: Iterable[str], *, empty: str = "- None recorded.") -> list[str]:
    bounded = [f"- {redact_bounded(value)}" for value in values]
    return bounded or [empty]


def ensure_codex_handoff(
    *,
    inbox_dir: Path,
    task_id: str,
    plan: PlanV1,
    decision: RouteDecisionV1,
    project: str | None,
    worktree: str | None,
    errors: list[str],
    modified_files: list[str],
    command_summaries: list[str],
    artifact_refs: list[str],
) -> Path:
    """Create exactly one bounded, redacted handoff bundle for a task."""

    inbox_dir.mkdir(parents=True, exist_ok=True)
    bundle = inbox_dir / f"{task_id}-codex.md"
    if bundle.exists():
        return bundle
    lines = [
        "# Codex task bundle",
        "",
        f"- Task ID: `{task_id}`",
        f"- Project: `{project or 'not specified'}`",
        f"- Worktree: `{worktree or project or 'not specified'}`",
        f"- Routing policy: `{decision.policy_version or 'unknown'}`",
        f"- Requested execution mode: `{decision.execution_mode.value}`",
        "",
        "## Original goal",
        "",
        redact_bounded(plan.goal, 262_144),
        "",
        "## Constraints",
        "",
        *_bullets(plan.constraints),
        "",
        "## Acceptance criteria",
        "",
        *_bullets(plan.acceptance_criteria),
        "",
        "## Verification plan",
        "",
        *_bullets(plan.verification_plan),
        "",
        "## Local attempts and errors",
        "",
        *_bullets(errors),
        "",
        "## Command summaries",
        "",
        *_bullets(command_summaries),
        "",
        "## Modified files",
        "",
        *_bullets(modified_files),
        "",
        "## Artifacts",
        "",
        *_bullets(artifact_refs),
        "",
        "## Memory record references",
        "",
        *_bullets(plan.memory_record_refs),
        "",
        "## Execution contract",
        "",
        "- Re-inspect the current worktree; do not trust summaries as source code.",
        "- Preserve the original constraints and acceptance criteria.",
        "- Follow applicable AGENTS.md files and run bounded relevant verification.",
        "- Do not push, deploy, or expand the workspace scope.",
    ]
    encoded = "\n".join(lines).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(bundle, flags, 0o600)
    except FileExistsError:
        return bundle
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        bundle.unlink(missing_ok=True)
        raise
    return bundle
