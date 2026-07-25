from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.coding.semantic_review import (
        LocalSemanticReviewConfig,
        LocalSemanticReviewResult,
        SemanticReviewSubject,
    )

from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    CodingMode,
    CodingTaskRequestV1,
    CommandResultV1,
    CommandStatus,
    ExecutorKind,
    ReviewFindingV1,
    ReviewResultV1,
    ReviewSeverity,
    ReviewVerdict,
    WorktreeRecordV1,
)
from services.coding.git import (
    RepositoryIdentity,
    git_diff,
    git_status_paths,
    resolve_repository,
    run_git,
    scan_changed_content,
)
from services.knowledge.privacy import detect_secret
from services.coding.verification import is_semantic_verification_argv


class ReviewPolicyError(RuntimeError):
    pass


def _finding(
    severity: ReviewSeverity,
    code: str,
    scenario: str,
    remediation: str,
    *,
    file: str | None = None,
    line: int | None = None,
) -> ReviewFindingV1:
    return ReviewFindingV1(
        severity=severity,
        code=code,
        file=file,
        line=line,
        failure_scenario=scenario,
        remediation=remediation,
    )


def _safe_expected_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ReviewPolicyError("expected diff path is outside repository scope")
    normalized = normalized.rstrip("/")
    candidate = PurePosixPath(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewPolicyError("expected diff path is outside repository scope")
    return posixpath.normpath(normalized)


def _matches_scope(path: str, expected: tuple[str, ...]) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    candidate = normalized.casefold() if os.name == "nt" else normalized
    return any(
        candidate == (item.casefold() if os.name == "nt" else item)
        or candidate.startswith(f"{item.casefold() if os.name == 'nt' else item}/")
        for item in expected
    )


def _safe_review_file(value: str, repository: Path | None) -> str:
    normalized = value.strip().replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ReviewPolicyError(
            "specialized review finding path escapes repository scope"
        )
    relative = posixpath.normpath(normalized)
    if relative in {"", "."} or relative.startswith("../"):
        raise ReviewPolicyError(
            "specialized review finding path escapes repository scope"
        )
    if repository is not None:
        try:
            root = repository.resolve(strict=True)
            resolved = (root / Path(relative)).resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ReviewPolicyError(
                "specialized review finding path escapes repository scope"
            ) from exc
    return PurePosixPath(relative).as_posix()


class _DuplicateCodexReviewKey(ValueError):
    pass


def _reject_duplicate_codex_review_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateCodexReviewKey(key)
        value[key] = item
    return value


def _reject_non_finite_codex_number(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _exact_object_keys(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReviewPolicyError("Codex review JSON object has an invalid type")
    optional = optional or set()
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ReviewPolicyError("Codex review JSON object has invalid fields")
    return value


def _bounded_review_string(
    value: object,
    *,
    field: str,
    maximum: int,
    minimum: int = 1,
) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ReviewPolicyError(f"Codex review JSON {field} is invalid")
    return value


def _bounded_review_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewPolicyError(f"Codex review JSON {field} is invalid")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ReviewPolicyError(f"Codex review JSON {field} is invalid") from exc
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise ReviewPolicyError(f"Codex review JSON {field} is invalid")
    return numeric


def _bounded_review_integer(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ReviewPolicyError(f"Codex review JSON {field} is invalid")
    return value


def _safe_absolute_review_file(value: object, repository: Path | None) -> str:
    raw = _bounded_review_string(
        value,
        field="absolute_file_path",
        maximum=4_096,
    )
    if raw != raw.strip() or any(ord(char) < 32 for char in raw):
        raise ReviewPolicyError("Codex review JSON path is invalid")
    if repository is None:
        raise ReviewPolicyError("Codex review JSON path has no repository boundary")
    normalized = raw.replace("\\", "/")
    if normalized.startswith(("//", "//?/", "//./")):
        raise ReviewPolicyError("Codex review JSON path escapes repository scope")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ReviewPolicyError("Codex review JSON path must be absolute")
    try:
        root = repository.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        relative_path = resolved.relative_to(root)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ReviewPolicyError(
            "Codex review JSON path escapes repository scope"
        ) from exc
    if not relative_path.parts or relative_path.parts[0].casefold() == ".git":
        raise ReviewPolicyError("Codex review JSON path escapes repository scope")
    return PurePosixPath(*relative_path.parts).as_posix()


def _parse_codex_json_review(
    codex_summary: str,
    *,
    repository: Path | None,
) -> tuple[list[ReviewFindingV1], bool]:
    """Parse Codex 0.144.1's built-in strict review JSON contract."""

    try:
        payload = json.loads(
            codex_summary,
            object_pairs_hook=_reject_duplicate_codex_review_keys,
            parse_constant=_reject_non_finite_codex_number,
        )
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ReviewPolicyError("Codex review JSON is invalid") from exc
    envelope = _exact_object_keys(
        payload,
        required={
            "findings",
            "overall_correctness",
            "overall_explanation",
            "overall_confidence_score",
        },
    )
    raw_findings = envelope["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > 256:
        raise ReviewPolicyError("Codex review JSON findings are invalid")
    overall_correctness = _bounded_review_string(
        envelope["overall_correctness"],
        field="overall_correctness",
        maximum=64,
    )
    if overall_correctness not in {"patch is correct", "patch is incorrect"}:
        raise ReviewPolicyError("Codex review JSON verdict is invalid")
    _bounded_review_string(
        envelope["overall_explanation"],
        field="overall_explanation",
        maximum=4_096,
    )
    _bounded_review_number(
        envelope["overall_confidence_score"],
        field="overall_confidence_score",
    )
    if bool(raw_findings) != (overall_correctness == "patch is incorrect"):
        raise ReviewPolicyError("Codex review JSON verdict is inconsistent")

    severities = {
        0: ReviewSeverity.CRITICAL,
        1: ReviewSeverity.HIGH,
        2: ReviewSeverity.MEDIUM,
        3: ReviewSeverity.LOW,
    }
    parsed: list[ReviewFindingV1] = []
    for raw_finding in raw_findings:
        finding = _exact_object_keys(
            raw_finding,
            required={"title", "body", "confidence_score", "code_location"},
            optional={"priority"},
        )
        title = _bounded_review_string(
            finding["title"], field="finding.title", maximum=80
        )
        title_match = re.fullmatch(r"\[(P[0-3])\]\s+(.+)", title)
        if title_match is None:
            raise ReviewPolicyError("Codex review JSON title has no priority marker")
        title_priority = int(title_match.group(1)[1])
        if "priority" in finding:
            priority = _bounded_review_integer(
                finding["priority"],
                field="finding.priority",
                minimum=0,
                maximum=3,
            )
            if priority != title_priority:
                raise ReviewPolicyError(
                    "Codex review JSON priority fields are inconsistent"
                )
        else:
            priority = title_priority
        body = _bounded_review_string(
            finding["body"], field="finding.body", maximum=4_096
        )
        _bounded_review_number(
            finding["confidence_score"], field="finding.confidence_score"
        )
        location = _exact_object_keys(
            finding["code_location"],
            required={"absolute_file_path", "line_range"},
        )
        file_value = _safe_absolute_review_file(
            location["absolute_file_path"], repository
        )
        line_range = _exact_object_keys(
            location["line_range"], required={"start", "end"}
        )
        line_start = _bounded_review_integer(
            line_range["start"],
            field="line_range.start",
            minimum=1,
            maximum=2_147_483_647,
        )
        line_end = _bounded_review_integer(
            line_range["end"],
            field="line_range.end",
            minimum=1,
            maximum=2_147_483_647,
        )
        if line_end < line_start or line_end - line_start + 1 > 10:
            raise ReviewPolicyError("Codex review JSON line range is invalid")
        clean_title = title_match.group(2)
        slug = (
            re.sub(r"[^a-z0-9]+", ".", clean_title.casefold()).strip(".")[:80]
            or "finding"
        )
        parsed.append(
            ReviewFindingV1(
                severity=severities[priority],
                code=f"codex.p{priority}.{slug}",
                file=file_value,
                line=line_start,
                failure_scenario=body,
                remediation=(
                    f"Resolve the specialized review finding: {clean_title}"[:4_096]
                ),
            )
        )
    return parsed, overall_correctness == "patch is correct"


class DeterministicReviewer:
    """Independent policy/evidence gate; it never trusts an executor summary."""

    reviewer = ExecutorKind.DETERMINISTIC

    def __init__(self, *, policy: CodingPolicy | None = None) -> None:
        self.policy = policy or get_coding_policy()

    def review(
        self,
        *,
        request: CodingTaskRequestV1,
        source_snapshot: RepositoryIdentity,
        target_repository: Path,
        worktree: WorktreeRecordV1 | None,
        command_results: list[CommandResultV1],
        required_command_ids: set[str],
        read_only_worktree_unchanged: bool = True,
    ) -> ReviewResultV1:
        findings: list[ReviewFindingV1] = []

        live_source = resolve_repository(
            str(source_snapshot.canonical_root),
            excluded_refs=source_snapshot.excluded_git_refs,
        )
        if live_source.base_commit != source_snapshot.base_commit:
            findings.append(
                _finding(
                    ReviewSeverity.CRITICAL,
                    "source.head_changed",
                    "The user's source worktree moved to another HEAD while the isolated task was running.",
                    "Stop and let the user reconcile the source repository before resuming.",
                )
            )
        metadata_changed = (
            live_source.git_metadata_fingerprint
            != source_snapshot.git_metadata_fingerprint
        )
        if metadata_changed:
            findings.append(
                _finding(
                    ReviewSeverity.CRITICAL,
                    "source.git_metadata_changed",
                    "Shared Git configuration, tags, or remote-tracking refs changed while the isolated task was running.",
                    "Stop before approval/commit; inspect remote configuration and protected refs, then restart from a confirmed snapshot.",
                )
            )
        if live_source.dirty_fingerprint != source_snapshot.dirty_fingerprint:
            findings.append(
                _finding(
                    ReviewSeverity.CRITICAL,
                    "source.dirty_work_changed",
                    "The user's pre-existing uncommitted work changed during task execution.",
                    "Stop; preserve the user worktree and resume only from a newly confirmed snapshot.",
                )
            )

        modified = git_status_paths(target_repository)
        diff = b""
        diff_available = request.mode is CodingMode.READ_ONLY
        if worktree is None:
            findings.append(
                _finding(
                    ReviewSeverity.CRITICAL,
                    "worktree.missing",
                    "The reviewed result has no isolated task worktree.",
                    "Create and register an owned task worktree before execution.",
                )
            )
        else:
            try:
                target_matches = Path(worktree.worktree_path).resolve(
                    strict=True
                ) == target_repository.resolve(strict=True)
                source_matches = Path(worktree.source_repository).resolve(
                    strict=True
                ) == source_snapshot.canonical_root.resolve(strict=True)
            except (OSError, ValueError):
                target_matches = False
                source_matches = False
            if (
                not target_matches
                or not source_matches
                or worktree.base_commit != source_snapshot.base_commit
            ):
                findings.append(
                    _finding(
                        ReviewSeverity.CRITICAL,
                        "worktree.identity_mismatch",
                        "The registered worktree does not match the reviewed target, source repository, and base commit.",
                        "Stop and recreate the owned worktree from the recorded source snapshot.",
                    )
                )
            head = (
                run_git(
                    target_repository,
                    ["rev-parse", "--verify", "HEAD"],
                    max_output_bytes=16_384,
                )
                .stdout.decode("ascii", errors="strict")
                .strip()
                .casefold()
            )
            if head != worktree.base_commit:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "executor.commit_forbidden",
                        "The executor changed HEAD before the independent review/commit gate.",
                        "Discard the executor commit and let the Coding Engine create the sole optional local commit.",
                    )
                )
        if request.mode is CodingMode.READ_ONLY:
            # A read-only target may already have user changes.  The source
            # fingerprint comparison above proves the complete snapshot stayed
            # unchanged, so no project command or clean-tree assumption is used.
            if command_results:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "read_only.project_command",
                        "A read-only inspect/list/review task recorded a project command.",
                        "Repeat the task without executing project scripts or verifiers.",
                    )
                )
            if modified:
                findings.append(
                    _finding(
                        ReviewSeverity.CRITICAL,
                        "read_only.worktree_changed",
                        "A read-only task changed files in its isolated worktree.",
                        "Discard the isolated changes and repeat with read-only tools only.",
                        file=modified[0],
                    )
                )
            if not read_only_worktree_unchanged and not any(
                item.code == "read_only.worktree_changed" for item in findings
            ):
                findings.append(
                    _finding(
                        ReviewSeverity.CRITICAL,
                        "read_only.worktree_changed",
                        "A read-only task changed the isolated worktree, including ignored files or Git HEAD.",
                        "Preserve the worktree for audit and repeat in a proven read-only sandbox.",
                    )
                )
        else:
            if not modified:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "diff.empty",
                        "The write task produced no reviewable file change.",
                        "Re-run with a concrete minimal change or mark the task blocked with evidence.",
                    )
                )
            try:
                scan_changed_content(
                    target_repository, max_bytes=self.policy.max_diff_bytes
                )
                diff = git_diff(target_repository, max_bytes=self.policy.max_diff_bytes)
                diff_available = True
            except Exception:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "diff.unavailable",
                        "The complete bounded diff could not be rendered.",
                        "Reduce the change scope and regenerate a bounded diff before review.",
                    )
                )
            check = run_git(
                target_repository,
                ["diff", "--check", "HEAD", "--"],
                check=False,
                max_output_bytes=256 * 1024,
            )
            added_trailing_whitespace = bool(
                diff and re.search(rb"(?m)^\+(?!\+\+\+).*?[\t ]\r?$", diff)
            )
            if check.returncode != 0 or added_trailing_whitespace:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "diff.whitespace_error",
                        "Git detected whitespace errors in the task diff.",
                        "Correct the reported whitespace errors and rerun verification.",
                    )
                )

            expected = tuple(
                _safe_expected_path(item) for item in request.expected_diff_paths
            )
            unexpected = [
                item
                for item in modified
                if expected and not _matches_scope(item, expected)
            ]
            for path in unexpected[:64]:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "diff.unexpected_file",
                        "The executor modified a file outside the task's declared diff scope.",
                        "Revert this file in the isolated worktree or explicitly expand the task scope.",
                        file=path,
                    )
                )
            forbidden = tuple(
                _safe_expected_path(item) for item in request.forbidden_diff_paths
            )
            forbidden_changes = [
                item for item in modified if _matches_scope(item, forbidden)
            ]
            for path in forbidden_changes[:64]:
                findings.append(
                    _finding(
                        ReviewSeverity.HIGH,
                        "diff.forbidden_file",
                        "The executor modified a file that the task explicitly protected from changes.",
                        "Revert this file in the isolated worktree; a negative path constraint cannot be overridden by the executor.",
                        file=path,
                    )
                )
            privacy_finding = detect_secret(diff) if diff else None
            if privacy_finding:
                findings.append(
                    _finding(
                        ReviewSeverity.CRITICAL,
                        "diff.secret_detected",
                        f"The bounded diff matched privacy rule {privacy_finding}.",
                        "Remove the credential or secret material; do not persist or hand it off.",
                    )
                )

        by_id = {item.command_id: item for item in command_results}
        missing = sorted(required_command_ids.difference(by_id))
        for command_id in missing:
            findings.append(
                _finding(
                    ReviewSeverity.HIGH,
                    "verification.missing",
                    f"Required verification {command_id} has no recorded result.",
                    "Run the required allowlisted verifier in the task worktree.",
                )
            )
        failed = [
            item
            for item in command_results
            if item.command_id in required_command_ids
            and item.status is not CommandStatus.PASSED
        ]
        for result in failed:
            findings.append(
                _finding(
                    ReviewSeverity.HIGH,
                    "verification.failed",
                    f"Required verification {result.command_id} ended as {result.status.value}.",
                    "Fix the failure and rerun this exact verifier before commit.",
                )
            )

        semantic_results = [
            item
            for item in command_results
            if item.command_id in required_command_ids
            and is_semantic_verification_argv(item.argv)
        ]
        semantic_evidence_passed = bool(semantic_results) and all(
            item.status is CommandStatus.PASSED for item in semantic_results
        )
        if request.mode is CodingMode.WRITE and not semantic_evidence_passed:
            findings.append(
                _finding(
                    ReviewSeverity.HIGH,
                    "requirements.evidence_missing",
                    "The write result has no passed semantic test, build, typecheck, lint, or UI verifier for its acceptance criteria.",
                    "Add a bounded repository verifier that exercises the requested behavior and rerun before approval.",
                )
            )

        blocking = any(
            item.severity in {ReviewSeverity.HIGH, ReviewSeverity.CRITICAL}
            for item in findings
        )
        verdict = ReviewVerdict.REJECTED if blocking else ReviewVerdict.APPROVED
        reviewed_material = (
            b"\0".join(
                item.encode("utf-8")
                for item in (
                    request.task_id,
                    source_snapshot.base_commit,
                    source_snapshot.git_metadata_fingerprint,
                    *request.expected_diff_paths,
                    *request.forbidden_diff_paths,
                    *modified,
                    *(sorted(required_command_ids)),
                )
            )
            + b"\0"
            + hashlib.sha256(diff).digest()
        )
        reviewer_id = f"review-{hashlib.sha256(reviewed_material).hexdigest()[:16]}"
        return ReviewResultV1(
            reviewer_id=reviewer_id,
            reviewer=self.reviewer,
            verdict=verdict,
            findings=findings,
            # Structural review cannot prove task semantics. A separate local
            # semantic or Codex review must establish complete requirement coverage.
            checked_requirements=False,
            checked_tests=(
                request.mode is CodingMode.READ_ONLY
                or semantic_evidence_passed
                and not missing
                and not failed
            ),
            checked_diff_scope=diff_available
            and not any(
                item.code
                in {"diff.unavailable", "diff.unexpected_file", "diff.forbidden_file"}
                for item in findings
            ),
            checked_secrets=diff_available
            and not any(item.code == "diff.secret_detected" for item in findings),
            checked_constitution=not any(
                item.code
                in {
                    "source.head_changed",
                    "source.git_metadata_changed",
                    "source.dirty_work_changed",
                    "read_only.project_command",
                    "read_only.worktree_changed",
                    "executor.commit_forbidden",
                    "worktree.missing",
                    "worktree.identity_mismatch",
                }
                for item in findings
            ),
            summary=(
                "Independent evidence gate approved the bounded result."
                if verdict is ReviewVerdict.APPROVED
                else "Independent evidence gate rejected the result; blocking findings remain."
            ),
            reviewed_at=datetime.now(timezone.utc),
        )


__all__ = ["DeterministicReviewer", "ReviewPolicyError"]


def merge_codex_review(
    deterministic: ReviewResultV1,
    *,
    codex_summary: str,
    worktree_unchanged: bool,
    repository: Path | None = None,
) -> ReviewResultV1:
    """Merge structured specialized Codex findings with deterministic gates."""

    findings = list(deterministic.findings)
    codex_verdict = ReviewVerdict.BLOCKED
    # Production Codex 0.144.1 review uses its built-in strict JSON contract.
    # The older closed P0-P3/NO_FINDINGS protocol remains accepted for durable
    # compatibility, while arbitrary prose and malformed/mixed JSON fail closed.
    header = re.compile(
        r"^(?:-\s+)?\[(P[0-3])\]\s+(.+?)\s+[—–-]\s+`?(.+?):(\d+)"
        r"(?:-\d+)?`?(?:(?:\s+[—–-]\s+|:\s*)(.+))?\s*$"
    )
    parsed: list[ReviewFindingV1] = []
    protocol_invalid = False
    protocol_invalid_reason: str | None = None
    json_approved = False
    lines = codex_summary.splitlines()
    index = 0
    severities = {
        "P0": ReviewSeverity.CRITICAL,
        "P1": ReviewSeverity.HIGH,
        "P2": ReviewSeverity.MEDIUM,
        "P3": ReviewSeverity.LOW,
    }
    stripped_summary = codex_summary.strip()
    # The installed Codex review contract is always a top-level JSON object.
    # Legacy standard review findings begin with ``[P0]`` through ``[P3]`` and
    # remain supported by the separate closed text parser below.
    looks_like_json = stripped_summary.startswith("{")
    if looks_like_json:
        try:
            parsed, json_approved = _parse_codex_json_review(
                codex_summary,
                repository=repository,
            )
        except ReviewPolicyError as exc:
            protocol_invalid = True
            protocol_invalid_reason = str(exc)[:512]
    else:
        while index < len(lines):
            match = header.match(lines[index].strip())
            if not match:
                index += 1
                continue
            priority, title, raw_file, raw_line, inline_scenario = match.groups()
            body: list[str] = [inline_scenario] if inline_scenario else []
            index += 1
            while index < len(lines) and not header.match(lines[index].strip()):
                if lines[index].strip():
                    body.append(lines[index].strip())
                index += 1
            try:
                file_value = _safe_review_file(raw_file, repository)
            except ReviewPolicyError:
                protocol_invalid = True
                parsed.clear()
                break
            scenario = " ".join(body)[:4_096] or title[:4_096]
            slug = (
                re.sub(r"[^a-z0-9]+", ".", title.casefold()).strip(".")[:80]
                or "finding"
            )
            parsed.append(
                ReviewFindingV1(
                    severity=severities[priority],
                    code=f"codex.{priority.casefold()}.{slug}",
                    file=file_value[:4_096],
                    line=int(raw_line),
                    failure_scenario=scenario,
                    remediation=f"Resolve the specialized review finding: {title}"[
                        :4_096
                    ],
                )
            )
    if not looks_like_json and not parsed and not protocol_invalid:
        # Codex review formatting varies slightly across runs (em dash,
        # backticks, Markdown links, `file:line:`).  A priority marker is still
        # an unambiguous *finding*, never an approval.  Parse that fail-closed
        # signal even when the strict standard header shape did not match.
        fallback_header = re.compile(r"^(?:-\s+)?\[(P[0-3])\]\s*:?[ \t]*(.+)$")
        fallback_location = re.compile(
            r"(?<![A-Za-z0-9_./-])"
            r"((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+):(\d+)"
        )
        for raw_line_text in lines:
            fallback_match = fallback_header.match(raw_line_text.strip())
            if not fallback_match:
                continue
            priority, remainder = fallback_match.groups()
            if re.search(r"(?:^|[\s`(])(?:[A-Za-z]:[/\\]|/|\.\.[/\\])", remainder):
                protocol_invalid = True
                parsed.clear()
                break
            location = fallback_location.search(remainder.replace("\\", "/"))
            file_value: str | None = None
            line_value: int | None = None
            if location:
                try:
                    file_value = _safe_review_file(location.group(1), repository)
                    line_value = int(location.group(2))
                except (ReviewPolicyError, ValueError):
                    protocol_invalid = True
                    parsed.clear()
                    break
            title = remainder[: location.start() if location else len(remainder)].strip(
                " `[]()—–-:"
            )
            if not title:
                title = "specialized review finding"
            slug = (
                re.sub(r"[^a-z0-9]+", ".", title.casefold()).strip(".")[:80]
                or "finding"
            )
            parsed.append(
                ReviewFindingV1(
                    severity=severities[priority],
                    code=f"codex.{priority.casefold()}.{slug}",
                    file=file_value,
                    line=line_value,
                    failure_scenario=remainder[:4_096],
                    remediation=(
                        f"Resolve the specialized review finding: {title}"[:4_096]
                    ),
                )
            )
    if parsed and not protocol_invalid:
        findings.extend(parsed)
        codex_verdict = ReviewVerdict.REJECTED
        summary = "Specialized Codex review returned actionable findings."
    elif not protocol_invalid and (
        json_approved or codex_summary.strip() == "NO_FINDINGS"
    ):
        codex_verdict = ReviewVerdict.APPROVED
        summary = "Specialized Codex review returned no actionable findings."
    else:
        findings.append(
            _finding(
                ReviewSeverity.HIGH,
                "codex.review_unstructured",
                (
                    "The specialized Codex reviewer output failed strict validation"
                    + (
                        f": {protocol_invalid_reason}"
                        if protocol_invalid_reason
                        else "."
                    )
                ),
                "Repeat specialized review with the strict JSON protocol before commit.",
            )
        )
        summary = "Specialized Codex review output could not be validated."
    if not worktree_unchanged:
        findings.append(
            _finding(
                ReviewSeverity.CRITICAL,
                "codex.review_mutated_worktree",
                "The read-only specialized review changed the task worktree.",
                "Stop, inspect the isolated worktree, and repeat review in a proven read-only sandbox.",
            )
        )
    blocking = any(
        item.severity in {ReviewSeverity.HIGH, ReviewSeverity.CRITICAL}
        for item in findings
    )
    if deterministic.verdict is ReviewVerdict.REJECTED:
        verdict = ReviewVerdict.REJECTED
    elif deterministic.verdict is ReviewVerdict.BLOCKED:
        verdict = ReviewVerdict.BLOCKED
    else:
        verdict = (
            ReviewVerdict.REJECTED
            if blocking or codex_verdict is ReviewVerdict.REJECTED
            else ReviewVerdict.BLOCKED
            if codex_verdict is ReviewVerdict.BLOCKED
            else ReviewVerdict.APPROVED
        )
    reviewer_material = (
        deterministic.reviewer_id.encode("utf-8")
        + b"\0"
        + codex_summary.encode("utf-8")
    )
    reviewer_id = f"codex-review-{hashlib.sha256(reviewer_material).hexdigest()[:12]}"
    return ReviewResultV1(
        reviewer_id=reviewer_id,
        reviewer=ExecutorKind.CODEX_REVIEW,
        verdict=verdict,
        findings=findings,
        checked_requirements=(
            not protocol_invalid
            and (
                bool(parsed) or json_approved or codex_summary.strip() == "NO_FINDINGS"
            )
            and worktree_unchanged
        ),
        checked_tests=deterministic.checked_tests,
        checked_diff_scope=deterministic.checked_diff_scope,
        checked_secrets=deterministic.checked_secrets,
        checked_constitution=deterministic.checked_constitution and worktree_unchanged,
        summary=summary,
        reviewed_at=datetime.now(timezone.utc),
    )


__all__.append("merge_codex_review")


def merge_local_semantic_review(
    deterministic: ReviewResultV1,
    *,
    subject: "SemanticReviewSubject",
    semantic_result: "LocalSemanticReviewResult",
    evidence_artifact: ArtifactReferenceV1,
    worktree_unchanged: bool,
    semantic_config: "LocalSemanticReviewConfig | None" = None,
) -> ReviewResultV1:
    """Merge an attested local semantic result with deterministic evidence gates.

    The semantic evidence must first be persisted by ``ArtifactStore``. The resulting
    artifact reference is part of the reviewer identity, preventing a response, subject,
    attestation, or evidence-artifact substitution after review.
    """

    from services.coding.semantic_review import (
        SEMANTIC_REVIEW_PRODUCER,
        LocalSemanticReviewResult,
        SemanticReviewBlocked,
        validate_semantic_result,
    )

    def blocked(code: str, scenario: str) -> ReviewResultV1:
        claimed_artifact_id = getattr(evidence_artifact, "artifact_id", "missing")
        if not isinstance(claimed_artifact_id, str):
            claimed_artifact_id = "invalid"
        material = b"\0".join(
            (
                deterministic.reviewer_id.encode("utf-8"),
                code.encode("utf-8"),
                claimed_artifact_id.encode("utf-8"),
            )
        )
        return ReviewResultV1(
            reviewer_id=(
                f"local-semantic-blocked-{hashlib.sha256(material).hexdigest()[:16]}"
            ),
            reviewer=ExecutorKind.LOCAL_SEMANTIC_REVIEW,
            verdict=ReviewVerdict.BLOCKED,
            findings=[
                _finding(
                    ReviewSeverity.HIGH,
                    code,
                    scenario,
                    "Repeat local semantic review from the current authenticated attempt.",
                )
            ],
            checked_requirements=False,
            checked_tests=deterministic.checked_tests,
            checked_diff_scope=deterministic.checked_diff_scope,
            checked_secrets=deterministic.checked_secrets,
            checked_constitution=(
                deterministic.checked_constitution and worktree_unchanged
            ),
            summary="Local semantic review evidence could not be authenticated.",
            reviewed_at=datetime.now(timezone.utc),
        )

    if not isinstance(semantic_result, LocalSemanticReviewResult):
        return blocked(
            "local_semantic.result_invalid",
            "The local semantic result has an invalid typed contract.",
        )
    try:
        validated_subject_sha256 = validate_semantic_result(
            semantic_result,
            subject,
            semantic_config,
        )
        evidence_bytes = semantic_result.evidence.artifact_bytes()
    except SemanticReviewBlocked:
        return blocked(
            "local_semantic.result_invalid",
            "The local semantic result could not be re-derived from its current subject and canonical response.",
        )
    if (
        not isinstance(evidence_artifact, ArtifactReferenceV1)
        or evidence_artifact.kind is not ArtifactKind.REVIEW
        or evidence_artifact.producer != SEMANTIC_REVIEW_PRODUCER
        or evidence_artifact.media_type != "application/json"
        or evidence_artifact.sha256 != hashlib.sha256(evidence_bytes).hexdigest()
        or evidence_artifact.size_bytes != len(evidence_bytes)
    ):
        return blocked(
            "local_semantic.evidence_binding_invalid",
            "The persisted semantic-review artifact ID, digest, size, kind, or producer is not authentic.",
        )
    if (
        semantic_result.deterministic_review_id != deterministic.reviewer_id
        or semantic_result.subject_sha256 != semantic_result.evidence.subject_sha256
        or semantic_result.evidence.response_sha256
        != hashlib.sha256(semantic_result.evidence.canonical_response).hexdigest()
        or semantic_result.evidence.verdict != semantic_result.verdict
        or semantic_result.evidence.attestation_before
        != semantic_result.evidence.attestation_after
    ):
        return blocked(
            "local_semantic.subject_binding_invalid",
            "The local semantic result is stale or cross-linked to another subject, response, attestation, or deterministic review.",
        )

    findings = list(deterministic.findings)
    severities = {
        "P0": ReviewSeverity.CRITICAL,
        "P1": ReviewSeverity.HIGH,
        "P2": ReviewSeverity.MEDIUM,
        "P3": ReviewSeverity.LOW,
    }
    for finding in semantic_result.findings:
        findings.append(
            _finding(
                severities[finding.priority],
                f"local_semantic.{finding.priority.casefold()}.{finding.code}",
                f"{finding.title}: {finding.failure_scenario}",
                "Resolve the attested local semantic finding and repeat verification/review.",
                file=finding.file,
                line=finding.line,
            )
        )
    if not worktree_unchanged:
        findings.append(
            _finding(
                ReviewSeverity.CRITICAL,
                "local_semantic.review_mutated_worktree",
                "The worktree/content binding changed during local semantic review.",
                "Preserve the worktree for audit and repeat from a current immutable subject.",
            )
        )

    structural_gates = all(
        (
            deterministic.checked_tests,
            deterministic.checked_diff_scope,
            deterministic.checked_secrets,
            deterministic.checked_constitution,
        )
    )
    semantic_complete = bool(semantic_result.coverage) and worktree_unchanged
    if deterministic.verdict is ReviewVerdict.REJECTED:
        verdict = ReviewVerdict.REJECTED
    elif deterministic.verdict is ReviewVerdict.BLOCKED or not structural_gates:
        verdict = ReviewVerdict.BLOCKED
    elif semantic_result.verdict == "rejected" or semantic_result.findings:
        verdict = ReviewVerdict.REJECTED
    elif not semantic_complete:
        verdict = ReviewVerdict.BLOCKED
    else:
        verdict = ReviewVerdict.APPROVED

    reviewer_material = b"\0".join(
        (
            semantic_result.subject_sha256.encode("ascii"),
            semantic_result.evidence.response_sha256.encode("ascii"),
            semantic_result.evidence.attestation_sha256.encode("ascii"),
            evidence_artifact.artifact_id.encode("utf-8"),
            evidence_artifact.sha256.encode("ascii"),
        )
    )
    reviewer_id = f"local-semantic-{hashlib.sha256(reviewer_material).hexdigest()[:24]}"
    return ReviewResultV1(
        reviewer_id=reviewer_id,
        reviewer=ExecutorKind.LOCAL_SEMANTIC_REVIEW,
        verdict=verdict,
        findings=findings,
        checked_requirements=semantic_complete,
        checked_tests=deterministic.checked_tests,
        checked_diff_scope=deterministic.checked_diff_scope,
        checked_secrets=deterministic.checked_secrets,
        checked_constitution=(
            deterministic.checked_constitution and worktree_unchanged
        ),
        subject_sha256=validated_subject_sha256,
        evidence_artifact_id=evidence_artifact.artifact_id,
        evidence_artifact_sha256=evidence_artifact.sha256,
        summary=(
            "Attested local semantic review approved complete requirement coverage."
            if verdict is ReviewVerdict.APPROVED
            else "Attested local semantic review did not approve the bounded result."
        ),
        reviewed_at=datetime.now(timezone.utc),
    )


__all__.append("merge_local_semantic_review")
