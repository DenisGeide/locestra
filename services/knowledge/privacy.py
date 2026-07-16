"""Fail-closed filesystem and payload boundary for untrusted knowledge sources."""

from __future__ import annotations

import os
import json
import html
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from services.knowledge.config import KnowledgePolicy


class KnowledgePolicyError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"knowledge policy rejected source: {reason_code}")


@dataclass(frozen=True, slots=True)
class ReadSource:
    project_path: Path
    source_path: Path
    relative_path: str
    payload: bytes
    size_bytes: int
    mtime_ns: int


_WINDOWS_RESERVED = re.compile(
    r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$"
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("secret.private_key", re.compile(br"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.I)),
    ("secret.openai_key", re.compile(br"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("secret.github_token", re.compile(br"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("secret.github_token", re.compile(br"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("secret.aws_key", re.compile(br"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret.slack_token", re.compile(br"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("secret.google_key", re.compile(br"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("secret.telegram_token", re.compile(br"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("secret.jwt", re.compile(br"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("secret.stripe_key", re.compile(br"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("secret.sendgrid_key", re.compile(br"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("secret.authorization", re.compile(br"(?i)[\"']?(?:authorization|proxy-authorization)[\"']?\s*:\s*[\"']?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("secret.bearer", re.compile(br"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("secret.cookie", re.compile(br"(?i)[\"']?(?:set-cookie|cookie)[\"']?\s*:\s*[\"']?(?:[A-Za-z0-9_.-]{1,64}=)?[A-Za-z0-9._~+/=%-]{12,}")),
    ("secret.connection_uri", re.compile(br"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^\s/:@]+:[^\s/@]+@[^\s]+")),
    ("secret.url_credentials", re.compile(br"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:[^\s/@]+@")),
)

_SECRET_JSON_KEYS = {
    "apikey", "accesstoken", "authtoken", "authorization", "bearertoken",
    "clientsecret", "cookie", "cookies", "credential", "credentials",
    "password", "passwd", "privatekey", "refreshtoken", "secret", "setcookie",
    "telegrambottoken", "token",
}
_PLACEHOLDER_WORDS = {
    "changeme", "dummy", "example", "placeholder", "redacted", "replace-me",
    "replace_me", "[redacted]", "***", "configured", "hidden", "optional",
    "required", "stored",
}
_FORMAT_CHARACTERS = {
    ord(char): None
    for char in (
        "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069",
    )
}
_UNICODE_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2}))")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<key>[\w.\-\"']{2,96})[ \t]*[:=][ \t]*"
    r"(?P<value>\"[^\"\r\n]{1,512}\"|'[^'\r\n]{1,512}'|[^\s,;#`]{1,512})"
)
_SECRET_KEY_TEXT = (
    r"(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|authorization|bearer[_ -]?token|"
    r"client[_ -]?secret|refresh[_ -]?token|password|passwd|private[_ -]?key|"
    r"cookie|credentials?|secret|token)"
)
_SECRET_TABLE = re.compile(
    rf"(?im)^\s*\|[ \t]*(?P<key>{_SECRET_KEY_TEXT})[ \t]*\|"
    r"[ \t]*(?P<value>[^|\r\n]{1,512})\|"
)
_SECRET_PROSE = re.compile(
    rf"(?i)\b(?P<key>{_SECRET_KEY_TEXT})\b[ \t]+(?:is|was|equals?|value[ \t]+is)[ \t]+"
    r"(?P<value>[^\s,;.]{1,512})"
)
_SECRET_HTML_PAIR = re.compile(
    rf"(?is)<dt\b[^>]*>\s*(?P<key>{_SECRET_KEY_TEXT})\s*</dt>\s*"
    r"<dd\b[^>]*>\s*(?P<value>.*?)\s*</dd>"
)
_CONFUSABLE_KEY_CHARACTERS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
        "і": "i", "ј": "j", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
        "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k",
        "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x",
        "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
        "ρ": "p", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "χ": "x",
        "ѕ": "s", "ԁ": "d", "ԝ": "w", "ӏ": "l",
    }
)


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise KnowledgePolicyError("path.unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or path.is_symlink():
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def canonical_project(project_path: str | Path) -> Path:
    raw = str(project_path)
    secret_reason = detect_secret(raw.encode("utf-8", errors="strict"))
    if secret_reason:
        raise KnowledgePolicyError("secret.path_metadata")
    if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise KnowledgePolicyError("path.device_or_unc")
    project = Path(project_path)
    if not project.is_absolute() or not project.is_dir():
        raise KnowledgePolicyError("scope.invalid_project")
    _reject_reparse_ancestors(project)
    if _is_reparse(project):
        raise KnowledgePolicyError("path.reparse")
    return project.resolve(strict=True)


def _reject_reparse_ancestors(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if _is_reparse(cursor):
            raise KnowledgePolicyError("path.reparse_ancestor")


def _validate_raw_path(path: str | Path) -> None:
    raw = str(path)
    normalized = unicodedata.normalize("NFKC", raw)
    if "%" in raw or normalized != raw:
        raise KnowledgePolicyError("path.encoded_or_unicode_alias")
    if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise KnowledgePolicyError("path.device_or_unc")
    drive, tail = os.path.splitdrive(raw)
    if ":" in tail:
        raise KnowledgePolicyError("path.alternate_data_stream")
    for part in Path(raw).parts:
        if part not in {".", "..", "\\", "/"} and (
            part.rstrip(" .") != part or _WINDOWS_RESERVED.match(part)
        ):
            raise KnowledgePolicyError("path.windows_alias")


def _within(project: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(project)), os.path.normcase(str(candidate)))) == os.path.normcase(str(project))
    except ValueError:
        return False


def _check_components(project: Path, source: Path) -> None:
    relative = source.relative_to(project)
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if _is_reparse(cursor):
            raise KnowledgePolicyError("path.reparse")


def _path_policy(
    relative: Path,
    policy: KnowledgePolicy,
    *,
    repository_tracked: bool = False,
) -> None:
    parts = relative.parts
    folded = tuple(_norm(part) for part in parts)
    blocked_dirs = {_norm(value) for value in policy.blocked_directory_names}
    blocked_files = {_norm(value) for value in policy.blocked_file_names}
    if any(part in blocked_dirs for part in folded[:-1]):
        raise KnowledgePolicyError("path.blocked_directory")
    name = folded[-1]
    if name in blocked_files or name.startswith(".env"):
        raise KnowledgePolicyError("path.secret_name")
    if any(name.endswith(_norm(suffix)) for suffix in policy.blocked_file_suffixes):
        raise KnowledgePolicyError("path.blocked_suffix")
    root_allowed = len(parts) == 1 and relative.as_posix() in policy.allowed_root_files
    directory_allowed = len(parts) > 1 and folded[0] in {
        _norm(value) for value in policy.allowed_directories
    }
    if not repository_tracked and not (root_allowed or directory_allowed):
        raise KnowledgePolicyError("scope.not_allowlisted")
    if not root_allowed and relative.suffix.casefold() not in policy.allowed_extensions:
        raise KnowledgePolicyError("format.unsupported_extension")


def detect_secret(payload: bytes) -> str | None:
    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(payload):
            return reason
    try:
        canonical = _canonicalize_untrusted_text(payload.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        canonical = ""
    except KnowledgePolicyError as exc:
        return exc.reason_code
    if canonical:
        encoded = canonical.encode("utf-8", errors="strict")
        for reason, pattern in _SECRET_PATTERNS:
            if pattern.search(encoded):
                return reason
    stripped = payload.lstrip()
    looks_like_json = stripped.startswith(b"{") or bool(
        re.match(br"^\[\s*(?:\{|\[|\"|-?\d|true\b|false\b|null\b|\])", stripped, re.I)
    )
    if looks_like_json:
        try:
            decoded = json.loads(payload.decode("utf-8", errors="strict"))
        except RecursionError:
            return "secret.structured_depth"
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = None
        else:
            # Valid structured data is inspected by key/value semantics.  Do
            # not run the prose assignment regex over JSON punctuation: doing
            # so turns harmless OpenAPI schemas and null/default fields into
            # false credential findings.
            return _detect_structured_secret(decoded)
    if canonical:
        assignment_reason = _detect_assignment_secret(canonical)
        if assignment_reason:
            return assignment_reason
        semantic_reason = _detect_semantic_secret(canonical)
        if semantic_reason:
            return semantic_reason
    return None


def _detect_structured_secret(value: Any, *, depth: int = 0) -> str | None:
    if depth > 16:
        return "secret.structured_depth"
    if isinstance(value, dict):
        for key, child in value.items():
            try:
                normalized_key = _normalize_secret_key(str(key))
            except KnowledgePolicyError as exc:
                return exc.reason_code
            if normalized_key in _SECRET_JSON_KEYS:
                if isinstance(child, str):
                    candidate = child.strip()
                    if candidate and not _is_placeholder(candidate):
                        return "secret.structured_key"
                elif not isinstance(child, (dict, list)) and child not in (None, False, 0, ""):
                    return "secret.structured_key"
            reason = _detect_structured_secret(child, depth=depth + 1)
            if reason:
                return reason
    elif isinstance(value, list):
        for child in value:
            reason = _detect_structured_secret(child, depth=depth + 1)
            if reason:
                return reason
    elif isinstance(value, str):
        normalized = _canonicalize_untrusted_text(value)
        encoded = normalized.encode("utf-8", errors="strict")
        for reason, pattern in _SECRET_PATTERNS:
            if pattern.search(encoded):
                return reason
    return None


def _canonicalize_untrusted_text(value: str) -> str:
    current = value
    for _ in range(8):
        previous = current
        current = _strip_scan_ignorables(
            unicodedata.normalize("NFKC", current).translate(_FORMAT_CHARACTERS)
        )
        current = html.unescape(current)
        current = unquote(current)

        def replace_escape(match: re.Match[str]) -> str:
            token = next(group for group in match.groups() if group is not None)
            try:
                return chr(int(token, 16))
            except (ValueError, OverflowError):
                return match.group(0)

        current = _UNICODE_ESCAPE.sub(replace_escape, current)
        current = _strip_scan_ignorables(unicodedata.normalize("NFKC", current))
        if current == previous:
            return current
        if len(current) > max(4_194_304, len(value) * 4):
            raise KnowledgePolicyError("limit.canonicalization")
    raise KnowledgePolicyError("secret.encoding_depth")


def _strip_scan_ignorables(value: str) -> str:
    result: list[str] = []
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character in {"\t", "\r", "\n"}:
            result.append(character)
            continue
        if category in {"Cc", "Cf"}:
            continue
        if character == "\u034f" or 0x180B <= codepoint <= 0x180F:
            continue
        if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
            continue
        result.append(character)
    return "".join(result)


def _normalize_secret_key(value: str) -> str:
    canonical = _canonicalize_untrusted_text(value).casefold().translate(_CONFUSABLE_KEY_CHARACTERS)
    return re.sub(r"[^a-z0-9]", "", canonical)


def _detect_assignment_secret(value: str) -> str | None:
    for match in _SECRET_ASSIGNMENT.finditer(value):
        key = _normalize_secret_key(match.group("key").strip("\"'"))
        if key not in _SECRET_JSON_KEYS:
            continue
        raw = match.group("value").strip()
        quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"\"", "'"}
        candidate = raw[1:-1] if quoted else raw
        candidate = candidate.strip()
        folded = candidate.casefold()
        if not candidate or _is_placeholder(folded):
            continue
        if folded in {
            "none", "null", "false", "true", "0", "bearer", "basic",
            "=", "==", ":=", "=>",
        }:
            continue
        if folded == key:
            continue
        if not quoted and re.match(r"^\(\?(?:[:=!<]|[a-z-])|^\(\.\s*[*+?]", candidate):
            continue
        # Source code frequently declares credential-shaped fields or assigns a
        # value returned by another object.  Those expressions do not embed the
        # credential itself and must remain indexable.  Quoted literals are not
        # exempted here: they are the high-signal case this scanner protects.
        if not quoted and re.fullmatch(
            r"(?:str|bytes|secretstr|secretbytes|any|object|"
            r"optional\s*\[\s*(?:str|bytes|secretstr|secretbytes)\s*\]|"
            r"(?:str|bytes|secretstr|secretbytes)\s*\|\s*none)[\])]*",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        if not quoted and re.fullmatch(
            r"(?:field|dataclasses\.field|pydantic\.field|column|mapped_column)"
            r"\([^\r\n]{0,384}\)",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        if not quoted and re.match(r"^(?:\$|[A-Za-z_])", candidate) and (
            "(" in candidate or "[" in candidate
        ):
            # Function calls, indexing expressions and PowerShell variable
            # references contain no embedded credential literal.  Known token
            # formats were scanned before assignment analysis.
            continue
        if not quoted and re.fullmatch(
            r"(?:os\.getenv|os\.environ\.get|config\.get|settings\.get|request\.[A-Za-z_][A-Za-z0-9_]*\.get)\([^\r\n]{1,256}\)",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        if not quoted and re.fullmatch(
            r"(?:os\.environ|config|settings|request\.[A-Za-z_][A-Za-z0-9_]*)\[[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\]",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        if not quoted and re.fullmatch(
            r"(?:settings|config|request|response|process\.env|os\.environ)(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        if not quoted and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*"
            r"(?:\([^\r\n]{0,256}\))?"
            r"(?:\[[\"'][A-Za-z_][A-Za-z0-9_.-]*[\"']\])"
            r"(?:\[[\"'][A-Za-z_][A-Za-z0-9_.-]*[\"']\]|\.[A-Za-z_][A-Za-z0-9_]*)*",
            candidate,
            flags=re.IGNORECASE,
        ):
            continue
        return "secret.assignment"
    return None


def _detect_semantic_secret(value: str) -> str | None:
    for pattern in (_SECRET_TABLE, _SECRET_PROSE, _SECRET_HTML_PAIR):
        for match in pattern.finditer(value):
            candidate = re.sub(r"<[^>]{1,256}>", "", match.group("value")).strip(" \t\"'`<>")
            if not candidate or _is_placeholder(candidate):
                continue
            folded = candidate.casefold()
            if folded in {"required", "optional", "configured", "stored", "hidden", "redacted"}:
                continue
            has_alpha = any(character.isalpha() for character in candidate)
            has_digit = any(character.isdigit() for character in candidate)
            has_symbol = any(not character.isalnum() for character in candidate)
            if len(candidate) >= 16 or (len(candidate) >= 6 and has_alpha and (has_digit or has_symbol)):
                return "secret.semantic_key_value"
    return None


def _is_placeholder(value: str) -> bool:
    raw = value.strip()
    candidate = raw.casefold()
    if candidate in _PLACEHOLDER_WORDS:
        return True
    return bool(
        re.fullmatch(
            r"\$[A-Z_][A-Z0-9_]*|\$\{[A-Z_][A-Z0-9_]*\}|%[A-Z_][A-Z0-9_]*%",
            raw,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(
            r"(?:<|\{\{|\[)\s*(?:your[-_ ][a-z0-9_. -]+|placeholder(?:[-_ ][a-z0-9_. -]+)?|redacted)\s*(?:>|\}\}|\])",
            candidate,
        )
        or re.fullmatch(r"(?:example|dummy|test|local)[-_][a-z0-9_-]+", candidate)
    )


def reject_secret_text(value: str) -> None:
    reason = detect_secret(value.encode("utf-8", errors="strict"))
    if reason:
        raise KnowledgePolicyError(reason)


def _open_no_follow(source: Path, project: Path) -> int:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(source, flags)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_information.restype = wintypes.BOOL
    get_final_name = kernel32.GetFinalPathNameByHandleW
    get_final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(source),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080 | 0x00200000 | 0x08000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise KnowledgePolicyError("path.open_failed")
    try:
        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

        attributes = FileAttributeTagInfo()
        if not get_information(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise KnowledgePolicyError("path.handle_validation_failed")
        if attributes.FileAttributes & 0x00000400:
            raise KnowledgePolicyError("path.reparse")
        buffer = ctypes.create_unicode_buffer(32_768)
        length = get_final_name(handle, buffer, len(buffer), 0)
        if length == 0 or length >= len(buffer):
            raise KnowledgePolicyError("path.handle_validation_failed")
        final_name = buffer.value
        if final_name.startswith("\\\\?\\UNC\\"):
            final_name = "\\\\" + final_name[8:]
        elif final_name.startswith("\\\\?\\"):
            final_name = final_name[4:]
        if not _within(project, Path(final_name)):
            raise KnowledgePolicyError("scope.handle_escape")
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        handle = None
        return descriptor
    finally:
        if handle not in {None, invalid}:
            close_handle(handle)


def read_registered_source(
    project_path: str | Path,
    source_path: str | Path,
    policy: KnowledgePolicy,
    *,
    repository_tracked: bool = False,
) -> ReadSource:
    """Read once from an approved root and verify the file did not change."""

    _validate_raw_path(project_path)
    _validate_raw_path(source_path)
    project = canonical_project(project_path)
    source_raw = Path(source_path)
    source_candidate = source_raw if source_raw.is_absolute() else project / source_raw
    try:
        source = source_candidate.resolve(strict=True)
    except OSError as exc:
        raise KnowledgePolicyError("path.unavailable") from exc
    if not _within(project, source) or source == project:
        raise KnowledgePolicyError("scope.escape")
    _check_components(project, source)
    relative = source.relative_to(project)
    _path_policy(relative, policy, repository_tracked=repository_tracked)
    reject_secret_text(relative.as_posix())

    descriptor = _open_no_follow(source, project)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise KnowledgePolicyError("format.not_regular_file")
        if getattr(before, "st_nlink", 1) > 1:
            raise KnowledgePolicyError("path.hardlink")
        if before.st_size > policy.max_file_bytes:
            raise KnowledgePolicyError("limit.file_bytes")
        chunks: list[bytes] = []
        remaining = policy.max_file_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or len(payload) != after.st_size:
            raise KnowledgePolicyError("source.changed_during_read")
    finally:
        os.close(descriptor)

    if b"\x00" in payload:
        raise KnowledgePolicyError("format.binary")
    secret_reason = detect_secret(payload)
    if secret_reason:
        raise KnowledgePolicyError(secret_reason)
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise KnowledgePolicyError("format.invalid_utf8") from exc
    return ReadSource(
        project_path=project,
        source_path=source,
        relative_path=relative.as_posix(),
        payload=payload,
        size_bytes=len(payload),
        mtime_ns=after.st_mtime_ns,
    )


def preflight_registered_source_size(
    project_path: str | Path,
    source_path: str | Path,
    policy: KnowledgePolicy,
    *,
    repository_tracked: bool = False,
) -> int:
    """Return a safely opened file size without reading its content."""

    return registered_source_observation(
        project_path,
        source_path,
        policy,
        repository_tracked=repository_tracked,
    )[0]


def registered_source_observation(
    project_path: str | Path,
    source_path: str | Path,
    policy: KnowledgePolicy,
    *,
    repository_tracked: bool = False,
) -> tuple[int, int]:
    """Return current size/mtime through the same scope boundary as import."""

    _validate_raw_path(project_path)
    _validate_raw_path(source_path)
    project = canonical_project(project_path)
    raw = Path(source_path)
    candidate = raw if raw.is_absolute() else project / raw
    try:
        source = candidate.resolve(strict=True)
    except OSError as exc:
        raise KnowledgePolicyError("path.unavailable") from exc
    if not _within(project, source) or source == project:
        raise KnowledgePolicyError("scope.escape")
    _check_components(project, source)
    relative = source.relative_to(project)
    _path_policy(relative, policy, repository_tracked=repository_tracked)
    reject_secret_text(relative.as_posix())
    descriptor = _open_no_follow(source, project)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise KnowledgePolicyError("format.not_regular_file")
        if getattr(info, "st_nlink", 1) > 1:
            raise KnowledgePolicyError("path.hardlink")
        if info.st_size > policy.max_file_bytes:
            raise KnowledgePolicyError("limit.file_bytes")
        return int(info.st_size), int(info.st_mtime_ns)
    finally:
        os.close(descriptor)
