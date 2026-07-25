from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import hmac
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from services.coding import CodingEngine, CodingEngineError
from services.coding.contracts import (
    CodingMode,
    CodingPermissionsV1,
    CodingRisk,
    CodingTaskRequestV1,
    CodingTaskResultV1,
    CodingTaskStatus,
    DataClassification,
    ExecutorKind,
    ReviewVerdict,
)
from services.coding.discovery import discover_verification_commands
from services.coding.git import CodingRepositoryError, resolve_repository
from services.coding.executors import (
    CodexExecutor,
    QwenExecutor,
    resolve_codex_executable,
)
from services.coding.process import ProcessTreeGuard, safe_child_environment
from services.coding.scope import CodingScopeError, resolve_declared_coding_scope
from services.common import INBOX_DIR, OUTPUT_DIR, ROOT, RUN_DIR, save_task, task_store_ready
from services.config import get_settings
from services.contracts import (
    AvailabilityStatus,
    AttachmentRefV1,
    DecisionStatus,
    ExecutionMode,
    ExecutorName,
    NormalizedRequestV1,
    PlanV1,
    RequestSource,
    RouteDecisionV1,
)
from services.health import CapabilityHealthV1, CapabilityStatus, aggregate_health
from services.knowledge.privacy import detect_secret
from services.memory.integration import attach_memory_to_planning
from services.mcp_hub.config import generate_qwen_views, load_registry, validate_installed_source
from services.mcp_hub.runtime import peek_status
from services.orchestration.handoff import (
    collect_modified_files,
    ensure_codex_handoff,
    redact_bounded,
)
from services.orchestration.config import get_routing_policy
from services.orchestration.normalizer import (
    attachment_references as normalized_attachment_references,
    flatten_content as normalized_flatten_content,
    last_user_text as normalized_last_user_text,
    normalize_messages,
    replace_last_user_text,
    resolve_project,
)
from services.orchestration.planner import (
    conservative_token_upper_bound,
    has_requested_mutation as planned_has_requested_mutation,
    is_read_only as planned_is_read_only,
    is_review_request as planned_is_review_request,
    plan_request,
    render_plan_execution_context,
)
from services.orchestration.router import (
    CapabilitySnapshot,
    FailureHistory,
    PermissionSnapshot,
    assumed_capabilities,
    route_request,
)

SETTINGS = get_settings()
OLLAMA_BASE_URL = SETTINGS.ollama_base_url
FAST_OLLAMA_BASE_URL = SETTINGS.fast_ollama_base_url
FAST_MODEL = SETTINGS.local_fast_model
STRONG_MODEL = SETTINGS.local_strong_model
AGENT_MODEL = SETTINGS.local_agent_model
CODEX_MODEL = SETTINGS.codex_model
CODEX_REASONING_EFFORT = SETTINGS.codex_reasoning_effort
DEFAULT_PROJECT = SETTINGS.default_project
ENABLE_LOCAL_CODE_EXEC = SETTINGS.enable_local_code_exec
ENABLE_CODEX_EXEC = SETTINGS.enable_codex_exec
CODEX_SANDBOX = SETTINGS.codex_sandbox
MAX_AUTOMATIC_CHAT_TOOLS = SETTINGS.max_automatic_chat_tools
GATEWAY_BEARER_CREDENTIAL = SETTINGS.gateway_credential.get_secret_value()
TRUSTED_GATEWAY_REQUEST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "trusted_gateway_request", default=False
)

app = FastAPI(title="Local Agent Gateway", version="0.2.0")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
logger = logging.getLogger("uvicorn.error")
_MCP_CORRELATION_ENV = "LOCESTRA_MCP_CORRELATION_ID"
_SAFE_MCP_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _validated_mcp_correlation_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        secret_like = detect_secret(value.encode("utf-8", errors="strict")) is not None
    except (UnicodeError, ValueError, RecursionError):
        secret_like = True
    if not _SAFE_MCP_CORRELATION_ID.fullmatch(value) or secret_like:
        raise RuntimeError("MCP correlation metadata is invalid")
    return value


@app.middleware("http")
async def authenticate_openai_boundary(request: Request, call_next):
    """Authenticate every OpenAI-compatible request before memory/execution."""

    if not request.url.path.startswith("/v1/"):
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if (
        not GATEWAY_BEARER_CREDENTIAL
        or not supplied
        or not hmac.compare_digest(supplied, GATEWAY_BEARER_CREDENTIAL)
    ):
        return JSONResponse(
            {
                "error": {
                    "message": "Gateway authentication is required.",
                    "type": "authentication_error",
                    "code": "authentication.required",
                }
            },
            status_code=401,
        )
    trust_context_handle = TRUSTED_GATEWAY_REQUEST.set(True)
    try:
        return await call_next(request)
    finally:
        TRUSTED_GATEWAY_REQUEST.reset(trust_context_handle)

AGENT_LOCK = asyncio.Lock()
CODEX_LOCK = asyncio.Lock()
IMAGE_LOCK = asyncio.Lock()
GPU_LOCK = asyncio.Lock()
FAST_MODEL_LOCK = asyncio.Lock()
DOCS_EXECUTION_LOCK = asyncio.Lock()
WORKTREE_LOCKS: dict[str, asyncio.Lock] = {}
ROUTING_CAPABILITY_CACHE: tuple[float, tuple[bool, bool], CapabilitySnapshot] | None = None
CODING_ENGINE_INSTANCE: CodingEngine | None = None
CODING_ENGINE_FACTORY_LOCK = threading.Lock()

_PUBLIC_DOCS_QUERY_MAX_CHARS = 4_096
_PUBLIC_DOCS_PROJECT_DECLARATION = re.compile(
    r"\b(?:project|repo|repository|\u043f\u0440\u043e\u0435\u043a\u0442|\u0440\u0435\u043f\u043e\u0437\u0438\u0442\u043e\u0440\u0438\u0439)\s*[:=]\s*"
    r"(?:\"(?:[A-Za-z]:[\\/]|\\\\|/)[^\"\r\n]+\"|'(?:[A-Za-z]:[\\/]|\\\\|/)[^'\r\n]+'|"
    r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\r\n;]+)\s*;?",
    re.IGNORECASE,
)
_PUBLIC_DOCS_ABSOLUTE_PATH = re.compile(
    r"(?:\"(?:[A-Za-z]:[\\/]|\\\\|/)[^\"\r\n]+\"|'(?:[A-Za-z]:[\\/]|\\\\|/)[^'\r\n]+'|"
    r"[A-Za-z]:[\\/][^\r\n;,<>|]+|\\\\[^\r\n;,<>|]+|"
    r"(?<![:/\w])/(?!/)[^\r\n;,<>|]+)",
    re.IGNORECASE,
)
_PUBLIC_DOCS_CODE_MARKUP = re.compile(
    r"```|~~~|<\s*/?\s*(?:code|pre|script|style)\b", re.IGNORECASE
)
_PUBLIC_DOCS_CODE_LINE = re.compile(
    r"(?im)^\s*(?:#!|>>>|PS>|"
    r"(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(|"
    r"class\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^\r\n]*\))?\s*:|"
    r"(?:from\s+[A-Za-z0-9_.]+\s+import|import\s+[A-Za-z0-9_.]+)|"
    r"(?:function|const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*|"
    r"#\s*include\s*[<\"]|using\s+namespace\s+|"
    r"(?:public|private|protected)\s+(?:static\s+)?(?:class|void|int|string)\b|"
    r"(?:select\b[^\r\n]+\bfrom|create\s+table|insert\s+into|update\b[^\r\n]+\bset)\b)"
)
_PUBLIC_DOCS_ASSIGNMENT_LINE = re.compile(
    r"(?m)^\s*[A-Za-z_$][A-Za-z0-9_.$\[\]'\"]*\s*(?::=|=(?!=))\s*\S+"
)
_DOCS_EPHEMERAL_ROOT = RUN_DIR / "ephemeral-docs"
_DOCS_CORE_TOOL_SENTINEL = "todo_write"
_DOCS_EXCLUDED_TOOLS = (
    "todo_write",
    "agent",
    "skill",
    "exit_plan_mode",
    "enter_plan_mode",
    "ask_user_question",
    "create_sub_session",
    "task_stop",
    "task_create",
    "task_update",
    "task_list",
    "team_create",
    "team_delete",
    "team_plan_approval",
    "send_message",
    "structured_output",
    "tool_search",
    "enter_worktree",
    "exit_worktree",
    "workflow",
    "artifact",
    "record_artifact",
)


class AgentContextBudgetExceeded(RuntimeError):
    """Raised before process launch when an executable Plan cannot fit."""


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "local-agent-auto"
    messages: list[dict[str, Any]]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    parallel_tool_calls: bool | None = None
    functions: list[dict[str, Any]] | None = None
    function_call: Any | None = None
    response_format: dict[str, Any] | None = None


def attachment_references(messages: list[dict[str, Any]]) -> list[AttachmentRefV1]:
    return normalized_attachment_references(messages)


def inline_audio_payload(messages: list[dict[str, Any]]) -> tuple[bytes, str, str]:
    limit = get_routing_policy().thresholds.max_attachment_bytes
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            break
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {"input_audio", "audio", "audio_url"}:
                continue
            if part.get("type") == "audio_url":
                audio_value = part.get("audio_url")
                encoded = audio_value.get("url") if isinstance(audio_value, dict) else audio_value
                audio = {"data": encoded, "format": "wav"}
            else:
                audio = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else part
                encoded = audio.get("data") if isinstance(audio, dict) else None
            audio_format = str(audio.get("format", "wav")) if isinstance(audio, dict) else "wav"
            if not isinstance(encoded, str):
                raise ValueError("inline audio data is missing")
            if encoded.startswith("data:"):
                if "," not in encoded or ";base64" not in encoded.split(",", 1)[0].casefold():
                    raise ValueError("inline audio must use base64 encoding")
                encoded = encoded.split(",", 1)[1]
            if len(encoded) > ((limit + 2) // 3) * 4 + 8:
                raise ValueError("audio attachment exceeds the routing policy limit")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("audio attachment is not valid base64") from exc
            if not payload or len(payload) > limit:
                raise ValueError("audio attachment is empty or exceeds the routing policy limit")
            safe_format = re.sub(r"[^a-zA-Z0-9]", "", audio_format).lower()[:16] or "wav"
            return payload, safe_format, f"audio/{safe_format}"
        break
    raise ValueError("audio attachment is missing")


async def transcribe_chat_audio(messages: list[dict[str, Any]]) -> str:
    payload, audio_format, media_type = inline_audio_payload(messages)
    async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0)) as client:
        response = await client.post(
            f"http://127.0.0.1:{SETTINGS.voice_port}/v1/audio/transcriptions",
            files={"file": (f"attachment.{audio_format}", payload, media_type)},
            data={"model": "whisper-1"},
            headers={"Authorization": f"Bearer {GATEWAY_BEARER_CREDENTIAL}"},
        )
        response.raise_for_status()
        body = response.json()
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Whisper returned no transcript")
    return text.strip()


def normalize_request(
    request: ChatRequest,
    *,
    request_id: str | None = None,
    source: RequestSource = RequestSource.API,
) -> NormalizedRequestV1:
    return normalize_messages(
        request.messages,
        default_project=DEFAULT_PROJECT,
        request_id=request_id,
        source=source,
    )


def normalize_entry_request(
    request: ChatRequest,
    *,
    request_id: str | None = None,
    source: RequestSource = RequestSource.API,
) -> NormalizedRequestV1:
    """Translate internal contract failures into a bounded public 422."""

    try:
        return normalize_request(request, request_id=request_id, source=source)
    except ValidationError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "normalized_request_invalid",
                "message": "The normalized request exceeds an internal contract limit or is invalid.",
            },
        ) from None


def _docs_payload_contains_code(value: str) -> bool:
    """Reject source/command payloads while allowing ordinary API terminology."""

    if _PUBLIC_DOCS_CODE_MARKUP.search(value) or _PUBLIC_DOCS_CODE_LINE.search(value):
        return True
    if len(_PUBLIC_DOCS_ASSIGNMENT_LINE.findall(value)) >= 2:
        return True
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            pass
        else:
            return True
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character not in "{[":
            continue
        try:
            decoded, _ = decoder.raw_decode(value[index:])
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(decoded, (dict, list)):
            return True
    return False


def _docs_payload_contains_secret(value: str) -> bool:
    try:
        return detect_secret(value.encode("utf-8", errors="strict")) is not None
    except (UnicodeError, ValueError, RecursionError):
        return True


def prepare_public_docs_execution(
    request: NormalizedRequestV1,
) -> tuple[NormalizedRequestV1, PlanV1]:
    """Build the only prompt/plan allowed to cross the Context7 boundary.

    Context7 is an external documentation service.  Repository identity,
    attachments, credentials, and pasted code are intentionally excluded
    instead of relying on an executor prompt to keep them private.
    """

    value = request.user_message
    rejected = (
        bool(request.attachments)
        or not value
        or len(value) > _PUBLIC_DOCS_QUERY_MAX_CHARS
        or "\x00" in value
        or _docs_payload_contains_secret(value)
        or _docs_payload_contains_code(value)
    )
    if rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "docs.external_payload_rejected",
                "message": (
                    "Documentation lookup accepts only a bounded public natural-language query "
                    "without attachments, credentials, or code payloads."
                ),
            },
        )

    sanitized = _PUBLIC_DOCS_PROJECT_DECLARATION.sub(" ", value)
    sanitized = _PUBLIC_DOCS_ABSOLUTE_PATH.sub(" [local path omitted] ", sanitized)
    sanitized = re.sub(r"[ \t]+", " ", sanitized)
    sanitized = re.sub(r"\s*\r?\n\s*", " ", sanitized).strip(" ;,\t\r\n")
    if (
        not sanitized
        or len(sanitized) > _PUBLIC_DOCS_QUERY_MAX_CHARS
        or _PUBLIC_DOCS_ABSOLUTE_PATH.search(sanitized)
        or _docs_payload_contains_secret(sanitized)
        or _docs_payload_contains_code(sanitized)
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "docs.external_payload_rejected",
                "message": "Documentation lookup could not produce a safe public query.",
            },
        )

    external_request = normalize_messages(
        [{"role": "user", "content": sanitized}],
        default_project=None,
        request_id=request.request_id,
        source=request.source,
    )
    external_planning = plan_request(external_request)
    external_plan = external_planning.plan
    if (
        external_request.project_hint is not None
        or external_plan is None
        or not external_planning.signals.docs
        or external_plan.tools != ["context7"]
        or external_plan.memory_context
        or external_plan.memory_record_refs
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "docs.external_payload_rejected",
                "message": "Documentation lookup could not produce a documentation-only plan.",
            },
        )
    return external_request, external_plan


def _path_is_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    metadata = metadata or path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _guarded_docs_temp_root() -> Path:
    _DOCS_EPHEMERAL_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = _DOCS_EPHEMERAL_ROOT.lstat()
    absolute = os.path.normcase(os.path.abspath(_DOCS_EPHEMERAL_ROOT))
    canonical = os.path.normcase(os.path.realpath(_DOCS_EPHEMERAL_ROOT))
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _path_is_reparse(_DOCS_EPHEMERAL_ROOT, metadata)
        or absolute != canonical
    ):
        raise RuntimeError("The documentation temp root is not a canonical local directory")
    return _DOCS_EPHEMERAL_ROOT


def _new_guarded_docs_directory(prefix: str) -> tuple[Path, tuple[int, int]]:
    root = _guarded_docs_temp_root()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _path_is_reparse(path, metadata)
            or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))
            or any(path.iterdir())
        ):
            raise RuntimeError("The documentation workspace is not fresh and canonical")
        identity = (metadata.st_dev, metadata.st_ino)
        return path, identity
    except Exception:
        if path.exists() and not _path_is_reparse(path):
            shutil.rmtree(path)
        raise


def _remove_guarded_docs_directory(
    path: Path,
    identity: tuple[int, int],
) -> None:
    metadata = path.lstat()
    if (
        (metadata.st_dev, metadata.st_ino) != identity
        or not stat.S_ISDIR(metadata.st_mode)
        or _path_is_reparse(path, metadata)
        or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))
    ):
        raise RuntimeError("Refusing to clean a replaced documentation workspace")
    for current_root, directories, files in os.walk(path, topdown=True, followlinks=False):
        current = Path(current_root)
        for name in [*directories, *files]:
            candidate = current / name
            candidate_metadata = candidate.lstat()
            if _path_is_reparse(candidate, candidate_metadata):
                raise RuntimeError("Refusing to traverse a reparse point in documentation workspace")
            try:
                os.chmod(
                    candidate,
                    (
                        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                        if stat.S_ISDIR(candidate_metadata.st_mode)
                        else stat.S_IRUSR | stat.S_IWUSR
                    ),
                )
            except OSError:
                pass
    shutil.rmtree(path)
    if path.exists():
        raise RuntimeError("Documentation workspace cleanup did not complete")


@contextmanager
def guarded_docs_directory(prefix: str):
    path, identity = _new_guarded_docs_directory(prefix)
    try:
        yield path
    finally:
        _remove_guarded_docs_directory(path, identity)


def validate_guarded_docs_cwd(path_value: str) -> Path:
    path = Path(path_value)
    metadata = path.lstat()
    root = _guarded_docs_temp_root()
    if (
        path.parent.resolve(strict=True) != root.resolve(strict=True)
        or not path.name.startswith("request-")
        or not stat.S_ISDIR(metadata.st_mode)
        or _path_is_reparse(path, metadata)
        or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))
        or any(path.iterdir())
    ):
        raise RuntimeError("Documentation execution requires a fresh platform-owned workspace")
    return path


def flatten_content(content: Any) -> str:
    return normalized_flatten_content(content)


def last_user_text(messages: list[dict[str, Any]]) -> str:
    return normalized_last_user_text(messages)


def extract_project(text: str) -> str | None:
    project, _ = resolve_project(text, DEFAULT_PROJECT)
    return project


def is_auxiliary(text: str) -> bool:
    request = normalize_messages(
        [{"role": "user", "content": text}],
        default_project=DEFAULT_PROJECT,
        request_id="classification",
    )
    return plan_request(request).signals.auxiliary


def is_code_request(text: str) -> bool:
    request = normalize_messages(
        [{"role": "user", "content": text}],
        default_project=DEFAULT_PROJECT,
        request_id="classification",
    )
    signals = plan_request(request).signals
    return signals.repository_action


def is_repository_review(text: str) -> bool:
    request = normalize_messages(
        [{"role": "user", "content": text}],
        default_project=DEFAULT_PROJECT,
        request_id="classification",
    )
    signals = plan_request(request).signals
    return signals.review and signals.repository_action


def has_requested_mutation(text: str) -> bool:
    return planned_has_requested_mutation(text)


def is_read_only(text: str) -> bool:
    return planned_is_read_only(text)


def is_review_request(text: str) -> bool:
    return planned_is_review_request(text)


def get_worktree_lock(project: str) -> asyncio.Lock:
    key = os.path.normcase(os.path.realpath(project))
    return WORKTREE_LOCKS.setdefault(key, asyncio.Lock())


def classify(text: str) -> str:
    request = normalize_messages(
        [{"role": "user", "content": text}],
        default_project=DEFAULT_PROJECT,
        request_id="classification",
    )
    planning = plan_request(request)
    return route_request(
        request,
        planning,
        capabilities=assumed_capabilities(),
        fast_model=FAST_MODEL,
        strong_model=STRONG_MODEL,
        agent_model=AGENT_MODEL,
        codex_model=CODEX_MODEL,
    ).route.value


def routing_capability_snapshot() -> CapabilitySnapshot:
    global ROUTING_CAPABILITY_CACHE
    monotonic_now = time.monotonic()
    flags = (ENABLE_LOCAL_CODE_EXEC, ENABLE_CODEX_EXEC)
    if (
        ROUTING_CAPABILITY_CACHE is not None
        and ROUTING_CAPABILITY_CACHE[1] == flags
        and monotonic_now - ROUTING_CAPABILITY_CACHE[0] < 5.0
    ):
        cached_at, cached_flags, cached_snapshot = ROUTING_CAPABILITY_CACHE
        statuses = dict(cached_snapshot.statuses)
        context7_status = managed_mcp_availability("context7")
        statuses["context7"] = (
            context7_status
            if cached_snapshot.status("qwen_code") is AvailabilityStatus.AVAILABLE
            else AvailabilityStatus.UNAVAILABLE
        )
        if statuses == cached_snapshot.statuses:
            return cached_snapshot
        refreshed_snapshot = CapabilitySnapshot(
            statuses=statuses,
            checked_at=datetime.now(timezone.utc),
        )
        # Preserve the listener-probe cache age while refreshing the cheap
        # managed runtime state.  Circuit transitions must affect the very
        # next route, not wait for the five-second TCP probe TTL.
        ROUTING_CAPABILITY_CACHE = (cached_at, cached_flags, refreshed_snapshot)
        return refreshed_snapshot
    now = datetime.now(timezone.utc)
    fast_listener = tcp_endpoint_reachable(FAST_OLLAMA_BASE_URL)
    strong_listener = tcp_endpoint_reachable(OLLAMA_BASE_URL)
    voice_listener = tcp_endpoint_reachable(f"http://127.0.0.1:{SETTINGS.voice_port}")
    qwen_available = (
        ENABLE_LOCAL_CODE_EXEC
        and strong_listener
        and bool(shutil.which("qwen.cmd") or shutil.which("qwen"))
    )
    codex_available = ENABLE_CODEX_EXEC and bool(resolve_codex_executable())
    browser_available = (
        bool(shutil.which("node"))
        and (ROOT / "services" / "browser" / "inspect.mjs").is_file()
        and (ROOT / "node_modules" / "playwright" / "package.json").is_file()
    )
    context7_status = managed_mcp_availability("context7")
    snapshot = CapabilitySnapshot(
        statuses={
            "fast_model": AvailabilityStatus.AVAILABLE if fast_listener else AvailabilityStatus.UNAVAILABLE,
            "strong_model": AvailabilityStatus.AVAILABLE if strong_listener else AvailabilityStatus.UNAVAILABLE,
            "qwen_code": AvailabilityStatus.AVAILABLE if qwen_available else AvailabilityStatus.DISABLED,
            "codex": AvailabilityStatus.AVAILABLE if codex_available else AvailabilityStatus.DISABLED,
            "context7": (
                context7_status
                if qwen_available
                else AvailabilityStatus.UNAVAILABLE
            ),
            "browser": AvailabilityStatus.AVAILABLE if browser_available else AvailabilityStatus.UNAVAILABLE,
            "voice": (
                AvailabilityStatus.AVAILABLE
                if voice_listener and (ROOT / "services" / "voice" / "app.py").is_file()
                else AvailabilityStatus.UNAVAILABLE
            ),
            "vision": AvailabilityStatus.UNAVAILABLE,
            "image": AvailabilityStatus.ON_DEMAND if comfyui_installation_ready() else AvailabilityStatus.UNAVAILABLE,
        },
        checked_at=now,
    )
    ROUTING_CAPABILITY_CACHE = (monotonic_now, flags, snapshot)
    return snapshot


def managed_mcp_availability(server_id: str) -> AvailabilityStatus:
    """Return passive MCP *routability* without starting an MCP process.

    A closed circuit with one or more failures remains probeable.  Reporting
    that state as degraded health is useful operationally, but treating it as
    unroutable would prevent the bounded retry/success path from ever closing
    the failure loop.  Only an open circuit (or a disabled/unavailable source)
    blocks routing.
    """

    state, _ = _managed_mcp_runtime_state(server_id)
    return {
        "ready": AvailabilityStatus.AVAILABLE,
        "on_demand": AvailabilityStatus.ON_DEMAND,
        "degraded": AvailabilityStatus.ON_DEMAND,
        "circuit_open": AvailabilityStatus.DEGRADED,
        "disabled": AvailabilityStatus.DISABLED,
        "unavailable": AvailabilityStatus.UNAVAILABLE,
    }.get(state, AvailabilityStatus.UNAVAILABLE)


def _managed_mcp_runtime_state(server_id: str) -> tuple[str, str]:
    """Return a bounded runtime state and non-sensitive diagnostic reason."""

    try:
        server = load_registry().server(server_id)
        if not server.enabled or server.configured_state == "disabled":
            return "disabled", "configured_disabled"
        validate_installed_source(server)
        runtime = peek_status(server)
    except (OSError, ValueError, KeyError, ValidationError):
        return "unavailable", "registry_or_source_unavailable"
    state = runtime.get("state")
    reason = runtime.get("last_reason_code")
    if not isinstance(state, str):
        return "unavailable", "invalid_runtime_state"
    if not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_.:-]{1,128}", reason) is None:
        reason = "invalid_or_redacted_reason"
    return state, reason


def managed_mcp_health_observation(server_id: str) -> tuple[AvailabilityStatus, str]:
    """Return diagnostic health separately from routing eligibility."""

    state, reason = _managed_mcp_runtime_state(server_id)
    status = {
        "ready": AvailabilityStatus.AVAILABLE,
        "on_demand": AvailabilityStatus.ON_DEMAND,
        "degraded": AvailabilityStatus.DEGRADED,
        "circuit_open": AvailabilityStatus.DEGRADED,
        "disabled": AvailabilityStatus.DISABLED,
        "unavailable": AvailabilityStatus.UNAVAILABLE,
    }.get(state, AvailabilityStatus.UNAVAILABLE)
    detail = (
        "Passive managed-registry state only; live discovery is owned by doctor/smoke. "
        f"runtime_state={state}; last_reason_code={reason}."
    )
    return status, detail


def tcp_endpoint_reachable(url: str, *, timeout: float = 0.08) -> bool:
    """Cheap cached readiness signal; semantic health remains owned by /health/ready."""

    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def build_route_decision(
    request: NormalizedRequestV1,
    *,
    capabilities: CapabilitySnapshot | None = None,
    permissions: PermissionSnapshot | None = None,
    failures: FailureHistory | None = None,
) -> RouteDecisionV1:
    planning = plan_request(request)
    return route_request(
        request,
        planning,
        capabilities=capabilities or routing_capability_snapshot(),
        permissions=permissions,
        failures=failures,
        fast_model=FAST_MODEL,
        strong_model=STRONG_MODEL,
        agent_model=AGENT_MODEL,
        codex_model=CODEX_MODEL,
    )


def build_coding_task_request(
    *,
    task_id: str,
    prompt: str,
    project: str,
    decision: RouteDecisionV1,
    plan: PlanV1,
) -> CodingTaskRequestV1:
    """Map the gateway plan into the fail-closed versioned coding contract."""

    if decision.execution_mode not in {ExecutionMode.READ_ONLY, ExecutionMode.WRITE}:
        raise CodingEngineError("local_code requires an explicit read-only or write execution mode")
    mode = CodingMode(decision.execution_mode.value)
    source = resolve_repository(project)
    try:
        declared_scope = resolve_declared_coding_scope(
            prompt,
            requested_project=Path(project),
            canonical_root=source.canonical_root,
            write=(mode is CodingMode.WRITE),
        )
    except CodingScopeError as exc:
        raise CodingRepositoryError(
            "declared coding path scope is invalid or exceeds the policy limit"
        ) from exc
    return CodingTaskRequestV1(
        task_id=task_id,
        request_id=decision.request_id,
        goal=prompt,
        repository_path=str(source.canonical_root),
        mode=mode,
        risk=CodingRisk(decision.risk.value),
        constraints=list(plan.constraints),
        acceptance_criteria=list(plan.acceptance_criteria),
        verification_plan=list(plan.verification_plan),
        verification_commands=(
            discover_verification_commands(source.canonical_root)
            if mode is CodingMode.WRITE
            else []
        ),
        permissions=CodingPermissionsV1(
            modify_files=(mode is CodingMode.WRITE),
            local_commit=False,
            cloud_execution=False,
            data_classification=DataClassification.INTERNAL,
        ),
        route_reasons=list(decision.reason_codes),
        rule_scope_paths=list(declared_scope.rule_scope_paths),
        expected_diff_paths=list(declared_scope.expected_diff_paths),
        forbidden_diff_paths=list(declared_scope.forbidden_diff_paths),
    )


def coding_engine_factory() -> CodingEngine:
    """Lazily construct one thread-safe gateway engine with the routed model settings."""

    global CODING_ENGINE_INSTANCE
    if CODING_ENGINE_INSTANCE is not None:
        return CODING_ENGINE_INSTANCE
    with CODING_ENGINE_FACTORY_LOCK:
        if CODING_ENGINE_INSTANCE is None:
            CODING_ENGINE_INSTANCE = CodingEngine(
                qwen_executor=QwenExecutor(model=AGENT_MODEL),
                codex_executor=CodexExecutor(
                    model=CODEX_MODEL,
                    reasoning_effort=CODEX_REASONING_EFFORT,
                ),
            )
    return CODING_ENGINE_INSTANCE


def run_process(
    command: list[str],
    cwd: str,
    timeout: float = 1800,
    prefer_stdout: bool = False,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    explicit_environment = {
        "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        "QWEN_HOME": str(ROOT / "config" / "qwen"),
        "NO_COLOR": "1",
    }
    explicit_environment.update(env_overrides or {})
    process_env = safe_child_environment(explicit_environment)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_env,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        start_new_session=(os.name != "nt"),
    )
    tree_guard = ProcessTreeGuard(process)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tree_guard.terminate(include_parent=True)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
    # A successful CLI parent may still have detached stdio MCP descendants.
    # They remain task-owned and must not outlive this bounded invocation.
    tree_guard.terminate(include_parent=False)
    combined_output = (stdout + "\n" + stderr).strip()
    if process.returncode != 0:
        raise RuntimeError(f"Command failed ({process.returncode}): {combined_output[-6000:]}")
    output = stdout.strip() if prefer_stdout and stdout.strip() else combined_output
    return output[-20000:]
def execution_contract(prompt: str, read_only: bool, mode: str = "code") -> str:
    action = (
        "Use Context7 MCP tools now and base the answer on the retrieved current documentation."
        if mode == "docs"
        else "Use filesystem and terminal tools now; inspect the real repository and complete the task."
    )
    permissions = "Do not change any file." if read_only else "You may edit project files; verify the result, but do not commit or push."
    quality = ""
    lowered = prompt.lower()
    if "security" in lowered or "безопасност" in lowered or "уязвим" in lowered:
        quality = (
            " Perform an actual security code review, not a repository overview. Trace trust boundaries and attacker-controlled "
            "inputs through the implementation. Return findings first. Every finding must include severity, exact file and line, "
            "concrete exploit or failure scenario, and a concise remediation. Do not spend the answer on git status, architecture "
            "summary, or generic best practices. If no vulnerability is found, state that explicitly and list the security-sensitive "
            "surfaces inspected with concrete evidence."
        )
    return (
        f"EXECUTION REQUIRED. Do not greet, acknowledge, summarize AGENTS.md, or ask what to do. {action} "
        f"Do not stop after reading repository instructions. {permissions} Reply in the user's language. "
        f"Concrete task: {prompt}{quality}"
    )


def suspicious_agent_result(result: str) -> bool:
    lowered = result.lower().strip()
    evasive = (
        "чем могу помочь", "что нужно сделать", "какая задача", "уточните, пожалуйста",
        "what would you like", "what should i do", "please specify the task", "you did not specify",
        "i'm ready", "i am ready", "ready — what", "ready - what",
    )
    return not lowered or any(marker in lowered for marker in evasive)


def single_line_prompt(prompt: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ", prompt).strip()


def normalize_task_for_agent(prompt: str) -> tuple[str, str]:
    if not re.search(r"[А-Яа-яЁё]", prompt):
        return prompt, "Reply in the user's language."
    payload = {
        "model": FAST_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the user's software task into precise English for an autonomous coding agent. "
                    "Preserve every path, filename, command, literal, constraint, and requested action exactly. "
                    "Return only the English translation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "reasoning_effort": "none",
        "think": False,
        "max_tokens": 2048,
    }
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(f"{FAST_OLLAMA_BASE_URL}/v1/chat/completions", json=payload)
            response.raise_for_status()
            translated = strip_hidden_thinking(response.json()["choices"][0]["message"].get("content", ""))
        if translated and not suspicious_agent_result(translated):
            return translated, "Return the final answer in Russian."
    except Exception:
        pass
    return prompt, "Return the final answer in Russian."


def normalize_image_prompt(prompt: str) -> str:
    payload = {
        "model": FAST_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Convert the request into one concise English SDXL image prompt. Preserve every requested subject, color, "
                    "composition, style, and background. Remove conversational instructions such as 'generate an image'. "
                    "Return only the visual prompt, with no quotes or explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "reasoning_effort": "none",
        "think": False,
        "max_tokens": 512,
    }
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(f"{FAST_OLLAMA_BASE_URL}/v1/chat/completions", json=payload)
            response.raise_for_status()
            normalized = strip_hidden_thinking(response.json()["choices"][0]["message"].get("content", ""))
        return single_line_prompt(normalized) if normalized else prompt
    except Exception:
        return prompt


def prepare_qwen_runtime_home(mode: str) -> Path:
    """Generate a validated executor view from the canonical MCP registry."""

    profile = "qwen-docs" if mode == "docs" else "qwen-code"
    target = generate_qwen_views()[profile]
    return target.parent


def run_codex_agent(
    prompt: str,
    project: str,
    cloud: bool,
    mode: str = "code",
    read_only: bool | None = None,
) -> str:
    codex_executable = resolve_codex_executable()
    if codex_executable is None:
        raise RuntimeError("Native Codex executable is unavailable")
    read_only = (mode == "docs" or is_read_only(prompt)) if read_only is None else read_only
    sandbox = "read-only" if read_only else CODEX_SANDBOX
    if cloud and mode == "review":
        review_prompt = execution_contract(prompt, True, mode)
        command = [
            codex_executable, "-C", project, "-m", CODEX_MODEL,
            "-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "-a", "never", "-s", "read-only", "review", "-",
        ]
        return run_process(
            command,
            project,
            timeout=3600,
            prefer_stdout=True,
            input_text=review_prompt,
        ).strip()
    if cloud:
        prepared_prompt = execution_contract(prompt, read_only, mode)
    else:
        normalized_prompt, language_instruction = normalize_task_for_agent(prompt)
        action = "Use Context7 MCP tools now." if mode == "docs" else "Use filesystem and terminal tools now."
        permissions = "Do not modify files." if read_only else "You may edit project files and verify the result. Do not commit or push."
        prepared_prompt = (
            f"EXECUTION REQUIRED. Do not greet or ask what to do. {action} {normalized_prompt} "
            f"{permissions} {language_instruction}"
        )
    task_id = uuid.uuid4().hex[:12]
    output_file = Path(tempfile.gettempdir()) / f"local-agent-{task_id}.txt"
    output_file.unlink(missing_ok=True)
    command = [codex_executable, "exec"]
    if cloud:
        command.extend(["-m", CODEX_MODEL, "-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"'])
    if not cloud:
        command.extend(["--oss", "--local-provider", "ollama", "-m", AGENT_MODEL])
    command.extend(
        [
            "--ephemeral", "-C", project, "-s", sandbox,
            "--output-last-message", str(output_file), "-",
        ]
    )
    try:
        raw_output = run_process(command, project, timeout=3600, input_text=prepared_prompt)
        result = output_file.read_text(encoding="utf-8").strip() if output_file.exists() else raw_output.strip()
        if suspicious_agent_result(result):
            raise RuntimeError("Codex returned an incomplete result; the bounded attempt was not retried.")
        return result
    finally:
        output_file.unlink(missing_ok=True)


def run_qwen_agent(
    prompt: str,
    project: str,
    mode: str = "code",
    model: str = AGENT_MODEL,
    read_only: bool | None = None,
    plan: PlanV1 | None = None,
    correlation_id: str | None = None,
) -> str:
    read_only = (mode == "docs" or is_read_only(prompt)) if read_only is None else read_only
    if plan is not None:
        normalized_prompt = render_plan_execution_context(plan, prompt)
        language_instruction = (
            "Return the final answer in Russian."
            if re.search(r"[А-Яа-яЁё]", plan.goal)
            else "Reply in the user's language."
        )
    else:
        normalized_prompt, language_instruction = normalize_task_for_agent(prompt)
    if mode == "docs":
        action = "Use Context7 MCP tools now. Do not answer until the requested documentation has been retrieved."
    else:
        action = (
            "Use only built-in filesystem and terminal tools necessary for this exact task. "
            "Find and follow applicable AGENTS.md files yourself. Do not run unrelated diagnostics."
        )
    permissions = (
        "This is read-only: inspect files but do not execute project commands or scripts and do not modify files. "
        "If the task asks to list, show, explain, or inspect commands, never run those commands. Return only the requested result."
        if read_only
        else "You may edit project files and verify the result. Do not commit or push."
    )
    precision = (
        " If exact literal file content is requested, preserve it byte-for-byte, do not append a newline, "
        "inspect the raw bytes after writing, and repair any extra bytes before finishing."
    )
    def compose_agent_prompt(execution_context: str) -> str:
        return (
            f"EXECUTION REQUIRED. Do not greet or ask what to do. {action} {execution_context} "
            f"{permissions}{precision} {language_instruction}"
        )

    agent_prompt = compose_agent_prompt(normalized_prompt)
    if (
        plan is not None
        and plan.memory_context
        and conservative_token_upper_bound(agent_prompt) > plan.context_budget.max_input_tokens
    ):
        # Retrieved memory is optional evidence.  Drop it before rejecting an
        # otherwise valid executable Plan, including on a retry with notes.
        without_memory = plan.model_copy(update={"memory_context": []})
        normalized_prompt = render_plan_execution_context(without_memory, prompt)
        agent_prompt = compose_agent_prompt(normalized_prompt)
    if plan is not None and conservative_token_upper_bound(agent_prompt) > plan.context_budget.max_input_tokens:
        raise AgentContextBudgetExceeded(
            "Executable plan exceeds context_budget.max_input_tokens; Qwen Code was not launched."
        )
    command = [
        "qwen.cmd", "--approval-mode", "plan" if read_only else "yolo", "--model", model,
        "--output-format", "text", "--prompt", "",
    ]
    if mode == "docs":
        correlation_id = _validated_mcp_correlation_id(correlation_id)
        validate_guarded_docs_cwd(project)
        canonical_home = prepare_qwen_runtime_home("docs")
        canonical_settings = canonical_home / "settings.json"
        canonical_bytes = canonical_settings.read_bytes()
        canonical_payload = json.loads(canonical_bytes)
        if set(canonical_payload.get("mcpServers", {})) != {"context7"}:
            raise RuntimeError("Generated documentation MCP view is not Context7-only")
        with (
            guarded_docs_directory("home-") as isolated_home,
            guarded_docs_directory("runtime-") as isolated_runtime,
        ):
            isolated_settings = isolated_home / "settings.json"
            with isolated_settings.open("xb") as stream:
                stream.write(canonical_bytes)
            if isolated_settings.read_bytes() != canonical_bytes:
                raise RuntimeError("Isolated documentation MCP settings changed during creation")
            os.chmod(isolated_settings, stat.S_IRUSR)
            command.extend(
                [
                    "--mcp-config",
                    str(isolated_settings.resolve(strict=True)),
                    "--allowed-mcp-server-names",
                    "context7",
                    "--core-tools",
                    _DOCS_CORE_TOOL_SENTINEL,
                    "--exclude-tools",
                    ",".join(_DOCS_EXCLUDED_TOOLS),
                ]
            )
            result = run_process(
                command,
                project,
                timeout=1800,
                input_text=agent_prompt,
                env_overrides={
                    "QWEN_HOME": str(isolated_home),
                    "QWEN_RUNTIME_DIR": str(isolated_runtime),
                    **(
                        {_MCP_CORRELATION_ENV: correlation_id}
                        if correlation_id is not None
                        else {}
                    ),
                },
            ).strip()
            return result

    command.extend(["--bare", "--auth-type", "openai"])
    qwen_home = prepare_qwen_runtime_home(mode)
    result = run_process(
        command,
        project,
        timeout=1800,
        input_text=agent_prompt,
        env_overrides={
            "QWEN_HOME": str(qwen_home),
            "OPENAI_API_KEY": "ollama",
            "OPENAI_BASE_URL": f"{OLLAMA_BASE_URL.rstrip('/')}/v1",
            "OPENAI_MODEL": model,
        },
    ).strip()
    return result


def create_codex_bundle(
    task_id: str,
    prompt: str,
    project: str | None,
    *,
    plan: PlanV1 | None = None,
    decision: RouteDecisionV1 | None = None,
    errors: list[str] | None = None,
    modified_files: list[str] | None = None,
    command_summaries: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> Path:
    if plan is None or decision is None:
        normalized = normalize_messages(
            [{"role": "user", "content": prompt}],
            default_project=project or DEFAULT_PROJECT,
            request_id=task_id,
        )
        planning = plan_request(normalized)
        plan = planning.plan or PlanV1(
            request_id=task_id,
            goal=prompt,
            subtasks=["Complete the bounded task."],
            tools=[],
            acceptance_criteria=["Return a concrete verified result."],
            risk="medium",
            approvals=["Cloud execution requires scoped approval."],
            verification_plan=["Verify the result against the task."],
            context_budget={
                "max_input_tokens": 22_000,
                "reserved_output_tokens": 8_000,
                "max_attachment_bytes": 25_000_000,
            },
        )
        decision = decision or route_request(
            normalized,
            planning,
            capabilities=routing_capability_snapshot(),
            fast_model=FAST_MODEL,
            strong_model=STRONG_MODEL,
            agent_model=AGENT_MODEL,
            codex_model=CODEX_MODEL,
        )
    return ensure_codex_handoff(
        inbox_dir=INBOX_DIR,
        task_id=task_id,
        plan=plan,
        decision=decision,
        project=project,
        worktree=project,
        errors=errors or [],
        modified_files=modified_files or [],
        command_summaries=command_summaries or [],
        artifact_refs=artifact_refs or [],
    )


def system_message(route: str) -> dict[str, str]:
    if route == "auxiliary":
        content = "Follow the formatting instructions exactly. Return only the requested machine-readable result. Do not use tools."
    elif route == "fast_chat":
        content = (
            "You are Local Agent, a private AI platform running on this Windows PC. The fast tier is Qwen3.5 4B, "
            "the strong local tier and Qwen Code use Qwen3.6 35B through Ollama, and super-complex programming tasks use Codex. "
            "Answer directly and concisely in the user's language. Never invent cloud infrastructure or capabilities, "
            "do not mention hidden routing unless asked, and do not add greetings or offers to help after the answer."
        )
    else:
        content = "Solve carefully and directly in the user's language. State concrete conclusions, not generic filler."
    return {"role": "system", "content": content}


def strip_hidden_thinking(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1].strip()
    return text.strip()


def strip_ambient_tool_catalog(payload: dict[str, Any], route: str) -> int:
    tools = payload.get("tools") or []
    automatic_choice = payload.get("tool_choice") is None or payload.get("tool_choice") == "auto"
    if (
        route in {"auxiliary", "fast_chat", "strong_chat"}
        and len(tools) > MAX_AUTOMATIC_CHAT_TOOLS
        and automatic_choice
    ):
        removed = len(tools)
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            payload.pop(field, None)
        return removed
    return 0


def bounded_chat_messages(messages: list[dict[str, Any]], *, max_input_tokens: int) -> list[dict[str, Any]]:
    """Keep recent context bounded, preserving assistant/tool exchanges atomically."""

    budget = max_input_tokens * 4
    remaining = max(0, budget - 64)  # list punctuation plus one provenance marker
    marker = "[context truncated: earlier or oversized content omitted]\n"
    omitted = False

    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            declared_ids = [
                str(item.get("id"))
                for item in message.get("tool_calls", [])
                if isinstance(item, dict) and item.get("id")
            ]
            call_ids = set(declared_ids)
            group = [message]
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                group.append(messages[cursor])
                cursor += 1
            result_ids = [str(item.get("tool_call_id") or "") for item in group[1:]]
            if (
                declared_ids
                and len(declared_ids) == len(call_ids)
                and len(result_ids) == len(set(result_ids))
                and set(result_ids) == call_ids
            ):
                groups.append(group)
            else:
                omitted = True
            index = cursor
            continue
        if role == "assistant" and message.get("function_call"):
            group = [message]
            cursor = index + 1
            expected_name = str(message["function_call"].get("name") or "") if isinstance(message["function_call"], dict) else ""
            if cursor < len(messages) and messages[cursor].get("role") == "function":
                function_result = messages[cursor]
                cursor += 1
                if expected_name and str(function_result.get("name") or "") == expected_name:
                    group.append(function_result)
                    groups.append(group)
                else:
                    omitted = True
            else:
                omitted = True
            index = cursor
            continue
        if role in {"tool", "function"}:
            omitted = True  # never emit an orphan tool result
            index += 1
            continue
        groups.append([message])
        index += 1

    def copy_message(message: dict[str, Any], allowance: int, *, structured: bool) -> dict[str, Any] | None:
        nonlocal omitted
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool", "function"}:
            omitted = True
            return None
        copied: dict[str, Any] = {"role": role}
        for key in ("name", "tool_call_id"):
            if message.get(key) is not None:
                copied[key] = str(message[key])[:256]
        if structured:
            for key in ("tool_calls", "function_call"):
                if key not in message:
                    continue
                encoded = json.dumps(message[key], ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > 8_000:
                    omitted = True
                    return None
                copied[key] = message[key]
        text = flatten_content(message.get("content", ""))
        base = len(json.dumps({**copied, "content": ""}, ensure_ascii=False, separators=(",", ":")))
        content_limit = min(20_000, max(0, allowance - base - 2))
        if len(text) > content_limit:
            omitted = True
            if content_limit <= len(marker):
                bounded = text[:content_limit]
            else:
                payload_limit = content_limit - len(marker)
                head = int(payload_limit * 0.6)
                tail_length = payload_limit - head
                bounded = text[:head] + marker + (text[-tail_length:] if tail_length else "")
        else:
            bounded = text
        copied["content"] = bounded
        while len(json.dumps(copied, ensure_ascii=False, separators=(",", ":"))) > allowance and copied["content"]:
            overshoot = len(json.dumps(copied, ensure_ascii=False, separators=(",", ":"))) - allowance
            copied["content"] = copied["content"][: max(0, len(copied["content"]) - overshoot - 1)]
            omitted = True
        if len(json.dumps(copied, ensure_ascii=False, separators=(",", ":"))) > allowance:
            omitted = True
            return None
        if not copied["content"] and not copied.get("tool_calls") and not copied.get("function_call"):
            return None
        return copied

    selected_groups: list[list[dict[str, Any]]] = []
    selected_messages = 0
    for group in reversed(groups):
        if selected_messages + len(group) > 32 or remaining <= 0:
            omitted = True
            continue
        is_tool_exchange = len(group) > 1 or bool(group[0].get("tool_calls") or group[0].get("function_call"))
        copied_group: list[dict[str, Any]] = []
        group_remaining = remaining
        for message in group:
            copied = copy_message(message, group_remaining, structured=(message is group[0]))
            if copied is None:
                copied_group = []
                break
            cost = len(json.dumps(copied, ensure_ascii=False, separators=(",", ":"))) + 1
            copied_group.append(copied)
            group_remaining -= cost
        if not copied_group or (is_tool_exchange and len(copied_group) != len(group)):
            omitted = True
            continue
        cost = remaining - group_remaining
        selected_groups.append(copied_group)
        remaining -= cost
        selected_messages += len(copied_group)

    selected = [message for group in reversed(selected_groups) for message in group]
    if omitted and selected:
        first = selected[0]
        content = str(first.get("content") or "")
        first["content"] = marker + content
        while len(json.dumps(selected, ensure_ascii=False, separators=(",", ":"))) > budget and first["content"]:
            first["content"] = first["content"][:-1]
    return selected


async def preopen_local_stream(
    *,
    url: str,
    payload: dict[str, Any],
    model_lock: asyncio.Lock,
    headers: dict[str, str],
    route: str,
    model: str,
    started_at: float,
) -> StreamingResponse:
    """Open and validate the upstream before ASGI sends HTTP 200/SSE headers."""

    await model_lock.acquire()
    client: httpx.AsyncClient | None = None
    response: httpx.Response | None = None

    async def cleanup() -> None:
        try:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException:
                    logger.exception("failed to close upstream local-model response")
            if client is not None:
                try:
                    await client.aclose()
                except BaseException:
                    logger.exception("failed to close upstream local-model client")
        finally:
            if model_lock.locked():
                model_lock.release()

    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        upstream_request = client.build_request("POST", url, json=payload)
        response = await client.send(upstream_request, stream=True)
        response.raise_for_status()
        chunks = response.aiter_bytes()
        first_chunk = await anext(chunks)
        if not first_chunk:
            raise RuntimeError("local model stream produced an empty first event")
    except BaseException:
        await cleanup()
        raise

    async def generate():
        try:
            yield first_chunk
            async for chunk in chunks:
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = {
                "error": {
                    "message": "Upstream local model stream failed after it started.",
                    "type": "local_agent_stream_error",
                    "code": "executor.local_stream_failure",
                },
                "local_agent_stream_status": "failed",
            }
            yield f"data: {json.dumps(failure, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            await cleanup()
            logger.info(
                "local stream complete route=%s model=%s elapsed=%.2fs",
                route,
                model,
                time.perf_counter() - started_at,
            )

    return StreamingResponse(generate(), media_type="text/event-stream", headers=headers)


async def local_chat(
    request: ChatRequest, model: str, route: str, thinking: bool, request_id: str | None = None
) -> JSONResponse | StreamingResponse:
    payload = request.model_dump(exclude_none=True)
    payload["model"] = model
    policy = get_routing_policy()
    input_budget = (
        policy.thresholds.strong_input_tokens
        if thinking
        else policy.thresholds.fast_input_tokens
    )
    payload["messages"] = [
        system_message(route),
        *bounded_chat_messages(request.messages, max_input_tokens=input_budget),
    ]
    payload["reasoning_effort"] = "medium" if thinking else "none"
    removed_tools = strip_ambient_tool_catalog(payload, route)
    if removed_tools:
        logger.info("removed ambient Open WebUI tool catalog route=%s tools=%d", route, removed_tools)
    headers = {"X-Local-Agent-Route": route, "X-Local-Agent-Model": model}
    if request_id:
        headers["X-Local-Agent-Request-ID"] = request_id
    model_base_url = FAST_OLLAMA_BASE_URL if model == FAST_MODEL else OLLAMA_BASE_URL
    model_lock = FAST_MODEL_LOCK if model == FAST_MODEL else GPU_LOCK
    if not thinking:
        payload["think"] = False
        payload.setdefault("max_tokens", 256)
        started_at = time.perf_counter()
        logger.info(
            "local request route=%s model=%s messages=%d chars=%d tools=%d max_tokens=%s keys=%s",
            route,
            model,
            len(payload.get("messages", [])),
            sum(len(flatten_content(item.get("content", ""))) for item in payload.get("messages", [])),
            len(payload.get("tools") or []),
            payload.get("max_tokens"),
            ",".join(sorted(payload)),
        )
        if request.stream:
            payload["stream"] = True
            return await preopen_local_stream(
                url=f"{model_base_url}/v1/chat/completions",
                payload=payload,
                model_lock=model_lock,
                headers=headers,
                route=route,
                model=model,
                started_at=started_at,
            )
        payload["stream"] = False
        async with model_lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                response = await client.post(f"{model_base_url}/v1/chat/completions", json=payload)
                response.raise_for_status()
                data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        logger.info(
            "local response route=%s model=%s elapsed=%.2fs usage=%s content_chars=%d reasoning_chars=%d",
            route,
            model,
            time.perf_counter() - started_at,
            data.get("usage"),
            len(message.get("content") or ""),
            len(message.get("reasoning") or ""),
        )
        if message.get("content") is not None:
            message["content"] = strip_hidden_thinking(message["content"])
        message.pop("reasoning", None)
        if request.stream:
            streamed = openai_response(
                message.get("content"),
                route,
                stream=True,
                message=message,
                finish_reason=choice.get("finish_reason"),
                request_id=request_id,
            )
            streamed.headers.update(headers)
            return streamed
        data["model"] = "local-agent-auto"
        data["local_agent_route"] = route
        data["local_agent_model"] = model
        if request_id:
            data["local_agent_request_id"] = request_id
        return JSONResponse(data, headers=headers)
    if request.stream:
        payload["stream"] = True
        return await preopen_local_stream(
            url=f"{model_base_url}/v1/chat/completions",
            payload=payload,
            model_lock=model_lock,
            headers=headers,
            route=route,
            model=model,
            started_at=time.perf_counter(),
        )
    async with model_lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            response = await client.post(f"{model_base_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        data["model"] = "local-agent-auto"
    data["local_agent_route"] = route
    data["local_agent_model"] = model
    if request_id:
        data["local_agent_request_id"] = request_id
    return JSONResponse(data, headers=headers)


def _memory_response_diagnostics(
    plan: PlanV1 | None,
) -> dict[str, Any] | None:
    """Expose retrieval provenance without repeating durable memory content."""

    if not plan or not plan.memory_record_refs:
        return None
    return {
        "record_refs": plan.memory_record_refs,
        "items": [
            {
                "record_id": item.record_id,
                "record_type": item.record_type,
                "score": item.score,
                "why": item.why,
                "source_refs": item.source_refs,
                "disclosure": item.disclosure,
            }
            for item in plan.memory_context
        ],
        "content_in_response": False,
    }


def openai_response(
    text: str | None,
    route: str,
    stream: bool = False,
    message: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    request_id: str | None = None,
    plan: PlanV1 | None = None,
) -> JSONResponse | StreamingResponse:
    now = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    headers = {"X-Local-Agent-Route": route}
    if request_id:
        headers["X-Local-Agent-Request-ID"] = request_id
    memory_diagnostics = _memory_response_diagnostics(plan)
    if memory_diagnostics:
        headers["X-Local-Agent-Memory-Count"] = str(len(plan.memory_record_refs))
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
    if message:
        assistant_message = {
            key: value for key, value in message.items()
            if key in {"role", "content", "tool_calls", "function_call"}
        }
        assistant_message.setdefault("role", "assistant")
        assistant_message.setdefault("content", text)
    finish_reason = finish_reason or ("tool_calls" if assistant_message.get("tool_calls") else "stop")
    if stream:
        async def generate():
            delta: dict[str, Any] = {"role": "assistant"}
            if assistant_message.get("content") is not None:
                delta["content"] = assistant_message["content"]
            if assistant_message.get("tool_calls"):
                delta["tool_calls"] = [
                    {**tool_call, "index": tool_call.get("index", index)}
                    for index, tool_call in enumerate(assistant_message["tool_calls"])
                ]
            if assistant_message.get("function_call"):
                delta["function_call"] = assistant_message["function_call"]
            first = {
                "id": completion_id, "object": "chat.completion.chunk", "created": now,
                "model": "local-agent-auto",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            final = {
                "id": completion_id, "object": "chat.completion.chunk", "created": now,
                "model": "local-agent-auto",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            if memory_diagnostics:
                final["local_agent_memory"] = memory_diagnostics
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream", headers=headers)
    body = {
            "id": completion_id, "object": "chat.completion", "created": now, "model": "local-agent-auto",
            "choices": [{"index": 0, "message": assistant_message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "local_agent_route": route,
        }
    if request_id:
        body["local_agent_request_id"] = request_id
    if memory_diagnostics:
        body["local_agent_memory"] = memory_diagnostics
    return JSONResponse(body, headers=headers)


def openai_error_response(
    *,
    message: str,
    code: str,
    route: str,
    request_id: str,
    status_code: int,
    decision: RouteDecisionV1 | None = None,
    plan: PlanV1 | None = None,
) -> JSONResponse:
    """Return an error before SSE headers so unavailable work is never false success."""

    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "local_agent_error",
            "code": code,
        },
        "model": "local-agent-auto",
        "local_agent_route": route,
        "local_agent_request_id": request_id,
    }
    if decision is not None:
        body["local_agent_decision"] = {
            "status": decision.decision_status.value,
            "risk": decision.risk.value,
            "reason_codes": decision.reason_codes,
            "blocking_reason_codes": decision.blocking_reason_codes,
            "capability": decision.capability,
            "capability_status": decision.capability_status.value,
        }
    memory_diagnostics = _memory_response_diagnostics(plan)
    headers = {
        "X-Local-Agent-Route": route,
        "X-Local-Agent-Request-ID": request_id,
    }
    if memory_diagnostics:
        body["local_agent_memory"] = memory_diagnostics
        headers["X-Local-Agent-Memory-Count"] = str(len(plan.memory_record_refs))
    return JSONResponse(
        body,
        status_code=status_code,
        headers=headers,
    )


def track_stream_task(
    response: StreamingResponse,
    *,
    task_id: str,
    route: str,
    prompt: str,
    model: str,
) -> StreamingResponse:
    """Complete the journal only after a real terminal OpenAI SSE event."""

    original_iterator = response.body_iterator

    terminal_pattern = re.compile(
        rb"(?:^|\r?\n)data:[ \t]*\[DONE\][ \t]*\r?\n\r?\n"
    )

    def record_terminal(status: str, result: str) -> None:
        try:
            save_task(task_id, route, status, prompt, result=result)
        except Exception:
            logger.exception("Failed to persist terminal stream state for %s", task_id)

    async def tracked_iterator():
        tail = b""
        event_buffer = b""
        saw_terminal_marker = False
        saw_failure_marker = False
        recorded = False
        try:
            async for chunk in original_iterator:
                encoded = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                combined = tail + encoded
                if terminal_pattern.search(combined):
                    saw_terminal_marker = True
                if b'"local_agent_stream_status":"failed"' in combined:
                    saw_failure_marker = True
                event_buffer = (event_buffer + encoded).replace(b"\r\n", b"\n")
                while b"\n\n" in event_buffer:
                    event, event_buffer = event_buffer.split(b"\n\n", 1)
                    data_lines = [line[5:].strip() for line in event.split(b"\n") if line.startswith(b"data:")]
                    data = b"\n".join(data_lines)
                    if data and data != b"[DONE]":
                        try:
                            decoded = json.loads(data)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            decoded = None
                        if isinstance(decoded, dict) and "error" in decoded:
                            saw_failure_marker = True
                event_buffer = event_buffer[-65_536:]
                tail = combined[-64:]
                yield chunk
        except asyncio.CancelledError:
            record_terminal("cancelled", "stream cancelled by client")
            recorded = True
            raise
        except GeneratorExit:
            record_terminal("failed", "stream consumer closed before a terminal event")
            recorded = True
            raise
        except Exception as exc:
            record_terminal("failed", f"{type(exc).__name__}: {str(exc)[-2000:]}")
            recorded = True
            raise
        else:
            if saw_failure_marker:
                record_terminal("failed", "upstream stream failed after response headers")
            elif saw_terminal_marker:
                record_terminal("complete", f"streamed from {model}")
            else:
                record_terminal("failed", "upstream stream ended before data: [DONE]")
            recorded = True
        finally:
            if not recorded:
                record_terminal("failed", "stream ended without a terminal lifecycle state")

    response.body_iterator = tracked_iterator()
    return response


async def run_blocking_safely(function, *args):
    """Do not release a worktree lock while a cancelled worker thread still runs."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise


async def execute_agent(
    task_id: str,
    route: str,
    prompt: str,
    project: str,
    cloud: bool,
    mode: str = "code",
    model: str | None = None,
    *,
    decision: RouteDecisionV1 | None = None,
    plan: PlanV1 | None = None,
) -> str:
    executor = ExecutorName.CODEX_CLI if cloud else ExecutorName.QWEN_CODE
    read_only = bool(decision and decision.execution_mode is ExecutionMode.READ_ONLY)
    save_task(
        task_id,
        route,
        "running",
        prompt,
        project,
        route_decision=decision,
        plan=plan,
        actual_executor=executor,
        actual_model=(decision.model if decision else model),
        actual_profile=(decision.profile if decision else None),
        command_summaries=[f"{executor.value} bounded invocation"],
    )
    try:
        execution_lock = DOCS_EXECUTION_LOCK if mode == "docs" else get_worktree_lock(project)
        async with execution_lock:
            if cloud:
                async with CODEX_LOCK:
                    result = await run_blocking_safely(run_codex_agent, prompt, project, True, mode, read_only)
            else:
                async with AGENT_LOCK:
                    async with GPU_LOCK:
                        result = await run_blocking_safely(
                            run_qwen_agent,
                            prompt,
                            project,
                            mode,
                            model or AGENT_MODEL,
                            read_only,
                            plan,
                            task_id,
                        )
        if suspicious_agent_result(result):
            raise RuntimeError(f"{executor.value} returned an incomplete result")
    except asyncio.CancelledError:
        save_task(
            task_id,
            route,
            "cancelled",
            prompt,
            project,
            route_decision=decision,
            plan=plan,
            actual_executor=executor,
            actual_model=(decision.model if decision else model),
            actual_profile=(decision.profile if decision else None),
            reason_codes=["executor.cancelled"],
        )
        raise
    except Exception as exc:
        error = redact_bounded(f"{type(exc).__name__}: {exc}")
        save_task(
            task_id,
            route,
            "failed",
            prompt,
            project,
            error,
            route_decision=decision,
            plan=plan,
            actual_executor=executor,
            actual_model=(decision.model if decision else model),
            actual_profile=(decision.profile if decision else None),
            error_summary=error,
            reason_codes=[f"executor.{executor.value}_failure"],
            modified_files=collect_modified_files(project),
        )
        raise
    save_task(
        task_id,
        route,
        "complete",
        prompt,
        project,
        result,
        route_decision=decision,
        plan=plan,
        actual_executor=executor,
        actual_model=(decision.model if decision else model),
        actual_profile=(decision.profile if decision else None),
        modified_files=collect_modified_files(project),
    )
    return result


@asynccontextmanager
async def queued_worktree_lock(
    *,
    task_id: str,
    prompt: str,
    project: str,
    decision: RouteDecisionV1,
    plan: PlanV1,
):
    """Journal cancellation even when a coding request never acquires its worktree."""

    lock = get_worktree_lock(project)
    try:
        await lock.acquire()
    except asyncio.CancelledError:
        save_task(
            task_id,
            "local_code",
            "cancelled",
            prompt,
            project,
            route_decision=decision,
            plan=plan,
            actual_executor=ExecutorName.QWEN_CODE,
            actual_model=decision.model,
            actual_profile=decision.profile,
            reason_codes=["executor.cancelled_while_queued"],
        )
        raise
    try:
        yield
    finally:
        lock.release()


async def _run_coding_engine_safely(
    engine: CodingEngine,
    request: CodingTaskRequestV1,
) -> CodingTaskResultV1:
    """Propagate async cancellation without abandoning a live engine thread."""

    cancel_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(engine.run, request, cancel_event=cancel_event)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await worker
        except Exception:
            # The caller's cancellation is authoritative; the worker exception
            # remains represented in Coding Engine state/artifacts.
            pass
        raise


async def execute_coding_engine(
    *,
    task_id: str,
    prompt: str,
    project: str,
    decision: RouteDecisionV1,
    plan: PlanV1,
    engine: CodingEngine | None = None,
) -> CodingTaskResultV1:
    """Execute the governed Coding Engine and mirror its terminal state to the gateway journal."""

    coding_request = build_coding_task_request(
        task_id=task_id,
        prompt=prompt,
        project=project,
        decision=decision,
        plan=plan,
    )
    selected_engine = engine

    def journal_executor(result: CodingTaskResultV1 | None = None) -> ExecutorName:
        if result is not None and result.final_executor in {
            ExecutorKind.CODEX_EXEC,
            ExecutorKind.CODEX_REVIEW,
        }:
            return ExecutorName.CODEX_CLI
        return ExecutorName.QWEN_CODE
    save_task(
        task_id,
        "local_code",
        "running",
        prompt,
        project,
        route_decision=decision,
        plan=plan,
        actual_executor=journal_executor(),
        actual_model=decision.model,
        actual_profile=decision.profile,
        reason_codes=["coding.engine_started"],
        command_summaries=["coding_engine governed execution"],
    )
    try:
        async with AGENT_LOCK:
            async with GPU_LOCK:
                if selected_engine is None:
                    selected_engine = coding_engine_factory()
                result = await _run_coding_engine_safely(selected_engine, coding_request)
    except asyncio.CancelledError:
        save_task(
            task_id,
            "local_code",
            "cancelled",
            prompt,
            project,
            route_decision=decision,
            plan=plan,
            actual_executor=journal_executor(),
            actual_model=decision.model,
            actual_profile=decision.profile,
            reason_codes=["executor.cancelled"],
        )
        raise
    except Exception as exc:
        error = redact_bounded(f"{type(exc).__name__}: {exc}")
        save_task(
            task_id,
            "local_code",
            "failed",
            prompt,
            project,
            error,
            route_decision=decision,
            plan=plan,
            actual_executor=journal_executor(),
            actual_model=decision.model,
            actual_profile=decision.profile,
            error_summary=error,
            reason_codes=["coding.engine_failure"],
        )
        raise

    journal_status = {
        CodingTaskStatus.COMPLETED: "complete",
        CodingTaskStatus.HANDOFF_READY: "ready",
        CodingTaskStatus.BLOCKED: "blocked",
        CodingTaskStatus.FAILED: "failed",
        CodingTaskStatus.CANCELLED: "cancelled",
    }.get(result.status)
    if journal_status is None:
        error = f"Coding Engine returned non-terminal status {result.status.value}"
        save_task(
            task_id,
            "local_code",
            "failed",
            prompt,
            project,
            error,
            route_decision=decision,
            plan=plan,
            actual_executor=journal_executor(),
            actual_model=decision.model,
            actual_profile=decision.profile,
            error_summary=error,
            reason_codes=["coding.invalid_terminal_status"],
        )
        raise CodingEngineError(error)
    handoff_ready = result.status is CodingTaskStatus.HANDOFF_READY
    if handoff_ready:
        artifact_refs = result.artifact_paths[-128:]
        # Close the mirrored local attempt before projecting the ready fallback;
        # the Coding Engine database remains the authoritative per-attempt log.
        save_task(
            task_id,
            "local_code",
            "failed",
            prompt,
            project,
            result.summary,
            route_decision=decision,
            plan=plan,
            actual_executor=journal_executor(result),
            actual_model=result.final_model or decision.model,
            actual_profile=decision.profile,
            error_summary=result.summary,
            reason_codes=["failure.local_attempt_limit"],
            modified_files=result.modified_files,
            artifact_refs=artifact_refs,
            worktree_path=result.worktree_path,
        )
        save_task(
            task_id,
            "codex_bundle",
            "ready",
            prompt,
            project,
            result.summary,
            {"bundle": result.handoff_path, "handoff": result.handoff_path},
            route_decision=decision,
            plan=plan,
            actual_executor=ExecutorName.CODEX_BUNDLE,
            actual_model=decision.model,
            actual_profile=decision.profile,
            fallback_used=True,
            error_summary=result.summary,
            reason_codes=["failure.local_attempt_limit", "fallback.codex_handoff"],
            modified_files=result.modified_files,
            artifact_refs=artifact_refs,
            worktree_path=result.worktree_path,
        )
        return result
    if result.status is CodingTaskStatus.BLOCKED:
        terminal_kwargs = {
            "route_decision": decision,
            "plan": plan,
            "actual_executor": journal_executor(result),
            "actual_model": result.final_model or decision.model,
            "actual_profile": decision.profile,
            "error_summary": result.summary,
            "reason_codes": ["coding.blocked"],
            "modified_files": result.modified_files,
            "artifact_refs": result.artifact_paths[-128:],
            "worktree_path": result.worktree_path,
        }
        save_task(
            task_id,
            "local_code",
            "failed",
            prompt,
            project,
            result.summary,
            **terminal_kwargs,
        )
        save_task(
            task_id,
            "local_code",
            "blocked",
            prompt,
            project,
            result.summary,
            **terminal_kwargs,
        )
        return result
    save_task(
        task_id,
        "local_code",
        journal_status,
        prompt,
        project,
        result.summary,
        None,
        route_decision=decision,
        plan=plan,
        actual_executor=journal_executor(result),
        actual_model=result.final_model or decision.model,
        actual_profile=decision.profile,
        fallback_used=False,
        error_summary=(
            result.summary if result.status is CodingTaskStatus.FAILED else None
        ),
        reason_codes=[
            "coding.review_findings_delivered"
            if (
                result.status is CodingTaskStatus.COMPLETED
                and result.review_verdict is ReviewVerdict.REJECTED
                and result.review_findings_count > 0
            )
            else "coding.completed"
            if result.status is CodingTaskStatus.COMPLETED
            else "executor.cancelled"
            if result.status is CodingTaskStatus.CANCELLED
            else "coding.failed"
        ],
        modified_files=result.modified_files,
        # The gateway TaskState contract is intentionally smaller than the
        # engine artifact registry; retain the newest terminal evidence.
        artifact_refs=result.artifact_paths[-128:],
        worktree_path=result.worktree_path,
    )
    return result


async def execute_local_coding(
    *,
    task_id: str,
    prompt: str,
    project: str,
    decision: RouteDecisionV1,
    plan: PlanV1,
) -> tuple[str | None, Path | None, list[str]]:
    """Legacy retry path retained for compatibility tests; production uses CodingEngine."""

    failures: list[str] = []
    commands: list[str] = []
    async with queued_worktree_lock(
        task_id=task_id,
        prompt=prompt,
        project=project,
        decision=decision,
        plan=plan,
    ):
        for attempt in range(1, decision.max_attempts + 1):
            attempt_prompt = prompt
            if failures:
                attempt_prompt = (
                    f"{prompt}\n\nPrevious bounded attempt failed: {failures[-1]}\n"
                    "Use a different concrete hypothesis, re-inspect the relevant files, and verify the result."
                )
            command_summary = f"qwen_code attempt {attempt} of {decision.max_attempts}"
            commands.append(command_summary)
            save_task(
                task_id,
                "local_code",
                "running",
                prompt,
                project,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.QWEN_CODE,
                actual_model=decision.model,
                actual_profile=decision.profile,
                reason_codes=[f"attempt.local.{attempt}"],
                command_summaries=[command_summary],
            )
            try:
                async with AGENT_LOCK:
                    async with GPU_LOCK:
                        result = await run_blocking_safely(
                            run_qwen_agent,
                            attempt_prompt,
                            project,
                            "code",
                            decision.model or AGENT_MODEL,
                            decision.execution_mode is ExecutionMode.READ_ONLY,
                            plan,
                        )
                if suspicious_agent_result(result):
                    raise RuntimeError("local agent returned an incomplete result")
            except asyncio.CancelledError:
                save_task(
                    task_id,
                    "local_code",
                    "cancelled",
                    prompt,
                    project,
                    route_decision=decision,
                    plan=plan,
                    actual_executor=ExecutorName.QWEN_CODE,
                    actual_model=decision.model,
                    actual_profile=decision.profile,
                    reason_codes=["executor.cancelled"],
                )
                raise
            except Exception as exc:
                memory_content_was_disclosed = any(
                    item.disclosure == "content" and item.content
                    for item in plan.memory_context
                )
                if memory_content_was_disclosed:
                    error = (
                        f"{type(exc).__name__}: local attempt failed with "
                        "memory-assisted context; raw executor output withheld"
                    )
                else:
                    error = redact_bounded(f"{type(exc).__name__}: {exc}")
                failures.append(error)
                save_task(
                    task_id,
                    "local_code",
                    "failed",
                    prompt,
                    project,
                    error,
                    route_decision=decision,
                    plan=plan,
                    actual_executor=ExecutorName.QWEN_CODE,
                    actual_model=decision.model,
                    actual_profile=decision.profile,
                    error_summary=error,
                    reason_codes=["executor.local_failure"],
                    command_summaries=[command_summary],
                    modified_files=collect_modified_files(project),
                )
                continue
            modified = collect_modified_files(project)
            save_task(
                task_id,
                "local_code",
                "complete",
                prompt,
                project,
                result,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.QWEN_CODE,
                actual_model=decision.model,
                actual_profile=decision.profile,
                reason_codes=["executor.local_complete"],
                command_summaries=[command_summary],
                modified_files=modified,
            )
            return result, None, failures

        modified = collect_modified_files(project)
        bundle = create_codex_bundle(
            task_id,
            prompt,
            project,
            plan=plan,
            decision=decision,
            errors=failures,
            modified_files=modified,
            command_summaries=commands,
        )
        save_task(
            task_id,
            "codex_bundle",
            "ready",
            prompt,
            project,
            str(bundle),
            {"bundle": str(bundle)},
            route_decision=decision,
            plan=plan,
            actual_executor=ExecutorName.CODEX_BUNDLE,
            fallback_used=True,
            error_summary=failures[-1] if failures else None,
            reason_codes=["failure.local_attempt_limit", "fallback.codex_bundle"],
            modified_files=modified,
            artifact_refs=[str(bundle)],
        )
        return None, bundle, failures


async def _probe_ollama_model(
    client: httpx.AsyncClient,
    *,
    name: str,
    base_url: str,
    model_name: str,
    checked_at: datetime,
) -> CapabilityHealthV1:
    started_at = time.perf_counter()
    try:
        response = await client.get(f"{base_url}/api/tags")
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("models", [])}
        available = names | {item.removesuffix(":latest") for item in names if item}
        present = model_name in available
        return CapabilityHealthV1(
            name=name,
            required=True,
            status=CapabilityStatus.OK if present else CapabilityStatus.UNAVAILABLE,
            detail=(
                f"Required model profile {model_name} is present."
                if present
                else f"Required model profile {model_name} is absent."
            ),
            checked_at=checked_at,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )
    except Exception as exc:
        return CapabilityHealthV1(
            name=name,
            required=True,
            status=CapabilityStatus.UNAVAILABLE,
            detail=f"Probe failed with {type(exc).__name__}.",
            checked_at=checked_at,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )


async def _probe_optional_http(
    client: httpx.AsyncClient,
    *,
    name: str,
    url: str,
    checked_at: datetime,
) -> CapabilityHealthV1:
    started_at = time.perf_counter()
    try:
        response = await client.get(url)
        response.raise_for_status()
        status = CapabilityStatus.OK
        detail = "Optional endpoint answered successfully."
    except Exception as exc:
        status = CapabilityStatus.UNAVAILABLE
        detail = f"Optional probe failed with {type(exc).__name__}."
    return CapabilityHealthV1(
        name=name,
        required=False,
        status=status,
        detail=detail,
        checked_at=checked_at,
        latency_ms=(time.perf_counter() - started_at) * 1000,
    )


def comfyui_installation_ready(root: Path | None = None) -> bool:
    base = root or ROOT
    portable = base / "modules" / "ComfyUI_windows_portable"
    return all(
        path.is_file()
        for path in (
            portable / "python_embeded" / "python.exe",
            portable / "ComfyUI" / "main.py",
            portable
            / "ComfyUI"
            / "models"
            / "checkpoints"
            / "sd_xl_turbo_1.0_fp16.safetensors",
        )
    )


async def collect_gateway_health():
    checked_at = datetime.now(timezone.utc)
    database_ok, database_detail = await asyncio.to_thread(task_store_ready)
    database_health = CapabilityHealthV1(
        name="task_store",
        required=True,
        status=CapabilityStatus.OK if database_ok else CapabilityStatus.UNAVAILABLE,
        detail=database_detail,
        checked_at=checked_at,
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        fast_health, strong_health, voice_health = await asyncio.gather(
            _probe_ollama_model(
                client,
                name="fast_model",
                base_url=FAST_OLLAMA_BASE_URL,
                model_name=FAST_MODEL,
                checked_at=checked_at,
            ),
            _probe_ollama_model(
                client,
                name="strong_model",
                base_url=OLLAMA_BASE_URL,
                model_name=STRONG_MODEL,
                checked_at=checked_at,
            ),
            _probe_optional_http(
                client,
                name="voice",
                url=f"http://127.0.0.1:{SETTINGS.voice_port}/health",
                checked_at=checked_at,
            ),
        )

    mcp_status_map = {
        AvailabilityStatus.AVAILABLE: CapabilityStatus.OK,
        AvailabilityStatus.DEGRADED: CapabilityStatus.DEGRADED,
        AvailabilityStatus.UNAVAILABLE: CapabilityStatus.UNAVAILABLE,
        AvailabilityStatus.DISABLED: CapabilityStatus.DISABLED,
        AvailabilityStatus.ON_DEMAND: CapabilityStatus.ON_DEMAND,
    }
    managed_mcp_capabilities = []
    for server_id in ("context7", "playwright", "local-diagnostics"):
        health_status, health_detail = managed_mcp_health_observation(server_id)
        managed_mcp_capabilities.append(
            CapabilityHealthV1(
                name=f"mcp_{server_id.replace('-', '_')}",
                required=False,
                status=mcp_status_map[health_status],
                detail=health_detail,
                checked_at=checked_at,
            )
        )
    optional_capabilities = [
        CapabilityHealthV1(
            name="qwen_code_cli",
            required=False,
            status=(
                CapabilityStatus.DISABLED
                if not ENABLE_LOCAL_CODE_EXEC
                else CapabilityStatus.OK
                if (shutil.which("qwen.cmd") or shutil.which("qwen"))
                else CapabilityStatus.UNAVAILABLE
            ),
            detail=(
                "Local code execution is disabled by configuration."
                if not ENABLE_LOCAL_CODE_EXEC
                else "CLI installation check only; task execution is tested separately."
            ),
            checked_at=checked_at,
        ),
        CapabilityHealthV1(
            name="codex_cli",
            required=False,
            status=(
                CapabilityStatus.DISABLED
                if not ENABLE_CODEX_EXEC
                else CapabilityStatus.OK
                if resolve_codex_executable()
                else CapabilityStatus.UNAVAILABLE
            ),
            detail=(
                "Codex execution is disabled by configuration."
                if not ENABLE_CODEX_EXEC
                else "CLI installation check only; login and cloud execution are tested separately."
            ),
            checked_at=checked_at,
        ),
        CapabilityHealthV1(
            name="browser_adapter",
            required=False,
            status=(
                CapabilityStatus.OK
                if shutil.which("node") and (ROOT / "services" / "browser" / "inspect.mjs").is_file()
                else CapabilityStatus.UNAVAILABLE
            ),
            detail="Node executable and bounded adapter file check only.",
            checked_at=checked_at,
        ),
        CapabilityHealthV1(
            name="comfyui",
            required=False,
            status=(
                CapabilityStatus.ON_DEMAND
                if comfyui_installation_ready()
                else CapabilityStatus.UNAVAILABLE
            ),
            detail=(
                "Installed runtime/checkpoint is intentionally stopped in idle and starts on demand."
                if comfyui_installation_ready()
                else "Portable runtime, main module, or required SDXL Turbo checkpoint is missing."
            ),
            checked_at=checked_at,
        ),
        CapabilityHealthV1(
            name="telegram",
            required=False,
            status=(
                CapabilityStatus.DEGRADED
                if SETTINGS.telegram_credential.get_secret_value().strip()
                else CapabilityStatus.DISABLED
            ),
            detail=(
                "Credential configured; polling process health is owned by lifecycle scripts."
                if SETTINGS.telegram_credential.get_secret_value().strip()
                else "Credential not configured."
            ),
            checked_at=checked_at,
        ),
    ]
    capabilities = [
        database_health,
        fast_health,
        strong_health,
        voice_health,
        *managed_mcp_capabilities,
        *optional_capabilities,
    ]
    return aggregate_health("gateway", capabilities, live=True, checked_at=checked_at)


@app.get("/health/live")
async def health_live() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "service": "gateway",
        "live": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    report = await collect_gateway_health()
    return JSONResponse(report.model_dump(mode="json"), status_code=200 if report.ready else 503)


@app.get("/health")
async def health() -> dict[str, Any]:
    report = await collect_gateway_health()
    by_name = {capability.name: capability for capability in report.capabilities}
    fast_present = by_name["fast_model"].status is CapabilityStatus.OK
    strong_present = by_name["strong_model"].status is CapabilityStatus.OK
    return {
        "gateway": "ok",
        "fast_model": FAST_MODEL,
        "strong_model": STRONG_MODEL,
        "ollama": "ok" if strong_present else "error",
        "fast_ollama": "ok" if fast_present else "error",
        "fast_model_present": fast_present,
        "strong_model_present": strong_present,
        "status": "ok" if report.ready else "degraded",
        "health": report.model_dump(mode="json"),
    }


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "local-agent-auto", "object": "model", "created": int(time.time()), "owned_by": "local-agent"}],
    }


@app.get("/v1/route")
def route_preview(text: str) -> dict[str, Any]:
    request = normalize_entry_request(
        ChatRequest(messages=[{"role": "user", "content": text}]),
        source=RequestSource.API,
    )
    decision = build_route_decision(request)
    payload = decision.model_dump(mode="json")
    payload["project"] = decision.project or ""
    return payload


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest):
    if not last_user_text(request.messages).strip():
        raise HTTPException(status_code=422, detail="A non-empty user message is required.")
    normalized = normalize_entry_request(request, source=RequestSource.API)
    prompt = normalized.user_message
    if not prompt:
        raise HTTPException(status_code=422, detail="A non-empty task must follow a routing override.")
    execution_request = request.model_copy(
        update={"messages": replace_last_user_text(request.messages, prompt)}
    )
    planning = plan_request(normalized)
    decision = route_request(
        normalized,
        planning,
        capabilities=routing_capability_snapshot(),
        fast_model=FAST_MODEL,
        strong_model=STRONG_MODEL,
        agent_model=AGENT_MODEL,
        codex_model=CODEX_MODEL,
    )
    if TRUSTED_GATEWAY_REQUEST.get():
        planning = (
            attach_memory_to_planning(normalized, planning, decision)
            if planning.plan is None
            else await asyncio.to_thread(
                attach_memory_to_planning,
                normalized,
                planning,
                decision,
            )
        )
    route = decision.route.value
    task_id = normalized.request_id
    project = decision.project
    plan = planning.plan

    if route in {"auxiliary", "fast_chat", "strong_chat"}:
        if decision.executor is ExecutorName.DEGRADED_RESPONSE:
            save_task(
                task_id,
                route,
                "blocked",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=decision.executor,
            )
            return openai_error_response(
                message=f"Локальная capability `{decision.capability}` сейчас недоступна.",
                code=decision.blocking_reason_codes[0] if decision.blocking_reason_codes else "capability.unavailable",
                route=route,
                request_id=task_id,
                status_code=503,
                decision=decision,
                plan=plan,
            )
        model = STRONG_MODEL if route == "strong_chat" else FAST_MODEL
        thinking = route == "strong_chat"
        save_task(
            task_id,
            route,
            "running",
            prompt,
            route_decision=decision,
            plan=plan,
            actual_executor=decision.executor,
            actual_model=model,
            actual_profile=decision.profile,
        )
        try:
            response = await local_chat(execution_request, model, route, thinking, request_id=task_id)
            if isinstance(response, StreamingResponse):
                return track_stream_task(
                    response,
                    task_id=task_id,
                    route=route,
                    prompt=prompt,
                    model=model,
                )
            save_task(
                task_id,
                route,
                "complete",
                prompt,
                result=f"forwarded to {model}",
                route_decision=decision,
                plan=plan,
                actual_executor=decision.executor,
                actual_model=model,
                actual_profile=decision.profile,
            )
            return response
        except asyncio.CancelledError:
            save_task(
                task_id,
                route,
                "cancelled",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=decision.executor,
                actual_model=model,
                actual_profile=decision.profile,
                reason_codes=["executor.cancelled"],
            )
            raise
        except Exception as exc:
            error = redact_bounded(str(exc))
            save_task(
                task_id,
                route,
                "failed",
                prompt,
                result=error,
                route_decision=decision,
                plan=plan,
                actual_executor=decision.executor,
                actual_model=model,
                actual_profile=decision.profile,
                error_summary=error,
            )
            return openai_error_response(
                message=f"Локальная модель завершилась ошибкой: {error}",
                code="executor.local_model_failure",
                route=route,
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )

    if route in {"codex", "codex_bundle"} and decision.executor is ExecutorName.CODEX_BUNDLE:
        assert plan is not None
        bundle = create_codex_bundle(
            task_id,
            prompt,
            project,
            plan=plan,
            decision=decision,
            errors=["Codex execution is waiting for scoped cloud approval or capability recovery."],
        )
        save_task(
            task_id,
            "codex_bundle",
            "ready",
            prompt,
            project,
            str(bundle),
            {"bundle": str(bundle)},
            route_decision=decision,
            plan=plan,
            actual_executor=ExecutorName.CODEX_BUNDLE,
            fallback_used=True,
            reason_codes=decision.reason_codes,
            artifact_refs=[str(bundle)],
        )
        code = (
            "permission.cloud_approval_required"
            if decision.permission_disposition.value == "approval_required"
            else "capability.codex.unavailable"
        )
        return openai_error_response(
            message=f"Задача сохранена для Codex, но не выдана за выполненную: {bundle}",
            code=code,
            route="codex_bundle",
            request_id=task_id,
            status_code=409 if code.startswith("permission") else 503,
            decision=decision,
            plan=plan,
        )

    if decision.executor is ExecutorName.DEGRADED_RESPONSE or decision.decision_status is not DecisionStatus.READY:
        state_status = "blocked" if decision.decision_status is DecisionStatus.BLOCKED else "ready"
        save_task(
            task_id,
            route,
            state_status,
            prompt,
            project,
            route_decision=decision,
            plan=plan,
            actual_executor=ExecutorName.DEGRADED_RESPONSE,
            reason_codes=decision.blocking_reason_codes or decision.reason_codes,
        )
        reason = (
            decision.blocking_reason_codes[0]
            if decision.blocking_reason_codes
            else f"capability.{decision.capability}.unavailable"
        )
        messages = {
            "voice.attachment_missing": "Для /voice приложите аудиофайл; обычная транскрипция доступна через /v1/audio/transcriptions.",
            "vision.attachment_missing": "Для /vision приложите изображение.",
            "project.explicit_invalid": "Явно указанный project path не существует; DEFAULT_PROJECT не использован.",
            "project.missing": "Для repository-задачи нужен существующий project path.",
            "permission.network_target_denied": "Browser policy запрещает локальные/private network targets.",
            "permission.critical_action_denied": "Критическое production-действие не разрешено routing override.",
            "permission.high_risk_local_override_denied": "High-risk запись не может обойти Codex approval через /local.",
            "permission.read_only_conflict": "Запрос одновременно требует и запрещает изменения; выполнение заблокировано.",
            "context.agent_input_exceeds_budget": "Полный executable Plan не помещается в безопасный контекст Qwen Code; задача не запущена.",
            "override.conflict": "Указаны конфликтующие routing overrides.",
        }
        message = messages.get(
            reason,
            f"Capability `{decision.capability}` недоступна или работает в degraded mode; задача не выполнена.",
        )
        status_code = 422 if reason.startswith(("project.", "voice.", "vision.", "override.", "context.")) else 403 if reason.startswith("permission.") else 503
        return openai_error_response(
            message=message,
            code=reason,
            route=route,
            request_id=task_id,
            status_code=status_code,
            decision=decision,
            plan=plan,
        )

    if route == "local_code":
        assert project is not None and plan is not None
        try:
            coding_result = await execute_coding_engine(
                task_id=task_id,
                prompt=prompt,
                project=project,
                decision=decision,
                plan=plan,
            )
        except asyncio.CancelledError:
            raise
        except CodingRepositoryError as exc:
            return openai_error_response(
                message=redact_bounded(str(exc)),
                code="coding.repository_invalid",
                route=route,
                request_id=task_id,
                status_code=422,
                decision=decision,
                plan=plan,
            )
        except Exception as exc:
            return openai_error_response(
                message=redact_bounded(f"{type(exc).__name__}: {exc}"),
                code="coding.engine_failure",
                route=route,
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )
        if coding_result.status is CodingTaskStatus.COMPLETED:
            details = [coding_result.summary]
            if coding_result.worktree_path:
                details.append(f"Worktree: {coding_result.worktree_path}")
            if coding_result.modified_files:
                details.append("Modified: " + ", ".join(coding_result.modified_files))
            details.append(
                "Verification: "
                + ("passed" if coding_result.verification_passed else "not passed")
            )
            if coding_result.review_verdict is ReviewVerdict.REJECTED:
                details.append(
                    "Review: findings delivered ("
                    f"{coding_result.review_findings_count}; code-change verdict rejected)"
                )
            elif coding_result.review_verdict is not None:
                details.append(f"Review: {coding_result.review_verdict.value}")
            if coding_result.commit_sha:
                details.append(f"Local commit: {coding_result.commit_sha}")
            return openai_response(
                "\n\n".join(details),
                route,
                request.stream,
                request_id=task_id,
                plan=plan,
            )
        if coding_result.status is CodingTaskStatus.HANDOFF_READY:
            return openai_error_response(
                message=(
                    "Bounded local attempts stopped; a resumable Codex handoff is ready: "
                    f"{coding_result.handoff_path}"
                ),
                code="failure.local_attempt_limit",
                route="codex_bundle",
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )
        status_code = (
            409
            if coding_result.status
            in {CodingTaskStatus.BLOCKED, CodingTaskStatus.CANCELLED}
            else 502
        )
        return openai_error_response(
            message=coding_result.summary,
            code=f"coding.{coding_result.status.value}",
            route=route,
            request_id=task_id,
            status_code=status_code,
            decision=decision,
            plan=plan,
        )

    if route == "docs":
        assert plan is not None
        external_request, external_plan = prepare_public_docs_execution(normalized)
        external_prompt = external_request.user_message
        external_decision = decision.model_copy(update={"project": None})
        # Qwen merges workspace settings.  A fresh platform-owned cwd prevents
        # repository `.qwen/settings.json` from becoming an MCP consumer.
        docs_workspace, docs_workspace_identity = _new_guarded_docs_directory("request-")
        docs_project = str(docs_workspace)
        try:
            try:
                result = await execute_agent(
                    task_id,
                    route,
                    external_prompt,
                    docs_project,
                    cloud=False,
                    mode="docs",
                    decision=external_decision,
                    plan=external_plan,
                )
                return openai_response(
                    result,
                    route,
                    request.stream,
                    request_id=task_id,
                    plan=external_plan,
                )
            except Exception as exc:
                error = redact_bounded(str(exc))
                return openai_error_response(
                    message=f"Context7 worker завершился ошибкой: {error}",
                    code="executor.context7_failure",
                    route=route,
                    request_id=task_id,
                    status_code=502,
                    decision=external_decision,
                    plan=external_plan,
                )
        finally:
            _remove_guarded_docs_directory(docs_workspace, docs_workspace_identity)

    if route == "voice":
        assert plan is not None
        try:
            save_task(
                task_id,
                route,
                "running",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.WHISPER,
            )
            transcript = await transcribe_chat_audio(request.messages)
            save_task(
                task_id,
                route,
                "complete",
                prompt,
                result="Whisper transcription completed.",
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.WHISPER,
            )
            return openai_response(transcript, route, request.stream, request_id=task_id)
        except asyncio.CancelledError:
            save_task(
                task_id,
                route,
                "cancelled",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.WHISPER,
                reason_codes=["executor.cancelled"],
            )
            raise
        except Exception as exc:
            error = redact_bounded(str(exc))
            save_task(
                task_id,
                route,
                "failed",
                prompt,
                result=error,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.WHISPER,
                error_summary=error,
            )
            return openai_error_response(
                message=f"Whisper worker завершился ошибкой: {error}",
                code="executor.voice_failure",
                route=route,
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )

    if route == "codex":
        assert plan is not None
        if decision.executor is ExecutorName.CODEX_CLI:
            execution_project = project
            if execution_project is None:
                codex_workspace = RUN_DIR / "codex-workspace"
                codex_workspace.mkdir(parents=True, exist_ok=True)
                execution_project = str(codex_workspace)
            try:
                codex_mode = "review" if decision.execution_mode is ExecutionMode.READ_ONLY else "code"
                result = await execute_agent(
                    task_id,
                    route,
                    prompt,
                    execution_project,
                    cloud=True,
                    mode=codex_mode,
                    decision=decision,
                    plan=plan,
                )
                return openai_response(result, route, request.stream, request_id=task_id, plan=plan)
            except Exception as exc:
                error = redact_bounded(str(exc))
                bundle = create_codex_bundle(
                    task_id,
                    prompt,
                    project,
                    plan=plan,
                    decision=decision,
                    errors=[error],
                    modified_files=collect_modified_files(execution_project),
                    command_summaries=["codex_cli bounded invocation failed"],
                )
                save_task(
                    task_id,
                    "codex_bundle",
                    "ready",
                    prompt,
                    project,
                    error,
                    {"bundle": str(bundle)},
                    route_decision=decision,
                    plan=plan,
                    actual_executor=ExecutorName.CODEX_BUNDLE,
                    fallback_used=True,
                    error_summary=error,
                    artifact_refs=[str(bundle)],
                )
                return openai_error_response(
                    message=f"Codex не завершил задачу; handoff сохранён: {bundle}",
                    code="executor.codex_failure",
                    route="codex_bundle",
                    request_id=task_id,
                    status_code=502,
                    decision=decision,
                    plan=plan,
                )

    if route == "browser":
        assert plan is not None and planning.signals.public_url is not None
        try:
            save_task(
                task_id,
                route,
                "running",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.PLAYWRIGHT,
            )
            output = await run_blocking_safely(
                run_process,
                ["node", str(ROOT / "services" / "browser" / "inspect.mjs"), planning.signals.public_url],
                str(ROOT),
                120,
            )
            save_task(
                task_id,
                route,
                "complete",
                prompt,
                result=output,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.PLAYWRIGHT,
            )
            return openai_response(output, route, request.stream, request_id=task_id)
        except asyncio.CancelledError:
            save_task(
                task_id,
                route,
                "cancelled",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.PLAYWRIGHT,
                reason_codes=["executor.cancelled"],
            )
            raise
        except Exception as exc:
            error = redact_bounded(str(exc))
            save_task(
                task_id,
                route,
                "failed",
                prompt,
                result=error,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.PLAYWRIGHT,
                error_summary=error,
            )
            return openai_error_response(
                message=f"Playwright worker завершился ошибкой: {error}",
                code="executor.browser_failure",
                route=route,
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )

    if route == "image":
        assert plan is not None
        try:
            save_task(
                task_id,
                route,
                "running",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.COMFYUI,
            )
            async with IMAGE_LOCK:
                async with GPU_LOCK:
                    image_prompt = await run_blocking_safely(normalize_image_prompt, prompt)
                    output = await run_blocking_safely(
                        run_process,
                        [
                            "powershell.exe", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "scripts" / "generate-image.ps1"), "-Prompt", image_prompt,
                        ],
                        str(ROOT), 1200,
                    )
            save_task(
                task_id,
                route,
                "complete",
                prompt,
                result=output,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.COMFYUI,
            )
            url_match = re.search(r"IMAGE_URL=(\S+)", output)
            text = f"![Generated image]({url_match.group(1)})" if url_match else output
            return openai_response(text, route, request.stream, request_id=task_id)
        except asyncio.CancelledError:
            save_task(
                task_id,
                route,
                "cancelled",
                prompt,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.COMFYUI,
                reason_codes=["executor.cancelled"],
            )
            raise
        except Exception as exc:
            error = redact_bounded(str(exc))
            save_task(
                task_id,
                route,
                "failed",
                prompt,
                result=error,
                route_decision=decision,
                plan=plan,
                actual_executor=ExecutorName.COMFYUI,
                error_summary=error,
            )
            return openai_error_response(
                message=f"ComfyUI worker завершился ошибкой: {error}",
                code="executor.image_failure",
                route=route,
                request_id=task_id,
                status_code=502,
                decision=decision,
                plan=plan,
            )

    raise HTTPException(status_code=500, detail=f"Unknown route: {route}")
