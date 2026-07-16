"""Bounded privacy policy for durable memory and task evidence.

This module deliberately does not promise perfect secret detection.  It provides
one conservative persistence boundary: durable memory is rejected when content
looks secret-bearing, while the legacy task journal can use the non-throwing
sanitizers to retain bounded operational evidence without blocking execution.

No rejection message, reason code, or object representation contains the
rejected value or a hash of it.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit


MEMORY_MAX_BYTES = 32_768
MEMORY_MAX_DEPTH = 8
MEMORY_MAX_ITEMS = 256
TASK_TEXT_MAX_CHARS = 2_048
TASK_METADATA_MAX_BYTES = 16_384

REDACTED = "[REDACTED:secret-like]"
FORBIDDEN_REFERENCE = "[REDACTED:forbidden-reference]"
OMITTED = "[OMITTED:privacy-limit]"


class PrivacyAction(str, Enum):
    ALLOW = "allow"
    REJECT = "reject"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    action: PrivacyAction
    sensitivity: Sensitivity
    reason_codes: tuple[str, ...] = ()
    normalized_value: Any = field(default=None, repr=False)

    @property
    def allowed(self) -> bool:
        return self.action is PrivacyAction.ALLOW


class MemoryPrivacyError(ValueError):
    """A payload-free privacy rejection suitable for API and audit boundaries."""

    def __init__(self, reason_code: str, reason_codes: tuple[str, ...] = ()) -> None:
        safe_codes = tuple(dict.fromkeys((reason_code, *reason_codes)))
        self.reason_code = reason_code
        self.reason_codes = safe_codes
        super().__init__(f"memory privacy policy rejected content: {reason_code}")


class _PolicyViolation(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


_ZERO_WIDTH = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])")

_SECRET_KEY_NAMES = frozenset(
    {
        "apikey",
        "accesstoken",
        "authtoken",
        "authorization",
        "bearertoken",
        "clientsecret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "setcookie",
        "telegrambottoken",
        "token",
    }
)

_PLACEHOLDER_PREFIXES = (
    "$",
    "%",
    "<",
    "{",
    "your-",
    "example-",
    "placeholder",
    "changeme",
    "replace",
    "redacted",
    "dummy",
    "test-",
    "local-",
    "***",
    "[redacted",
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "secret.private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
            r"(?:.|\n){0,32768}?"
            r"(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|$)",
            re.IGNORECASE,
        ),
    ),
    ("secret.aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret.github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("secret.github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("secret.openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("secret.slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("secret.google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("secret.telegram_token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    (
        "secret.jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    ("secret.stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    (
        "secret.sendgrid_key",
        re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "secret.authorization",
        re.compile(
            r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*"
            r"(?:bearer|basic)?\s*[A-Za-z0-9._~+/=-]{8,}"
        ),
    ),
    ("secret.bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "secret.cookie",
        re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]{4,}"),
    ),
    (
        "secret.connection_uri",
        re.compile(
            r"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://"
            r"[^\s/:@]+:[^\s/@]+@[^\s]+"
        ),
    ),
    (
        "secret.url_credentials",
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:[^\s/@]+@"),
    ),
)

_GENERIC_ASSIGNMENT = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|refresh[_-]?token|telegram[_-]?bot[_-]?token|"
    r"private[_-]?key|password|passwd|secret|token)\b"
    r"\s*[:=]\s*[\"']?(?P<value>[^\s\"'`,;]{6,})"
)

_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._~+/=-]{23,255}")
_UUID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_COMMIT_HASH_RE = re.compile(r"(?i)^[0-9a-f]{7,64}$")

_FORBIDDEN_FILE_NAMES = frozenset(
    {
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        "auth.json",
        "credentials.json",
        "secrets.json",
        "cookies.txt",
        "cookies.sqlite",
        "id_rsa",
        "id_ed25519",
        "key4.db",
        "logins.json",
        "login data",
        "web data",
    }
)
_FORBIDDEN_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".kdbx"})
_SECRET_DIRECTORIES = frozenset({".ssh", ".aws", ".azure", ".kube", ".docker"})


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    chars: list[str] = []
    for character in normalized:
        if character in _ZERO_WIDTH or character == "\x00":
            continue
        if ord(character) < 32 and character not in {"\n", "\r", "\t"}:
            chars.append(" ")
        else:
            chars.append(character)
    return "".join(chars)


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize_text(value).casefold())


def _is_secret_key(value: str) -> bool:
    return _canonical_key(value) in _SECRET_KEY_NAMES


def _is_placeholder(value: str) -> bool:
    candidate = value.strip().casefold()
    return candidate in {"", "none", "null", "false"} or candidate.startswith(_PLACEHOLDER_PREFIXES)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _is_high_entropy_token(value: str) -> bool:
    if _UUID_RE.fullmatch(value) or _COMMIT_HASH_RE.fullmatch(value):
        return False
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    return classes >= 3 and _shannon_entropy(value) >= 4.3


def _text_secret_reasons(value: str, *, include_entropy: bool = True) -> tuple[str, ...]:
    reasons: list[str] = []
    for reason, pattern in _PATTERNS:
        if pattern.search(value):
            reasons.append(reason)
    for match in _GENERIC_ASSIGNMENT.finditer(value):
        if not _is_placeholder(match.group("value")):
            reasons.append("secret.assignment")
    if include_entropy and any(
        _is_high_entropy_token(match.group(0))
        for match in _ENTROPY_CANDIDATE.finditer(value)
    ):
        reasons.append("secret.high_entropy")
    return tuple(dict.fromkeys(reasons))


def _redact_text(value: str) -> tuple[str, tuple[str, ...]]:
    redacted = value
    reasons: list[str] = []
    for reason, pattern in _PATTERNS:
        if pattern.search(redacted):
            reasons.append(reason)
            redacted = pattern.sub(REDACTED, redacted)

    def replace_assignment(match: re.Match[str]) -> str:
        if _is_placeholder(match.group("value")):
            return match.group(0)
        reasons.append("secret.assignment")
        return REDACTED

    redacted = _GENERIC_ASSIGNMENT.sub(replace_assignment, redacted)

    def replace_entropy(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if not _is_high_entropy_token(candidate):
            return candidate
        reasons.append("secret.high_entropy")
        return REDACTED

    redacted = _ENTROPY_CANDIDATE.sub(replace_entropy, redacted)
    return redacted, tuple(dict.fromkeys(reasons))


@dataclass(slots=True)
class _WalkBudget:
    max_depth: int
    max_items: int
    items: int = 0

    def consume(self, depth: int) -> None:
        if depth > self.max_depth:
            raise _PolicyViolation("content.too_deep")
        self.items += 1
        if self.items > self.max_items:
            raise _PolicyViolation("content.too_many_items")


def _normalize_structure(
    value: Any,
    *,
    budget: _WalkBudget,
    depth: int,
    active_containers: set[int],
    text_leaves: list[str],
) -> Any:
    budget.consume(depth)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _PolicyViolation("content.non_finite_number")
        return value
    if isinstance(value, str):
        normalized = _normalize_text(value)
        if len(normalized.encode("utf-8")) > MEMORY_MAX_BYTES:
            raise _PolicyViolation("content.too_large")
        text_leaves.append(normalized)
        return normalized
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _PolicyViolation("content.binary_forbidden")
    if not isinstance(value, (dict, list, tuple)):
        raise _PolicyViolation("content.unsupported_type")

    identity = id(value)
    if identity in active_containers:
        raise _PolicyViolation("content.cyclic")
    active_containers.add(identity)
    try:
        if isinstance(value, dict):
            normalized_mapping: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _PolicyViolation("content.non_string_key")
                normalized_key = _normalize_text(key)
                if normalized_key in normalized_mapping:
                    raise _PolicyViolation("content.duplicate_key_after_normalization")
                if _text_secret_reasons(normalized_key):
                    raise _PolicyViolation("secret.sensitive_field")
                normalized_item = _normalize_structure(
                    item,
                    budget=budget,
                    depth=depth + 1,
                    active_containers=active_containers,
                    text_leaves=text_leaves,
                )
                if _is_secret_key(normalized_key) and not (
                    normalized_item is None
                    or isinstance(normalized_item, bool)
                    or (isinstance(normalized_item, str) and _is_placeholder(normalized_item))
                ):
                    raise _PolicyViolation("secret.sensitive_field")
                normalized_mapping[normalized_key] = normalized_item
            return normalized_mapping
        return [
            _normalize_structure(
                item,
                budget=budget,
                depth=depth + 1,
                active_containers=active_containers,
                text_leaves=text_leaves,
            )
            for item in value
        ]
    finally:
        active_containers.remove(identity)


def _url_component_has_secret(value: str, *, plus_as_space: bool) -> bool:
    """Inspect a bounded URL component, including nested percent encoding."""

    candidate = _normalize_text(value)
    decoder = unquote_plus if plus_as_space else unquote
    variants = [candidate]
    # Two bounded decoding passes cover ordinary and double-encoded query
    # values without turning this policy into a general-purpose decoder.
    for _ in range(2):
        decoded = _normalize_text(decoder(candidate))
        if decoded == candidate:
            break
        variants.append(decoded)
        candidate = decoded
    for item in variants:
        # Inspect the complete component for named assignments and known
        # provider patterns.  Entropy is evaluated on each parameter value so
        # an ordinary ``request_id=<uuid>`` is not made high-entropy merely by
        # concatenating its key and separator.
        if _text_secret_reasons(item, include_entropy=False):
            return True
        for part in re.split(r"[&;]", item):
            key, separator, parameter_value = part.partition("=")
            candidate_value = parameter_value if separator else key
            if _text_secret_reasons(candidate_value):
                return True
    return False


def _source_variant_rejection_reason(value: str) -> str | None:
    if not value:
        return None
    if len(value.encode("utf-8")) > MEMORY_MAX_BYTES:
        return "source.too_large"
    slash_path = value.replace("\\", "/")
    if slash_path.startswith("//"):
        return "source.network_path_forbidden"

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "source.invalid"
    scheme = parsed.scheme.casefold()
    if scheme == "local-file":
        # ``local-file://C:/...`` is the canonical Windows form emitted by
        # the store.  Any other authority, or a path beginning with ``//``,
        # is an SMB/UNC boundary and must never become durable provenance.
        if parsed.netloc and not re.fullmatch(r"[A-Za-z]:", parsed.netloc):
            return "source.network_path_forbidden"
        if parsed.path.replace("\\", "/").startswith("//"):
            return "source.network_path_forbidden"
    if len(parsed.scheme) > 1 and (parsed.username is not None or parsed.password is not None):
        return "source.url_credentials_forbidden"
    if parsed.query and _url_component_has_secret(parsed.query, plus_as_space=True):
        return "source.query_secret_forbidden"
    if parsed.fragment and _url_component_has_secret(parsed.fragment, plus_as_space=False):
        return "source.fragment_secret_forbidden"

    if scheme == "project":
        remainder = value.split("://", 1)[1] if "://" in value else ""
        normalized_remainder = remainder.replace("\\", "/")
        if (
            not normalized_remainder
            or normalized_remainder.startswith("/")
            or re.match(r"(?i)^[a-z]:/", normalized_remainder)
            or any(
                component == ".."
                for component in normalized_remainder.split("/")
            )
        ):
            return "source.project_path_escape_forbidden"

    path_value = parsed.path if len(parsed.scheme) > 1 else slash_path
    if scheme in {"project", "local-file"} and parsed.netloc:
        path_value = parsed.netloc + "/" + path_value.lstrip("/")
    raw_components = [
        component
        for component in path_value.replace("\\", "/").split("/")
        if component
    ]
    for index, component in enumerate(raw_components):
        is_windows_drive = index == 0 and re.fullmatch(r"[A-Za-z]:", component)
        if ":" in component and not is_windows_drive:
            return "source.ntfs_stream_forbidden"
    components = [component.casefold().rstrip(" .") for component in raw_components]
    if not components:
        return None
    name = components[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example") or name.endswith(".env"):
        return "source.secret_file_forbidden"
    if name in _FORBIDDEN_FILE_NAMES or any(name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES):
        return "source.secret_file_forbidden"
    if any(component in _SECRET_DIRECTORIES for component in components):
        return "source.secret_directory_forbidden"

    joined = "/".join(components)
    browser_profile = (
        ("chrome/user data/" in joined or "edge/user data/" in joined)
        and name in {"cookies", "login data", "web data"}
    ) or ("firefox/profiles/" in joined and name in {"cookies.sqlite", "key4.db", "logins.json"})
    if browser_profile:
        return "source.browser_profile_forbidden"
    return None


def _source_rejection_reason(source_uri: str) -> str | None:
    value = _normalize_text(source_uri).strip()
    variants = [value]
    candidate = value
    for _ in range(2):
        decoded = _normalize_text(unquote(candidate))
        if decoded == candidate:
            break
        variants.append(decoded)
        candidate = decoded
    for variant in variants:
        reason = _source_variant_rejection_reason(variant)
        if reason:
            return reason
    return None


def inspect_memory_payload(value: Any, *, source_uri: str | None = None) -> PrivacyDecision:
    """Return a payload-safe decision for a prospective durable memory value."""

    if source_uri is not None:
        if not isinstance(source_uri, str):
            return PrivacyDecision(
                PrivacyAction.REJECT,
                Sensitivity.SENSITIVE,
                ("source.invalid",),
            )
        source_reason = _source_rejection_reason(source_uri)
        if source_reason:
            return PrivacyDecision(
                PrivacyAction.REJECT,
                Sensitivity.SECRET,
                (source_reason,),
            )

    leaves: list[str] = []
    try:
        normalized = _normalize_structure(
            value,
            budget=_WalkBudget(MEMORY_MAX_DEPTH, MEMORY_MAX_ITEMS),
            depth=0,
            active_containers=set(),
            text_leaves=leaves,
        )
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MEMORY_MAX_BYTES:
            raise _PolicyViolation("content.too_large")
    except _PolicyViolation as exc:
        sensitivity = Sensitivity.SECRET if exc.reason_code.startswith("secret.") else Sensitivity.INTERNAL
        return PrivacyDecision(PrivacyAction.REJECT, sensitivity, (exc.reason_code,))
    except (TypeError, ValueError, UnicodeError):
        return PrivacyDecision(
            PrivacyAction.REJECT,
            Sensitivity.INTERNAL,
            ("content.invalid",),
        )

    reasons: list[str] = []
    for leaf in leaves:
        reasons.extend(_text_secret_reasons(leaf))
    # A rolling view also catches a token intentionally split across adjacent
    # structured leaves.  It is bounded by the accepted payload size.
    reasons.extend(_text_secret_reasons("".join(leaves)))
    secret_reasons = tuple(dict.fromkeys(reasons))
    if secret_reasons:
        return PrivacyDecision(PrivacyAction.REJECT, Sensitivity.SECRET, secret_reasons)

    sensitivity = Sensitivity.SENSITIVE if any(_EMAIL_RE.search(leaf) for leaf in leaves) else Sensitivity.INTERNAL
    return PrivacyDecision(PrivacyAction.ALLOW, sensitivity, (), normalized)


def normalize_source_reference(source_uri: str) -> str:
    """Normalize a source URI after applying the same reject-only policy."""

    if not isinstance(source_uri, str):
        raise MemoryPrivacyError("source.invalid")
    normalized = _normalize_text(source_uri).strip()
    reason = _source_rejection_reason(normalized)
    if reason:
        raise MemoryPrivacyError(reason)
    secret_reasons = _text_secret_reasons(normalized, include_entropy=False)
    if secret_reasons:
        raise MemoryPrivacyError(secret_reasons[0], secret_reasons[1:])
    return normalized


def validate_memory_payload(subject: str, value: Any, source_uri: str | None = None) -> None:
    """Validate a durable memory payload, raising a payload-free error."""

    if not isinstance(subject, str) or not _SUBJECT_RE.fullmatch(subject):
        raise MemoryPrivacyError("privacy.subject_invalid")
    decision = inspect_memory_payload(value, source_uri=source_uri)
    if not decision.allowed:
        reason = decision.reason_codes[0] if decision.reason_codes else "privacy.rejected"
        raise MemoryPrivacyError(reason, decision.reason_codes[1:])


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "… [truncated by privacy policy]"
    return value[: max(0, limit - len(marker))] + marker


def sanitize_task_text(text: str, label: str = "task") -> str:
    """Return a bounded redacted task summary and never raise.

    ``label`` exists for call-site clarity but is intentionally not copied into
    output or errors because callers may accidentally pass private metadata.
    """

    del label
    try:
        if not isinstance(text, str):
            return OMITTED
        # Only the prefix can survive the final bound.  The larger scan window
        # catches a secret that crosses the eventual truncation boundary.
        prefix = text[: TASK_TEXT_MAX_CHARS * 4]
        normalized = _normalize_text(prefix)
        redacted, _ = _redact_text(normalized)
        return _truncate_text(redacted.strip(), TASK_TEXT_MAX_CHARS) or "[EMPTY]"
    except Exception:
        return OMITTED


def _sanitize_task_value(
    value: Any,
    *,
    depth: int,
    seen: set[int],
    item_budget: list[int],
) -> Any:
    if depth > MEMORY_MAX_DEPTH:
        return OMITTED
    item_budget[0] += 1
    if item_budget[0] > MEMORY_MAX_ITEMS:
        return OMITTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else OMITTED
    if isinstance(value, str):
        return sanitize_task_text(value, "metadata")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[OMITTED:binary]"
    if not isinstance(value, (dict, list, tuple)):
        return "[OMITTED:unsupported]"
    identity = id(value)
    if identity in seen:
        return "[OMITTED:cyclic]"
    seen.add(identity)
    try:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if not isinstance(key, str):
                    safe_key = f"field_{index}"
                else:
                    safe_key = _truncate_text(_normalize_text(key), 128) or f"field_{index}"
                    if _text_secret_reasons(safe_key):
                        safe_key = f"redacted_field_{index}"
                if safe_key in result:
                    safe_key = f"field_{index}"
                if isinstance(key, str) and _is_secret_key(key):
                    result[safe_key] = REDACTED
                else:
                    result[safe_key] = _sanitize_task_value(
                        item,
                        depth=depth + 1,
                        seen=seen,
                        item_budget=item_budget,
                    )
            return result
        return [
            _sanitize_task_value(
                item,
                depth=depth + 1,
                seen=seen,
                item_budget=item_budget,
            )
            for item in value[:MEMORY_MAX_ITEMS]
        ]
    finally:
        seen.remove(identity)


def sanitize_task_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact and bound task metadata without blocking execution."""

    try:
        if not isinstance(metadata, dict):
            return {"_privacy": OMITTED}
        sanitized = _sanitize_task_value(metadata, depth=0, seen=set(), item_budget=[0])
        if not isinstance(sanitized, dict):
            return {"_privacy": OMITTED}
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > TASK_METADATA_MAX_BYTES:
            return {"_privacy": "[OMITTED:payload-too-large]"}
        return sanitized
    except Exception:
        return {"_privacy": "[OMITTED:invalid-metadata]"}


def sanitize_reference(reference: str) -> str:
    """Return a safe bounded reference without reading or resolving its target."""

    try:
        if not isinstance(reference, str):
            return FORBIDDEN_REFERENCE
        if _source_rejection_reason(reference):
            return FORBIDDEN_REFERENCE
        return sanitize_task_text(reference, "reference")
    except Exception:
        return FORBIDDEN_REFERENCE


def sanitize_export_value(value: Any) -> Any:
    """Defense-in-depth validation for export; return normalized data or raise."""

    decision = inspect_memory_payload(value)
    if not decision.allowed:
        reason = decision.reason_codes[0] if decision.reason_codes else "privacy.export_rejected"
        raise MemoryPrivacyError(reason, decision.reason_codes[1:])
    return decision.normalized_value


__all__ = [
    "FORBIDDEN_REFERENCE",
    "MEMORY_MAX_BYTES",
    "MemoryPrivacyError",
    "PrivacyAction",
    "PrivacyDecision",
    "REDACTED",
    "Sensitivity",
    "inspect_memory_payload",
    "normalize_source_reference",
    "sanitize_export_value",
    "sanitize_reference",
    "sanitize_task_metadata",
    "sanitize_task_text",
    "validate_memory_payload",
]
