"""Validate the governance/architecture foundation without touching secret stores."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


REQUIRED_DOCUMENTS = (
    "SYSTEM_MANIFEST.md",
    "constitution/CORE.md",
    "constitution/REASONING.md",
    "constitution/CODING.md",
    "constitution/MEMORY.md",
    "constitution/TOOL_USE.md",
    "constitution/SECURITY.md",
    "constitution/PRIVACY.md",
    "constitution/COMMUNICATION.md",
    "constitution/SELF_IMPROVEMENT.md",
    "docs/PROJECT_CHARTER.md",
    "docs/GLOSSARY.md",
    "docs/PERMISSIONS.md",
    "docs/SECURITY_MODEL.md",
    "docs/ROADMAP.md",
    "docs/CURRENT_STATE.md",
    "docs/ARCHITECTURE.md",
    "docs/TARGET_ARCHITECTURE.md",
    "docs/CONTEXT_STRATEGY.md",
    "docs/MEMORY_STRATEGY.md",
    "docs/CODEX_HANDOFF.md",
    "docs/OPERATIONS.md",
    "docs/CONFIGURATION.md",
    "docs/CONTRACTS.md",
    "docs/ROUTING.md",
)

ROADMAP = "docs/ROADMAP.md"
EXPECTED_STAGES = tuple(f"{stage:03d}" for stage in range(13))
FOUNDATION_CANDIDATES = (
    *REQUIRED_DOCUMENTS,
    "docs/adr/0001-governance-and-evidence.md",
    "docs/adr/0002-local-first-and-codex-boundary.md",
    "docs/adr/0003-permissions-and-protected-change.md",
    "docs/adr/0004-versioned-boundary-contracts.md",
    "docs/adr/0005-configuration-and-health-semantics.md",
    "docs/adr/0006-process-ownership-and-resource-boundaries.md",
    "docs/adr/0007-deterministic-planner-router.md",
    "config/platform.json",
    "config/routing.json",
    "scripts/validate_foundation.py",
    "scripts/process-ownership.ps1",
    "services/config.py",
    "services/contracts/__init__.py",
    "services/contracts/v1.py",
    "services/health.py",
    "services/orchestration/config.py",
    "services/orchestration/normalizer.py",
    "services/orchestration/planner.py",
    "services/orchestration/router.py",
    "services/orchestration/handoff.py",
    "tests/test_config.py",
    "tests/test_contracts.py",
    "tests/test_foundation_validator.py",
    "tests/test_gateway_contracts.py",
    "tests/test_health.py",
    "tests/test_process_ownership.py",
    "tests/test_routing.py",
    "tests/test_routing_config.py",
    "tests/test_routing_eval.py",
    "tests/test_execution_policy.py",
    "tests/test_task_state_store.py",
)

# This is the public, non-secret configuration contract. Values come from
# .env.example; the validator never opens .env or another secret store.
PUBLIC_CONFIG_KEYS = (
    "LOCAL_FAST_MODEL",
    "LOCAL_STRONG_MODEL",
    "LOCAL_AGENT_MODEL",
    "CODEX_MODEL",
    "OLLAMA_BASE_URL",
    "FAST_OLLAMA_BASE_URL",
    "GATEWAY_PORT",
    "VOICE_PORT",
    "OPEN_WEBUI_PORT",
    "N8N_PORT",
    "COMFYUI_URL",
    "WHISPER_MODEL",
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str | None = None

    def format(self) -> str:
        location = f" ({self.path})" if self.path else ""
        return f"{self.code}: {self.message}{location}"


@dataclass(frozen=True)
class ManifestFact:
    name: str
    value: str
    aliases: tuple[str, ...]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _strip_fenced_code(text: str) -> str:
    """Remove fenced and inline code so examples are not treated as links/stages."""

    visible: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = None, 0
            visible.append("")
            continue
        if fence_char is not None:
            visible.append("")
            continue
        visible.append(re.sub(r"`[^`\n]*`", "", line))
    return "\n".join(visible)


def check_required_documents(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in REQUIRED_DOCUMENTS:
        path = root / relative
        if not path.is_file():
            findings.append(Finding("document.missing", "required document is missing", relative))
        elif path.stat().st_size == 0:
            findings.append(Finding("document.empty", "required document is empty", relative))
    return findings


def check_roadmap_stages(root: Path) -> list[Finding]:
    path = root / ROADMAP
    if not path.is_file():
        return [Finding("roadmap.missing", "roadmap is missing", ROADMAP)]

    text = _strip_fenced_code(_read_text(path))
    counts = Counter(re.findall(r"(?m)^\|\s*(\d{3})\s*\|", text))
    findings: list[Finding] = []
    for stage in EXPECTED_STAGES:
        count = counts.get(stage, 0)
        if count == 0:
            findings.append(Finding("roadmap.stage_missing", f"stage {stage} is missing", ROADMAP))
        elif count > 1:
            findings.append(
                Finding("roadmap.stage_duplicate", f"stage {stage} occurs {count} times", ROADMAP)
            )
    for stage in sorted(set(counts) - set(EXPECTED_STAGES)):
        findings.append(Finding("roadmap.stage_unexpected", f"unexpected stage {stage}", ROADMAP))
    return findings


def _markdown_files(root: Path) -> list[Path]:
    files = {path for path in root.glob("*.md") if path.is_file()}
    for directory in ("constitution", "docs"):
        base = root / directory
        if base.is_dir():
            files.update(path for path in base.rglob("*.md") if path.is_file())
    return sorted(files)


_MARKDOWN_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|(?:[^()\n]|\([^()\n]*\))+?)\s*\)"
)
_REFERENCE_DEFINITION = re.compile(
    r"(?m)^\s{0,3}\[(?!\^)[^\]\n]+\]:\s*(<[^>\n]+>|\S+)"
)
_REMOTE_LINK_SCHEMES = {"http", "https", "mailto", "tel", "data"}


def _link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")].strip()
    # A title may follow the path: (file.md "title"). Whitespace inside a path
    # should be encoded or enclosed in angle brackets in valid Markdown.
    return re.split(r"\s+(?=[\"'])", raw, maxsplit=1)[0].strip()


def _check_link_target(root: Path, document: Path, raw_target: str) -> Finding | None:
    target = _link_target(raw_target)
    if not target or target.startswith("#"):
        return None

    parsed = urlsplit(target)
    scheme = parsed.scheme.casefold()
    if target.startswith("//") or scheme in _REMOTE_LINK_SCHEMES:
        return None
    if scheme or re.match(r"^[A-Za-z]:[\\/]", target):
        return Finding(
            "link.absolute",
            f"repository documentation link must be relative: {target}",
            _relative(document, root),
        )

    target_without_fragment = target.split("#", 1)[0].split("?", 1)[0]
    if not target_without_fragment:
        return None
    target_without_fragment = unquote(target_without_fragment).replace("/", os.sep)
    if Path(target_without_fragment).is_absolute():
        return Finding(
            "link.absolute",
            f"repository documentation link must be relative: {target}",
            _relative(document, root),
        )

    destination = (document.parent / target_without_fragment).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError:
        return Finding(
            "link.outside_repo",
            f"relative link leaves the repository: {target}",
            _relative(document, root),
        )
    if not destination.exists():
        return Finding(
            "link.missing",
            f"relative link target does not exist: {target}",
            _relative(document, root),
        )
    return None


def check_markdown_links(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for document in _markdown_files(root):
        text = _strip_fenced_code(_read_text(document))
        raw_targets = [match.group(1) for match in _MARKDOWN_LINK.finditer(text)]
        raw_targets.extend(match.group(1) for match in _REFERENCE_DEFINITION.finditer(text))
        for raw_target in raw_targets:
            finding = _check_link_target(root, document, raw_target)
            if finding:
                findings.append(finding)
    return findings


def _parse_public_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.casefold()}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return value.rstrip("/")


def _manifest_facts(config: dict[str, str]) -> tuple[ManifestFact, ...]:
    fast_model = config["LOCAL_FAST_MODEL"]
    strong_model = config["LOCAL_STRONG_MODEL"]
    agent_model = config["LOCAL_AGENT_MODEL"]
    return (
        ManifestFact("fast model", fast_model, (fast_model, "fast model")),
        ManifestFact("strong model", strong_model, (strong_model, "strong model")),
        ManifestFact(
            "local coding agent model",
            agent_model,
            ("qwen code", "coding agent", "local agent"),
        ),
        ManifestFact("Codex model", config["CODEX_MODEL"], ("codex",)),
        ManifestFact("Whisper model", config["WHISPER_MODEL"], ("voice", "whisper")),
        ManifestFact(
            "fast Ollama endpoint",
            _endpoint(config["FAST_OLLAMA_BASE_URL"]),
            (fast_model, "fast ollama"),
        ),
        ManifestFact(
            "strong Ollama endpoint",
            _endpoint(config["OLLAMA_BASE_URL"]),
            (strong_model, "strong ollama"),
        ),
        ManifestFact("gateway endpoint", f"http://127.0.0.1:{config['GATEWAY_PORT']}", ("gateway",)),
        ManifestFact(
            "voice endpoint",
            f"http://127.0.0.1:{config['VOICE_PORT']}",
            ("voice", "whisper"),
        ),
        ManifestFact(
            "Open WebUI endpoint",
            f"http://127.0.0.1:{config['OPEN_WEBUI_PORT']}",
            ("open webui", "webui"),
        ),
        ManifestFact("n8n endpoint", f"http://127.0.0.1:{config['N8N_PORT']}", ("n8n",)),
        ManifestFact("ComfyUI endpoint", _endpoint(config["COMFYUI_URL"]), ("comfyui",)),
    )


def _contains_manifest_token(line: str, token: str) -> bool:
    escaped = re.escape(token.casefold())
    return bool(re.search(rf"(?<![a-z0-9_.-]){escaped}(?![a-z0-9_.-])", line))


def _modelfile_base(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in _read_text(path).splitlines():
        match = re.match(r"^\s*FROM\s+(\S+)", line, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def check_manifest_contract(root: Path) -> list[Finding]:
    example = root / ".env.example"
    manifest_path = root / "SYSTEM_MANIFEST.md"
    if not example.is_file():
        return [Finding("manifest.config_missing", "public configuration template is missing", ".env.example")]
    if not manifest_path.is_file():
        return [Finding("manifest.missing", "SYSTEM_MANIFEST.md is missing", "SYSTEM_MANIFEST.md")]

    config = _parse_public_config(example)
    findings: list[Finding] = []
    for key in PUBLIC_CONFIG_KEYS:
        if not config.get(key):
            findings.append(
                Finding("manifest.config_key_missing", f"public configuration key {key} is missing", ".env.example")
            )
    if findings:
        return findings

    facts = list(_manifest_facts(config))
    for profile_key, relative in (
        ("LOCAL_FAST_MODEL", "models/fast.Modelfile"),
        ("LOCAL_STRONG_MODEL", "models/strong.Modelfile"),
    ):
        base = _modelfile_base(root / relative)
        if not base:
            findings.append(
                Finding("manifest.modelfile_invalid", "model profile has no readable FROM value", relative)
            )
            continue
        facts.append(
            ManifestFact(
                f"base model for {config[profile_key]}",
                base,
                (config[profile_key],),
            )
        )

    lines = [line.casefold() for line in _read_text(manifest_path).splitlines()]
    for fact in facts:
        if not any(
            _contains_manifest_token(line, fact.value)
            and any(_contains_manifest_token(line, alias) for alias in fact.aliases)
            for line in lines
        ):
            findings.append(
                Finding(
                    "manifest.fact_missing",
                    f"{fact.name} must map to {fact.value!r} on one manifest line",
                    "SYSTEM_MANIFEST.md",
                )
            )
    return findings


_FORBIDDEN_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
    ".gguf",
    ".safetensors",
    ".ckpt",
}
_FORBIDDEN_NAMES = {
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    ".envrc",
    "auth.json",
    "credentials.json",
    "secrets.json",
    "cookies.txt",
    "id_rsa",
    "id_ed25519",
}
_SECRET_DIRECTORIES = {".ssh", ".aws", ".azure", ".kube", ".docker"}
_GENERATED_DIRECTORIES = {
    "data",
    "inbox",
    "logs",
    "modules",
    "n8n_data",
    "open-webui_data",
    "open_webui_data",
    "outputs",
    "run",
}
_MAX_UNTRACKED_TEXT_BYTES = 2 * 1024 * 1024


def _is_forbidden_path(relative: str) -> bool:
    path = Path(relative)
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    if name == ".gitkeep":
        return False
    if name != ".env.example" and (
        name == ".env"
        or name.endswith(".env")
        or name.startswith(".env.")
        or ".env." in name
    ):
        return True
    if name in _FORBIDDEN_NAMES or path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        return True
    if any(part in _SECRET_DIRECTORIES for part in parts[:-1]):
        return True
    if parts and parts[0] in _GENERATED_DIRECTORIES:
        return True
    data_like_suffixes = {"", ".json", ".txt", ".yaml", ".yml", ".ini", ".toml"}
    if path.suffix.casefold() in data_like_suffixes and re.search(
        r"(?:^|[-_.])(tokens?|secrets?|cookies?|credentials?)(?:[-_.]|$)", name
    ):
        return True
    return False


def _run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_path_list(root: Path, arguments: list[str]) -> tuple[list[str], str | None]:
    result = _run_git(root, arguments)
    if result.returncode != 0:
        return [], result.stderr.strip() or "git path query failed"
    return [item for item in result.stdout.split("\0") if item], None


def _changed_paths(root: Path) -> tuple[list[str], str | None]:
    paths: set[str] = set()
    for arguments in (
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", "--"],
        ["diff", "--name-only", "-z", "--diff-filter=ACMR", "--"],
    ):
        current, error = _git_path_list(root, arguments)
        if error:
            return [], error
        paths.update(current)
    return sorted(paths), None


def _untracked_foundation_candidates(root: Path) -> list[str]:
    untracked: list[str] = []
    for relative in dict.fromkeys(FOUNDATION_CANDIDATES):
        path = root / relative
        if not path.is_file():
            continue
        tracked = _run_git(root, ["ls-files", "--error-unmatch", "--", relative])
        if tracked.returncode != 0:
            untracked.append(relative)
    return untracked


def _parse_added_lines(diff: str, source: str) -> list[tuple[str, str, str]]:
    current_path = ""
    added: list[tuple[str, str, str]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = ""
            continue
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append((source, current_path, line[1:]))
    return added


def _added_diff_lines(
    root: Path, safe_paths: list[str]
) -> tuple[list[tuple[str, str, str]], str | None]:
    if not safe_paths:
        return [], None
    added: list[tuple[str, str, str]] = []
    for source, prefix in (
        ("staged", ["diff", "--cached"]),
        ("unstaged", ["diff"]),
    ):
        arguments = [*prefix, "--no-ext-diff", "--no-color", "--unified=0", "--", *safe_paths]
        result = _run_git(root, arguments)
        if result.returncode != 0:
            return [], result.stderr.strip() or f"{source} git diff failed"
        added.extend(_parse_added_lines(result.stdout, source))
    return added, None


_SPECIFIC_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
_GENERIC_SECRET = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|access[_-]?token|[_-]?auth[_-]?token|"
    r"client[_-]?secret|refresh[_-]?token|telegram[_-]?bot[_-]?token|bot[_-]?token|"
    r"github[_-]?(?:pat|token)|private[_-]?key|password|secret|token)\b"
    r"\s*[:=]\s*[\"']?([^\s\"'`,;]{12,})"
)
_PLACEHOLDER_PREFIXES = (
    "$",
    "%",
    "<",
    "{",
    "your-",
    "your_",
    "example",
    "placeholder",
    "changeme",
    "replace",
    "redacted",
    "dummy",
    "test-",
    "local-",
    "***",
)


def _secret_kind(line: str) -> str | None:
    for pattern in _SPECIFIC_SECRET_PATTERNS:
        if pattern.search(line):
            return "credential-shaped value"
    generic = _GENERIC_SECRET.search(line)
    if not generic:
        return None
    candidate = generic.group(1).strip().casefold()
    if candidate.startswith(_PLACEHOLDER_PREFIXES) or candidate in {"none", "null", "false"}:
        return None
    return "assigned secret-shaped value"


def _untracked_secret_findings(root: Path, paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in paths:
        if _is_forbidden_path(relative):
            findings.append(
                Finding(
                    "git.forbidden_path",
                    "forbidden or secret-bearing untracked file is present; content not read",
                    relative,
                )
            )
            continue
        path = root / relative
        if (
            not path.is_file()
            or path.stat().st_size > _MAX_UNTRACKED_TEXT_BYTES
        ):
            continue
        try:
            lines = _read_text(path).splitlines()
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            kind = _secret_kind(line)
            if kind:
                findings.append(
                    Finding(
                        "git.secret_candidate",
                        f"{kind} found in untracked text line {line_number}; value not printed",
                        relative,
                    )
                )
    return findings


def check_git_diff_secrets(root: Path) -> list[Finding]:
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return [Finding("git.unavailable", "root is not a Git worktree")]

    changed_paths, error = _changed_paths(root)
    if error:
        return [Finding("git.diff_failed", error)]
    findings = _untracked_secret_findings(root, _untracked_foundation_candidates(root))
    safe_paths: list[str] = []
    for relative in changed_paths:
        if _is_forbidden_path(relative):
            findings.append(
                Finding("git.forbidden_path", "forbidden or secret-bearing file is in the Git diff", relative)
            )
        else:
            safe_paths.append(relative)

    # Diff content is requested only for non-sensitive paths. A changed .env,
    # key, cookie, credential or generated-data path is reported by name and is
    # never included in this content command.
    lines, error = _added_diff_lines(root, safe_paths)
    if error:
        findings.append(Finding("git.diff_failed", error))
        return findings
    for line_number, (source, relative, line) in enumerate(lines, start=1):
        kind = _secret_kind(line)
        if kind:
            findings.append(
                Finding(
                    "git.secret_candidate",
                    f"{kind} found in {source} added diff line #{line_number}; value not printed",
                    relative or None,
                )
            )
    return findings


def run_checks(root: Path) -> list[tuple[str, list[Finding]]]:
    return [
        ("required documents", check_required_documents(root)),
        ("roadmap stages 000-012", check_roadmap_stages(root)),
        ("relative Markdown links", check_markdown_links(root)),
        ("manifest endpoint/model contract", check_manifest_contract(root)),
        ("tracked/staged Git diff and explicit foundation candidates", check_git_diff_secrets(root)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()

    failures = 0
    for name, findings in run_checks(root):
        if not findings:
            print(f"[PASS] foundation: {name}")
            continue
        failures += len(findings)
        print(f"[FAIL] foundation: {name} ({len(findings)} finding(s))")
        for finding in findings:
            print(f"       - {finding.format()}")
    if failures:
        print(f"FOUNDATION_FAILED findings={failures}")
        return 1
    print("FOUNDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
