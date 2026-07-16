from __future__ import annotations

import re
import os
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.contracts import (
    AttachmentKind,
    AttachmentRefV1,
    NormalizedRequestV1,
    ProjectResolutionSource,
    ProjectResolutionStatus,
    ProjectResolutionV1,
    RequestSource,
    RouteName,
    RouteOverride,
)

_OVERRIDE_PATTERN = re.compile(
    r"^\s*/(?P<name>local|codex|voice|vision|image|browser)(?=\s|$)", re.IGNORECASE
)
_EXPLICIT_PROJECT_PATTERN = re.compile(
    r"(?:project|проект|repo|репозиторий)\s*[:=]\s*(?:\"([^\"]+)\"|'([^']+)'|([^\r\n;]+))",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = (
    r"(?:[A-Za-z]:[\\/][^\s\r\n;<>|\",)]+"
    r"|\\\\[^\\/\s\r\n;<>|\",)]+[\\/][^\s\r\n;<>|\",)]+"
    r"|/mnt/[A-Za-z]/[^\s\r\n;<>|\",)]+)"
)
_QUOTED_ABSOLUTE_PATH = (
    r"(?:[A-Za-z]:[\\/][^\"\r\n;]+"
    r"|\\\\[^\"\r\n;]+"
    r"|/mnt/[A-Za-z]/[^\"\r\n;]+)"
)
_LEADING_ABSOLUTE_PATH = re.compile(
    rf"^\s*(?:\"({_QUOTED_ABSOLUTE_PATH})\"|({_ABSOLUTE_PATH}))",
    re.MULTILINE,
)
_ANY_ABSOLUTE_PATH = re.compile(
    rf"(?:^|[\s(])(?:\"({_QUOTED_ABSOLUTE_PATH})\"|({_ABSOLUTE_PATH}))",
    re.MULTILINE,
)

_OVERRIDE_TO_ROUTE = {
    RouteOverride.CODEX: RouteName.CODEX,
    RouteOverride.VOICE: RouteName.VOICE,
    RouteOverride.VISION: RouteName.VISION,
    RouteOverride.IMAGE: RouteName.IMAGE,
    RouteOverride.BROWSER: RouteName.BROWSER,
}


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def parse_override(text: str) -> tuple[str, RouteOverride | None, bool]:
    """Parse only leading, standalone control tokens and strip all conflicting tokens."""

    remainder = text
    parsed: list[RouteOverride] = []
    while match := _OVERRIDE_PATTERN.match(remainder):
        parsed.append(RouteOverride(match.group("name").casefold()))
        remainder = remainder[match.end() :]
    unique = list(dict.fromkeys(parsed))
    return normalize_text(remainder), (unique[0] if unique else None), len(unique) > 1


def flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        attachment_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", ""))
            if kind in {"text", "input_text"}:
                text_parts.append(str(item.get("text", "")))
            elif kind in {"image_url", "input_image", "image"}:
                attachment_parts.append("[image attached]")
            elif kind in {"input_audio", "audio", "audio_url"}:
                attachment_parts.append("[audio attached]")
            elif kind in {"file", "input_file"}:
                attachment_parts.append("[file attached]")
        return "\n".join([*text_parts, *attachment_parts])
    return str(content)


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return flatten_content(message.get("content", ""))
    return ""


def _media_type_from_part(part: dict[str, Any]) -> str | None:
    for key in ("media_type", "mime_type"):
        if isinstance(part.get(key), str):
            return part[key][:255]
    value: Any = part.get("image_url") or part.get("audio_url") or part.get("file")
    if isinstance(value, dict):
        for key in ("url", "data"):
            if isinstance(value.get(key), str):
                value = value[key]
                break
    if isinstance(value, str):
        match = re.match(r"data:([^;,]+)", value, re.IGNORECASE)
        if match:
            return match.group(1)[:255]
    audio = part.get("input_audio")
    if isinstance(audio, dict) and isinstance(audio.get("format"), str):
        return f"audio/{audio['format'].casefold()}"[:255]
    return None


def attachment_references(messages: list[dict[str, Any]]) -> list[AttachmentRefV1]:
    references: list[AttachmentRefV1] = []
    current_turn = next(
        (
            (message_index, message)
            for message_index, message in reversed(list(enumerate(messages)))
            if message.get("role") == "user"
        ),
        None,
    )
    if current_turn is None:
        return references
    for message_index, message in [current_turn]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", "")).casefold()
            media_type = _media_type_from_part(part)
            if part_type in {"image_url", "input_image", "image"} or (media_type or "").startswith("image/"):
                kind = AttachmentKind.IMAGE
            elif part_type in {"input_audio", "audio"} or (media_type or "").startswith("audio/"):
                kind = AttachmentKind.AUDIO
            elif part_type in {"file", "input_file"}:
                kind = AttachmentKind.DOCUMENT if (media_type or "").startswith(("text/", "application/pdf")) else AttachmentKind.FILE
            else:
                continue
            references.append(
                AttachmentRefV1(
                    attachment_id=f"attachment-{message_index}-{part_index}",
                    kind=kind,
                    reference=f"request-message:{message_index}:part:{part_index}",
                    media_type=media_type,
                    provenance=["OpenAI message position; inline content intentionally excluded."],
                )
            )
    return references


def resolve_project(text: str, default_project: str | None) -> tuple[str | None, ProjectResolutionV1]:
    marker = _EXPLICIT_PROJECT_PATTERN.search(text)
    candidate: str | None = None
    if marker:
        candidate = next(group for group in marker.groups() if group is not None)
    else:
        leading = _LEADING_ABSOLUTE_PATH.search(text)
        if leading:
            candidate = next(group for group in leading.groups() if group is not None)
        else:
            embedded = _ANY_ABSOLUTE_PATH.search(text)
            if embedded:
                candidate = next(group for group in embedded.groups() if group is not None)
    if candidate is not None:
        cleaned = candidate.strip().rstrip(".,;)")
        # Never probe UNC paths during request normalization: a missing share can
        # block for seconds and leak Windows credentials over SMB/DNS.
        if cleaned.startswith(("\\\\", "//")):
            return None, ProjectResolutionV1(source="explicit", status="invalid")
        if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", cleaned):
            try:
                import ctypes

                drive_root = cleaned[:2] + "\\"
                if ctypes.windll.kernel32.GetDriveTypeW(drive_root) == 4:  # DRIVE_REMOTE
                    return None, ProjectResolutionV1(source="explicit", status="invalid")
            except (AttributeError, OSError):
                return None, ProjectResolutionV1(source="explicit", status="invalid")
        path = Path(cleaned)
        if path.exists() and path.is_dir():
            return str(path.resolve()), ProjectResolutionV1(source="explicit", status="resolved")
        return None, ProjectResolutionV1(source="explicit", status="invalid")
    if default_project:
        default = Path(default_project)
        if default.exists() and default.is_dir():
            return str(default.resolve()), ProjectResolutionV1(source="default", status="resolved")
    return None, ProjectResolutionV1(source="none", status="missing")


def normalize_messages(
    messages: list[dict[str, Any]],
    *,
    default_project: str | None,
    request_id: str | None = None,
    source: RequestSource = RequestSource.API,
) -> NormalizedRequestV1:
    identifier = request_id or uuid.uuid4().hex[:12]
    raw_message = normalize_text(last_user_text(messages))
    user_message, override, conflict = parse_override(raw_message)
    project, project_resolution = resolve_project(user_message, default_project)
    return NormalizedRequestV1(
        request_id=identifier,
        user_message=user_message,
        attachments=attachment_references(messages),
        source=source,
        project_hint=project,
        explicit_route=_OVERRIDE_TO_ROUTE.get(override),
        created_at=datetime.now(timezone.utc),
        correlation_id=identifier,
        routing_override=override,
        override_conflict=conflict,
        project_resolution=project_resolution,
    )


def replace_last_user_text(messages: list[dict[str, Any]], cleaned_text: str) -> list[dict[str, Any]]:
    """Return a copy suitable for execution, with the routing prefix removed."""

    copied = [dict(message) for message in messages]
    for index in range(len(copied) - 1, -1, -1):
        if copied[index].get("role") != "user":
            continue
        content = copied[index].get("content")
        if isinstance(content, str):
            copied[index]["content"] = cleaned_text
        elif isinstance(content, list):
            parts = [dict(item) if isinstance(item, dict) else item for item in content]
            replaced = False
            for part in parts:
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                    part["text"] = cleaned_text if not replaced else ""
                    replaced = True
            if not replaced:
                parts.insert(0, {"type": "text", "text": cleaned_text})
            copied[index]["content"] = parts
        break
    return copied
