from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class DeclaredCodingScope:
    """Repository-relative scopes deterministically derived at ingress.

    ``rule_scope_paths`` may include read-only or explicitly protected path
    references.  ``expected_diff_paths`` is deliberately narrower: it contains
    only the explicit nested workspace boundary and positive mutation targets.
    ``forbidden_diff_paths`` preserves explicit negative mutation constraints
    independently, including when the positive allowlist is empty.  A path
    mention alone must never grant write permission.
    """

    rule_scope_paths: tuple[str, ...]
    expected_diff_paths: tuple[str, ...]
    forbidden_diff_paths: tuple[str, ...]


class CodingScopeError(ValueError):
    """The declared path scope could not be represented safely and boundedly."""


@dataclass(frozen=True, slots=True)
class _PathReference:
    start: int
    end: int
    value: str


_PROJECT_DECLARATION = re.compile(
    r"\b(?:project|repo|repository|project path|workspace|проект|репозиторий)\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n;]*)",
    re.IGNORECASE,
)
_URL = re.compile(r"\b(?:https?|file)://[^\s<>]+", re.IGNORECASE)
_TOKEN = re.compile(
    r"`(?P<backtick>[^`\r\n]+)`"
    r'|"(?P<double>[^"\r\n]+)"'
    r"|'(?P<single>[^'\r\n]+)'"
    r"|(?P<bare>[^\s,;(){}\[\]<>]+)"
)
_MUTATION_ACTION = re.compile(
    r"\b(?:create|add|write|edit|modify|change|update|delete|remove|fix|generate|"
    r"implement|replace|touch|"
    r"создай|создать|создавай|создавать|добавь|добавить|добавляй|добавлять|"
    r"запиши|записать|записывай|записывать|отредактируй|редактировать|редактируй|"
    r"измени|изменить|изменяй|изменять|обнови|обновить|обновляй|обновлять|"
    r"удали|удалить|удаляй|удалять|исправь|исправить|исправляй|исправлять|"
    r"сгенерируй|сгенерировать|реализуй|реализовать|замени|заменить)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_ACTION = re.compile(
    r"(?:\bdo\s+not\b|\bdon't\b|\bnever\b|\bwithout\b|\bno\b|"
    r"\bне\b|\bнельзя\b|\bникогда\s+не\b)[^.;!?\r\n]{0,48}$",
    re.IGNORECASE,
)
_POSITIVE_EXCEPTION = re.compile(
    r"\b(?:other\s+than|except|за\s+исключением|кроме)\b[^.;!?\r\n]{0,48}$",
    re.IGNORECASE,
)
_SOURCE_ONLY_BOUNDARY = re.compile(
    r"\b(?:using|from|based\s+on|according\s+to|after\s+reading|by\s+reading|"
    r"с\s+помощью|на\s+основе|согласно|после\s+чтения|из)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(r"[;!?\r\n]|\.(?=\s|$)")
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")

_KNOWN_BASENAMES = {
    "agents.md",
    "cmakelists.txt",
    "containerfile",
    "dockerfile",
    "gemfile",
    "justfile",
    "makefile",
    "procfile",
    "readme",
    "rakefile",
}
_KNOWN_EXTENSIONS = {
    ".bat", ".bin", ".c", ".cc", ".cfg", ".cljs", ".clj", ".cmd", ".conf",
    ".cpp", ".cs", ".css", ".csv", ".cxx", ".dart", ".diff", ".dockerfile",
    ".env", ".ex", ".exs", ".fs", ".fsx", ".go", ".gradle", ".graphql", ".h",
    ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt",
    ".kts", ".less", ".lock", ".lua", ".md", ".mjs", ".mm", ".php", ".pl",
    ".pm", ".properties", ".proto", ".ps1", ".py", ".pyi", ".rb", ".rs", ".rst",
    ".sass", ".scala", ".scss", ".sh", ".sql", ".svelte", ".swift", ".toml",
    ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".zig",
}
_BLOCKED_PARTS = {".git", ".hg", ".svn"}
_MAX_DECLARED_SCOPE_PATHS = 256


def _mask(pattern: re.Pattern[str], value: str) -> str:
    return pattern.sub(lambda match: " " * (match.end() - match.start()), value)


def _clean_token(value: str) -> str:
    cleaned = value.strip().strip("`\"'")
    cleaned = _LINE_SUFFIX.sub("", cleaned)
    return cleaned.rstrip(".,;:!?)]}")


def _looks_like_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if not normalized or any(char in normalized for char in ("\x00", "*", "?")):
        return False
    if "/" in normalized:
        return True
    name = PurePosixPath(normalized).name.casefold()
    if name in _KNOWN_BASENAMES:
        return True
    return PurePosixPath(name).suffix.casefold() in _KNOWN_EXTENSIONS


def _path_references(masked_prompt: str) -> list[_PathReference]:
    references: list[_PathReference] = []
    for match in _TOKEN.finditer(masked_prompt):
        raw = next(
            value
            for value in (
                match.group("backtick"),
                match.group("double"),
                match.group("single"),
                match.group("bare"),
            )
            if value is not None
        )
        cleaned = _clean_token(raw)
        if _looks_like_path(cleaned):
            references.append(
                _PathReference(start=match.start(), end=match.end(), value=cleaned)
            )
            if len(references) > _MAX_DECLARED_SCOPE_PATHS:
                raise CodingScopeError("declared coding path scope exceeds the policy limit")
    return references


def _normalise_reference(
    value: str,
    *,
    requested_project: Path,
    canonical_root: Path,
) -> str | None:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith(("//", "\\\\"))
        or re.match(r"^[A-Za-z]:[^/]", normalized)
    ):
        return None
    lexical = PurePosixPath(normalized)
    if ".." in lexical.parts or lexical in {PurePosixPath("."), PurePosixPath("/")}:
        return None

    # On Windows ``Path`` accepts forward slashes directly; on POSIX the
    # same representation is native.  Keep absolute in-repository references
    # supportable while rejecting every external/UNC target.
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = requested_project / Path(*lexical.parts)
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(canonical_root).as_posix()
    except (OSError, ValueError):
        return None
    if relative in {"", "."}:
        return None
    folded_parts = {part.casefold() for part in PurePosixPath(relative).parts}
    if folded_parts.intersection(_BLOCKED_PARTS):
        return None
    if any(part == ".env" or part.startswith(".env.") for part in folded_parts):
        return None
    return relative


def _mutation_reference_kind(
    masked_prompt: str,
    reference: _PathReference,
) -> str | None:
    """Classify one path reference as a positive or forbidden mutation.

    Negation belongs to the mutation action, not to every later action in the
    same prose clause.  In particular, ``do not modify Dockerfile, create
    src/app.py`` must protect Dockerfile while keeping ``src/app.py`` as the
    positive target.  Looking only at a broad prefix incorrectly propagated
    the first ``do not`` across the second action and collapsed the allowlist
    to empty, which the reviewer historically interpreted as unrestricted.
    """

    prefix = masked_prompt[: reference.start]
    clause_boundary = -1
    for boundary in _CLAUSE_BOUNDARY.finditer(prefix):
        clause_boundary = boundary.start()
    actions = [
        match for match in _MUTATION_ACTION.finditer(prefix)
        if match.start() > clause_boundary
    ]
    if not actions:
        return None
    action = actions[-1]
    if reference.start - action.end() > 192:
        return None
    previous_action = actions[-2] if len(actions) > 1 else None
    action_context_start = (
        previous_action.end() if previous_action is not None else clause_boundary + 1
    )
    before_action = masked_prompt[
        max(action_context_start, action.start() - 64) : action.start()
    ]
    between = masked_prompt[action.end() : reference.start]
    negated = bool(_NEGATION_BEFORE_ACTION.search(before_action))
    if _SOURCE_ONLY_BOUNDARY.search(between):
        return None
    if negated and not _POSITIVE_EXCEPTION.search(between):
        return "forbidden"
    return "positive"


def _collapse_scopes(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if any(value == existing or value.startswith(f"{existing}/") for existing in result):
            continue
        result = [
            existing
            for existing in result
            if not existing.startswith(f"{value}/")
        ]
        result.append(value)
    return tuple(result)


def resolve_declared_coding_scope(
    prompt: str,
    *,
    requested_project: Path,
    canonical_root: Path,
    write: bool,
) -> DeclaredCodingScope:
    """Return conservative rule and write scopes for one gateway request.

    Ambiguous prose may intentionally produce no positive file allowlist, but
    explicit negative mutation references remain enforceable independently.
    The engine's post-executor rule expansion remains the fail-closed fallback
    for targets discovered by repository inspection rather than declared by
    the caller.
    """

    root = canonical_root.resolve(strict=True)
    requested = requested_project.resolve(strict=True)
    try:
        workspace = requested.relative_to(root).as_posix()
    except ValueError as exc:
        raise CodingScopeError("requested project is outside the canonical repository") from exc

    masked = _mask(_URL, _mask(_PROJECT_DECLARATION, prompt))
    references = _path_references(masked)
    normalized: list[tuple[_PathReference, str]] = []
    for reference in references:
        relative = _normalise_reference(
            reference.value,
            requested_project=requested,
            canonical_root=root,
        )
        if relative is not None:
            normalized.append((reference, relative))

    rule_values = [] if workspace == "." else [workspace]
    rule_values.extend(relative for _, relative in normalized)

    expected_values: list[str] = []
    forbidden_values: list[str] = []
    if write:
        if workspace != ".":
            expected_values.append(workspace)
        for reference, relative in normalized:
            kind = _mutation_reference_kind(masked, reference)
            if kind == "positive":
                expected_values.append(relative)
            elif kind == "forbidden":
                forbidden_values.append(relative)

    rule_scopes = _collapse_scopes(rule_values)
    expected_scopes = _collapse_scopes(expected_values)
    forbidden_scopes = _collapse_scopes(forbidden_values)
    conflicts = {
        path
        for path in expected_scopes
        if any(
            path == denied
            or path.startswith(f"{denied}/")
            for denied in forbidden_scopes
        )
    }
    if conflicts:
        raise CodingScopeError(
            "declared coding path scope contains conflicting positive and forbidden targets"
        )
    if (
        len(rule_scopes) > _MAX_DECLARED_SCOPE_PATHS
        or len(expected_scopes) > _MAX_DECLARED_SCOPE_PATHS
        or len(forbidden_scopes) > _MAX_DECLARED_SCOPE_PATHS
    ):
        raise CodingScopeError("declared coding path scope exceeds the policy limit")
    return DeclaredCodingScope(
        rule_scope_paths=rule_scopes,
        expected_diff_paths=expected_scopes,
        forbidden_diff_paths=forbidden_scopes,
    )


__all__ = [
    "CodingScopeError",
    "DeclaredCodingScope",
    "resolve_declared_coding_scope",
]
