import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse

from services.gateway import app as gateway
from services.gateway.app import ChatRequest
from services.orchestration.router import CapabilitySnapshot, assumed_capabilities
from services.contracts import AvailabilityStatus


def test_ingress_normalization_references_but_does_not_embed_inline_image(tmp_path):
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Project: {tmp_path}; inspect image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
    )

    normalized = gateway.normalize_request(request, request_id="request-001")
    serialized = normalized.model_dump_json()

    assert normalized.request_id == "request-001"
    assert normalized.project_hint == str(tmp_path.resolve())
    assert normalized.attachments[0].reference == "request-message:0:part:1"
    assert normalized.attachments[0].media_type == "image/png"
    assert "base64" not in serialized
    assert "AAAA" not in serialized


def test_route_decision_wraps_existing_classifier_with_current_resources(tmp_path):
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": f"Project: {tmp_path}; create hello.py and run tests",
            }
        ]
    )
    normalized = gateway.normalize_request(request, request_id="request-002")
    decision = gateway.build_route_decision(normalized)

    assert decision.route == "local_code"
    assert decision.executor == "qwen_code"
    assert decision.model == gateway.AGENT_MODEL
    assert {"qwen_agent", "gpu_heavy"}.issubset(decision.required_locks)
    assert any(lock.startswith("worktree:") for lock in decision.required_locks)
    assert decision.fallback.route == "codex_bundle"


def test_route_decision_reports_degraded_executor_when_local_feature_is_unavailable(tmp_path, monkeypatch):
    local_request = ChatRequest(
        messages=[{"role": "user", "content": f"Project: {tmp_path}; create hello.py"}]
    )
    monkeypatch.setattr(gateway, "ENABLE_LOCAL_CODE_EXEC", False)
    local_decision = gateway.build_route_decision(
        gateway.normalize_request(local_request, request_id="request-disabled-local")
    )
    assert local_decision.route == "local_code"
    assert local_decision.executor == "degraded_response"
    assert "capability.qwen_code.unavailable" in local_decision.reason_codes
    assert local_decision.decision_status == "degraded"
    assert local_decision.required_locks == []

    codex_request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": f"Project: {tmp_path}; perform a security review before merge",
            }
        ]
    )
    monkeypatch.setattr(gateway, "ENABLE_CODEX_EXEC", False)
    codex_decision = gateway.build_route_decision(
        gateway.normalize_request(codex_request, request_id="request-disabled-codex")
    )
    assert codex_decision.route == "codex"
    assert codex_decision.executor == "codex_bundle"
    assert codex_decision.model is None
    assert codex_decision.required_locks == []


def test_route_preview_keeps_legacy_fields_and_adds_versioned_decision(monkeypatch):
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)
    result = gateway.route_preview("What is dependency injection?")

    assert result["route"] == "fast_chat"
    assert result["project"] == ""
    assert result["schema_version"] == "1.0"
    assert result["executor"] == "fast_ollama"
    assert "action.chat" in result["reason_codes"]
    assert result["policy_version"] == "2026-07-14.1"
    assert result["action"] == "chat"
    assert result["decision_status"] == "ready"


def test_liveness_endpoint_has_no_dependency_probe(monkeypatch):
    async def forbidden_probe():
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr(gateway, "collect_gateway_health", forbidden_probe)
    result = asyncio.run(gateway.health_live())

    assert result["live"] is True
    assert result["schema_version"] == "1.0"


def test_entry_validation_returns_bounded_422_without_echoing_payload():
    marker = "private-marker-that-must-not-be-echoed"
    request = ChatRequest(messages=[{"role": "user", "content": marker + "x" * 262_144}])

    with pytest.raises(HTTPException) as caught:
        gateway.normalize_entry_request(request)

    assert caught.value.status_code == 422
    assert marker not in str(caught.value.detail)


def test_stream_journal_completes_only_after_iterator_finishes(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        assert not writes
        yield b"data: [DO"
        yield b"NE]\n\n"

    response = StreamingResponse(source())
    tracked = gateway.track_stream_task(
        response,
        task_id="task-001",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in tracked.body_iterator])

    assert asyncio.run(consume()).endswith(b"data: [DONE]\n\n")
    assert writes[-1][0][2] == "complete"


def test_stream_journal_rejects_clean_but_truncated_eof(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="task-truncated",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    asyncio.run(consume())
    assert writes[-1][0][2] == "failed"
    assert "[DONE]" in writes[-1][1]["result"]


def test_stream_journal_does_not_accept_done_text_inside_json(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b'data: {"choices":[{"delta":{"content":"say data: [DONE] literally"}}]}\n\n'

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="task-false-marker",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    asyncio.run(consume())
    assert writes[-1][0][2] == "failed"


def test_stream_journal_records_cancellation(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b"partial"
        raise asyncio.CancelledError

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="task-cancelled",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(consume())
    assert writes[-1][0][2] == "cancelled"


def test_stream_journal_records_generator_failure(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b"partial"
        raise RuntimeError("upstream closed")

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="task-002",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    with pytest.raises(RuntimeError, match="upstream closed"):
        asyncio.run(consume())
    assert writes[-1][0][2] == "failed"


def test_synthetic_response_exposes_correlation_without_changing_model_contract():
    response = gateway.openai_response(
        "done", "local_code", request_id="request-003"
    )

    assert response.headers["X-Local-Agent-Request-ID"] == "request-003"
    assert b'"model":"local-agent-auto"' in response.body
    assert b'"local_agent_request_id":"request-003"' in response.body


def test_comfyui_on_demand_health_requires_runtime_and_checkpoint(tmp_path):
    portable = tmp_path / "modules" / "ComfyUI_windows_portable"
    python = portable / "python_embeded" / "python.exe"
    main = portable / "ComfyUI" / "main.py"
    checkpoint = (
        portable
        / "ComfyUI"
        / "models"
        / "checkpoints"
        / "sd_xl_turbo_1.0_fp16.safetensors"
    )
    python.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    python.touch()
    main.touch()

    assert gateway.comfyui_installation_ready(tmp_path) is False
    checkpoint.parent.mkdir(parents=True)
    checkpoint.touch()
    assert gateway.comfyui_installation_ready(tmp_path) is True


def test_unavailable_optional_route_returns_json_error_before_sse_and_skips_adapter(monkeypatch):
    base = assumed_capabilities()
    statuses = dict(base.statuses)
    statuses["browser"] = AvailabilityStatus.UNAVAILABLE
    snapshot = CapabilitySnapshot(statuses=statuses, checked_at=base.checked_at)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", lambda: snapshot)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway,
        "run_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("adapter must not run")),
    )

    response = asyncio.run(
        gateway.chat(
            ChatRequest(
                messages=[{"role": "user", "content": "Open https://example.com"}],
                stream=True,
            )
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"code":"capability.browser.unavailable"' in response.body


def test_execution_receives_override_stripped_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)

    async def fake_local_chat(request, model, route, thinking, request_id=None):
        captured["text"] = request.messages[-1]["content"]
        return gateway.openai_response("done", route, request_id=request_id)

    monkeypatch.setattr(gateway, "local_chat", fake_local_chat)
    response = asyncio.run(
        gateway.chat(
            ChatRequest(messages=[{"role": "user", "content": "/local What is dependency injection?"}])
        )
    )

    assert response.status_code == 200
    assert captured["text"] == "What is dependency injection?"


def test_direct_internal_chat_call_does_not_open_memory_without_authenticated_boundary(monkeypatch):
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)
    monkeypatch.setattr(
        gateway,
        "attach_memory_to_planning",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted direct call must not retrieve memory")
        ),
    )

    async def fake_local_chat(request, model, route, thinking, request_id=None):
        return gateway.openai_response("done", route, request_id=request_id)

    monkeypatch.setattr(gateway, "local_chat", fake_local_chat)
    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": "Hello"}]))
    )
    assert response.status_code == 200


def test_chat_audio_attachment_executes_whisper_adapter(monkeypatch):
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)

    async def fake_transcribe(messages):
        return "локальная расшифровка"

    monkeypatch.setattr(gateway, "transcribe_chat_audio", fake_transcribe)
    response = asyncio.run(
        gateway.chat(
            ChatRequest(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "/voice расшифруй"},
                            {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
                        ],
                    }
                ]
            )
        )
    )

    assert response.status_code == 200
    assert b'"local_agent_route":"voice"' in response.body
    assert "локальная расшифровка" in response.body.decode("utf-8")


@pytest.mark.parametrize(
    "prompt",
    [
        r"Project: C:\definitely-missing; security review repository",
        "/local /codex security review repository",
    ],
)
def test_validation_gates_never_create_codex_bundle(prompt, monkeypatch):
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway,
        "create_codex_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bundle must not be created")),
    )

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": prompt}]))
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 422


def test_read_only_qwen_uses_plan_mode_and_stdin(monkeypatch):
    captured = {}

    def fake_run(command, project, timeout, input_text=None, **kwargs):
        captured.update(command=command, input_text=input_text, **kwargs)
        return "Done; files inspected."

    monkeypatch.setattr(gateway, "run_process", fake_run)
    result = gateway.run_qwen_agent(
        "Inspect repository files and do not modify them.",
        "C:\\work\\repo",
        read_only=True,
    )

    command = captured["command"]
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert command[command.index("--prompt") + 1] == ""
    assert "--bare" in command
    assert command[command.index("--auth-type") + 1] == "openai"
    assert "--allowed-mcp-server-names" not in command
    assert "Inspect repository files" in captured["input_text"]
    assert captured["env_overrides"]["QWEN_HOME"].endswith("run\\qwen-homes\\qwen-code")
    assert "config\\qwen-code" not in captured["env_overrides"]["QWEN_HOME"]
    assert result == "Done; files inspected."


def test_docs_qwen_allows_only_context7_and_uses_neutral_profile(monkeypatch):
    captured = {}

    def fake_run(command, project, timeout, input_text=None, **kwargs):
        captured.update(command=command, project=project, input_text=input_text, **kwargs)
        return "Current documentation retrieved."

    monkeypatch.setattr(gateway, "run_process", fake_run)
    result = gateway.run_qwen_agent(
        "Find current FastAPI lifespan documentation.",
        "C:\\neutral\\docs",
        mode="docs",
        read_only=True,
    )

    command = captured["command"]
    assert "--bare" not in command
    assert command[command.index("--allowed-mcp-server-names") + 1] == "context7"
    assert captured["env_overrides"]["QWEN_HOME"].endswith("run\\qwen-homes\\qwen-docs")
    assert "config\\qwen-docs" not in captured["env_overrides"]["QWEN_HOME"]
    assert result == "Current documentation retrieved."


def test_qwen_executable_plan_is_bounded_and_preserves_contract_fields(tmp_path, monkeypatch):
    captured = {}
    prompt = (
        f"Project: {tmp_path}; create result.txt with exactly:\n"
        "A=1\nB=2\nDo not modify README.md"
    )
    normalized = gateway.normalize_request(
        ChatRequest(messages=[{"role": "user", "content": prompt}]),
        request_id="bounded-plan",
    )
    planning = gateway.plan_request(normalized)
    assert planning.plan is not None
    plan = planning.plan

    def fake_run(command, project, timeout, input_text=None, **kwargs):
        captured["input_text"] = input_text
        return "Created and verified result.txt."

    monkeypatch.setattr(gateway, "run_process", fake_run)
    gateway.run_qwen_agent(
        normalized.user_message,
        str(tmp_path),
        read_only=False,
        plan=plan,
    )

    agent_input = captured["input_text"]
    assert plan.goal in agent_input
    assert "A=1\nB=2" in agent_input
    assert all(item in agent_input for item in plan.constraints)
    assert all(item in agent_input for item in plan.acceptance_criteria)
    assert all(item in agent_input for item in plan.verification_plan)
    assert len(agent_input.encode("utf-8")) <= plan.context_budget.max_input_tokens


def test_oversized_executable_plan_never_launches_qwen(tmp_path, monkeypatch):
    prompt = f"Project: {tmp_path}; fix the test. " + ("x" * 20_000)
    normalized = gateway.normalize_request(
        ChatRequest(messages=[{"role": "user", "content": prompt}]),
        request_id="oversized-plan",
    )
    planning = gateway.plan_request(normalized)
    assert planning.plan is not None
    monkeypatch.setattr(
        gateway,
        "run_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Qwen must not launch")),
    )

    with pytest.raises(gateway.AgentContextBudgetExceeded):
        gateway.run_qwen_agent(
            normalized.user_message,
            str(tmp_path),
            read_only=False,
            plan=planning.plan,
        )


def test_short_concrete_agent_result_is_not_treated_as_evasive():
    assert gateway.suspicious_agent_result("Done; tests pass.") is False
    assert gateway.suspicious_agent_result("What would you like me to do?") is True


def test_bounded_chat_context_limits_structured_payload_and_marks_omission():
    messages = [
        {"role": "user", "content": "A" * 40_000},
        {
            "role": "assistant",
            "content": "tool requested",
            "tool_calls": [{"id": "x", "function": {"name": "huge", "arguments": "B" * 40_000}}],
        },
        {"role": "user", "content": "current task must survive"},
    ]

    bounded = gateway.bounded_chat_messages(messages, max_input_tokens=2_000)
    encoded = json.dumps(bounded, ensure_ascii=False)

    assert "current task must survive" in encoded
    assert "context truncated" in encoded
    assert "B" * 1_000 not in encoded
    assert len(encoded) < 10_000


def test_bounded_chat_context_never_reintroduces_huge_text_or_orphan_tools():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "huge", "arguments": "X" * 40_000}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        {"role": "user", "content": "A" * 1_000_000},
        {"role": "user", "content": "current"},
    ]

    bounded = gateway.bounded_chat_messages(messages, max_input_tokens=256)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= 1_024
    assert "current" in encoded
    assert not any(message["role"] == "tool" for message in bounded)
    assert "None" not in encoded


def test_bounded_chat_context_keeps_only_complete_tool_exchanges():
    complete = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "one", "arguments": "{}"}},
                {"id": "call-2", "type": "function", "function": {"name": "two", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "one"},
        {"role": "tool", "tool_call_id": "call-2", "content": "two"},
        {"role": "user", "content": "continue"},
    ]
    incomplete = [complete[0], complete[1], complete[-1]]

    kept = gateway.bounded_chat_messages(complete, max_input_tokens=2_000)
    dropped = gateway.bounded_chat_messages(incomplete, max_input_tokens=2_000)

    assert [message["role"] for message in kept] == ["assistant", "tool", "tool", "user"]
    assert kept[0]["content"] == ""
    assert [message["role"] for message in dropped] == ["user"]
    assert "context truncated" in dropped[0]["content"]


def test_bounded_chat_context_preserves_complete_legacy_function_exchange():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "lookup", "arguments": "{}"},
        },
        {"role": "function", "name": "lookup", "content": "result"},
        {"role": "user", "content": "continue"},
    ]

    bounded = gateway.bounded_chat_messages(messages, max_input_tokens=2_000)

    assert [message["role"] for message in bounded] == ["assistant", "function", "user"]
    assert bounded[0]["function_call"]["name"] == "lookup"
    assert bounded[0]["content"] == ""


def test_capability_snapshot_uses_cached_listener_state(monkeypatch):
    monkeypatch.setattr(gateway, "ROUTING_CAPABILITY_CACHE", None)
    monkeypatch.setattr(gateway, "ENABLE_LOCAL_CODE_EXEC", True)
    monkeypatch.setattr(gateway, "ENABLE_CODEX_EXEC", True)
    monkeypatch.setattr(gateway.shutil, "which", lambda name: f"C:\\bin\\{name}")
    monkeypatch.setattr(
        gateway,
        "tcp_endpoint_reachable",
        lambda url, **kwargs: url != gateway.OLLAMA_BASE_URL,
    )

    snapshot = gateway.routing_capability_snapshot()

    assert snapshot.status("fast_model") == "available"
    assert snapshot.status("strong_model") == "unavailable"
    assert snapshot.status("qwen_code") == "disabled"
    assert snapshot.status("voice") == "available"


def test_stream_connect_failure_happens_before_streaming_response(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            raise httpx.ConnectError("listener is down")

        async def aclose(self):
            pass

    monkeypatch.setattr(gateway.httpx, "AsyncClient", FailingClient)
    lock = asyncio.Lock()

    async def scenario():
        with pytest.raises(httpx.ConnectError):
            await gateway.preopen_local_stream(
                url="http://127.0.0.1:1/v1/chat/completions",
                payload={"stream": True},
                model_lock=lock,
                headers={},
                route="fast_chat",
                model="local-fast",
                started_at=time.perf_counter(),
            )
        assert not lock.locked()

    asyncio.run(scenario())


def test_midstream_failure_emits_terminal_error_and_journals_failed(monkeypatch):
    writes = []

    class FailingResponse:
        def raise_for_status(self):
            return None

        def aiter_bytes(self):
            async def chunks():
                yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
                raise RuntimeError("socket closed")

            return chunks()

        async def aclose(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return FailingResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(gateway.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def scenario():
        response = await gateway.preopen_local_stream(
            url="http://127.0.0.1:1/v1/chat/completions",
            payload={"stream": True},
            model_lock=asyncio.Lock(),
            headers={},
            route="fast_chat",
            model="local-fast",
            started_at=time.perf_counter(),
        )
        tracked = gateway.track_stream_task(
            response,
            task_id="midstream-failure",
            route="fast_chat",
            prompt="hello",
            model="local-fast",
        )
        return b"".join([chunk async for chunk in tracked.body_iterator])

    body = asyncio.run(scenario())
    assert b'"local_agent_stream_status":"failed"' in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert writes[-1][0][2] == "failed"


def test_upstream_openai_error_event_with_done_journals_failed(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b'data: {"error":{"message":"out of memory","type":"server_error"}}\n\n'
        yield b"data: [DONE]\n\n"

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="upstream-error",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def consume():
        return b"".join([chunk async for chunk in response.body_iterator])

    asyncio.run(consume())
    assert writes[-1][0][2] == "failed"


def test_abandoned_stream_journals_failed(monkeypatch):
    writes = []
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: writes.append((args, kwargs)))

    async def source():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    response = gateway.track_stream_task(
        StreamingResponse(source()),
        task_id="abandoned",
        route="fast_chat",
        prompt="hello",
        model="local-fast",
    )

    async def abandon():
        await anext(response.body_iterator)
        await response.body_iterator.aclose()

    asyncio.run(abandon())
    assert writes[-1][0][2] == "failed"


def test_stream_cleanup_releases_lock_even_when_response_close_fails(monkeypatch):
    class BadCloseResponse:
        def raise_for_status(self):
            return None

        def aiter_bytes(self):
            async def chunks():
                yield b"data: [DONE]\n\n"

            return chunks()

        async def aclose(self):
            raise RuntimeError("close failed")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return BadCloseResponse()

        async def aclose(self):
            raise RuntimeError("client close failed")

    monkeypatch.setattr(gateway.httpx, "AsyncClient", FakeClient)

    async def scenario():
        lock = asyncio.Lock()
        response = await gateway.preopen_local_stream(
            url="http://127.0.0.1:1/v1/chat/completions",
            payload={"stream": True},
            model_lock=lock,
            headers={},
            route="fast_chat",
            model="local-fast",
            started_at=time.perf_counter(),
        )
        body = b"".join([chunk async for chunk in response.body_iterator])
        assert body.endswith(b"data: [DONE]\n\n")
        assert not lock.locked()

    asyncio.run(scenario())


def test_successful_direct_codex_execution_does_not_create_unused_bundle(tmp_path, monkeypatch):
    real_route_request = gateway.route_request

    def approved_route(request, planning, **kwargs):
        return real_route_request(
            request,
            planning,
            capabilities=gateway.assumed_capabilities(),
            permissions=gateway.PermissionSnapshot(codex_cloud_approved=True),
            fast_model=gateway.FAST_MODEL,
            strong_model=gateway.STRONG_MODEL,
            agent_model=gateway.AGENT_MODEL,
            codex_model=gateway.CODEX_MODEL,
        )

    async def successful_agent(*args, **kwargs):
        return "Codex review completed with concrete findings."

    monkeypatch.setattr(gateway, "route_request", approved_route)
    monkeypatch.setattr(gateway, "execute_agent", successful_agent)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway,
        "create_codex_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("success must not create a handoff")),
    )

    response = asyncio.run(
        gateway.chat(
            ChatRequest(
                messages=[
                    {
                        "role": "user",
                        "content": f"Project: {tmp_path}; security review repository",
                    }
                ]
            )
        )
    )

    assert response.status_code == 200
    assert b'"local_agent_route":"codex"' in response.body


def test_local_model_failure_uses_openai_error_contract(monkeypatch):
    async def failed_chat(*args, **kwargs):
        raise httpx.ConnectError("model listener unavailable")

    monkeypatch.setattr(gateway, "local_chat", failed_chat)
    monkeypatch.setattr(gateway, "routing_capability_snapshot", gateway.assumed_capabilities)
    monkeypatch.setattr(gateway, "save_task", lambda *args, **kwargs: None)

    response = asyncio.run(
        gateway.chat(ChatRequest(messages=[{"role": "user", "content": "Hello"}], stream=True))
    )

    assert response.status_code == 502
    assert response.headers["X-Local-Agent-Route"] == "fast_chat"
    assert b'"code":"executor.local_model_failure"' in response.body
    assert b'"type":"local_agent_error"' in response.body


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree behavior")
def test_run_process_timeout_terminates_descendants_before_return(tmp_path):
    script = (
        "import subprocess; "
        "subprocess.Popen(['powershell.exe','-NoProfile','-Command','Start-Sleep -Seconds 5']).wait()"
    )
    started = time.perf_counter()

    with pytest.raises(subprocess.TimeoutExpired):
        gateway.run_process([sys.executable, "-c", script], str(tmp_path), timeout=0.2)

    assert time.perf_counter() - started < 3.0
