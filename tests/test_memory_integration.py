import json

from services.contracts import ExecutorName, RouteDecisionV1, RouteName
from services.gateway.app import openai_error_response, openai_response
from services.memory import MemoryRecordType, RetrievalItemV1, RetrievalResultV1
from services.memory import integration
from services.orchestration.handoff import ensure_codex_handoff
from services.orchestration.normalizer import normalize_messages
from services.orchestration.planner import (
    conservative_token_upper_bound,
    plan_request,
    render_plan_execution_context,
)


def normalized(text: str, project: str | None = None, *, request_id: str = "memory-test"):
    return normalize_messages(
        [{"role": "user", "content": text}],
        default_project=project,
        request_id=request_id,
    )


def retrieved(*, content="Use `uv run pytest`.", record_id: str = "memory-1") -> RetrievalResultV1:
    return RetrievalResultV1(
        items=[
            RetrievalItemV1(
                record_id=record_id,
                record_type=MemoryRecordType.PROJECT_KNOWLEDGE,
                subject="test command",
                value=content,
                score=0.92,
                why="subject and query terms matched",
                source_refs=["file:README.md#tests"],
            )
        ],
        used_chars=len(str(content)),
        max_chars=1_200,
    )


def test_fast_path_skips_memory_store_construction(monkeypatch):
    request = normalized("What is two plus two?")
    planning = plan_request(request)
    assert planning.plan is None

    def unexpected_store():
        raise AssertionError("fast path must not construct MemoryStore")

    monkeypatch.setattr(integration, "MemoryStore", unexpected_store)

    assert integration.attach_memory_to_planning(request, planning, RouteName.FAST_CHAT) is planning


def test_degraded_retrieval_returns_original_planning(tmp_path, monkeypatch):
    request = normalized("Read README.md and report its title.", str(tmp_path))
    planning = plan_request(request)
    assert planning.plan is not None

    class DegradedStore:
        def retrieve_safe(self, **kwargs):
            return RetrievalResultV1(
                items=[],
                used_chars=0,
                max_chars=kwargs["max_chars"],
                degraded=True,
                diagnostic="memory unavailable",
            )

    monkeypatch.setattr(integration, "MemoryStore", DegradedStore)

    assert integration.attach_memory_to_planning(request, planning, RouteName.LOCAL_CODE) is planning


def test_local_code_attaches_bounded_confirmed_content_and_provenance(tmp_path, monkeypatch):
    request = normalized("Read README.md and report its title.", str(tmp_path))
    planning = plan_request(request)
    assert planning.plan is not None
    calls = []

    class LocalStore:
        def retrieve_safe(self, **kwargs):
            calls.append(kwargs)
            return retrieved()

    monkeypatch.setattr(integration, "MemoryStore", LocalStore)
    monkeypatch.setattr(integration, "current_commit_sha", lambda project: "a" * 40)
    original_size = conservative_token_upper_bound(render_plan_execution_context(planning.plan))

    enriched = integration.attach_memory_to_planning(request, planning, RouteName.LOCAL_CODE)

    assert enriched is not planning
    assert enriched.plan is not None
    assert enriched.plan.memory_record_refs == ["memory-1"]
    assert len(enriched.plan.memory_context) == 1
    assert enriched.plan.memory_context[0].status == "confirmed"
    assert calls[0]["owner_id"] == "local-user"
    assert calls[0]["project_path"] == str(tmp_path.resolve())
    assert calls[0]["task_id"] == request.request_id
    assert calls[0]["max_records"] == 6
    assert calls[0]["max_chars"] == 1_200
    assert calls[0]["current_commit_sha"] == "a" * 40
    rendered = render_plan_execution_context(enriched.plan)
    assert conservative_token_upper_bound(rendered) > original_size
    assert "UNTRUSTED RETRIEVED EVIDENCE" in rendered
    assert "Use `uv run pytest`." in rendered
    assert "file:README.md#tests" in rendered


def test_codex_gets_record_refs_only_and_handoff_never_gets_content(tmp_path, monkeypatch):
    request = normalized(
        f"Project: {tmp_path}\nPerform a security review of the repository. Do not modify files.",
        request_id="codex-memory-test",
    )
    planning = plan_request(request)
    assert planning.plan is not None

    class CodexStore:
        def retrieve_safe(self, **kwargs):
            return retrieved(content="MEMORY_CONTENT_MUST_NOT_CROSS_BOUNDARY", record_id="memory-codex-1")

    monkeypatch.setattr(integration, "MemoryStore", CodexStore)
    enriched = integration.attach_memory_to_planning(request, planning, RouteName.CODEX)

    assert enriched.plan is not None
    assert enriched.plan.memory_record_refs == ["memory-codex-1"]
    assert len(enriched.plan.memory_context) == 1
    assert enriched.plan.memory_context[0].disclosure == "reference_only"
    assert enriched.plan.memory_context[0].content is None
    assert enriched.plan.memory_context[0].why

    decision = RouteDecisionV1(
        request_id=request.request_id,
        route=RouteName.CODEX,
        executor=ExecutorName.CODEX_BUNDLE,
        model=None,
        profile=None,
        reason_codes=["fallback.codex_bundle"],
        risk=enriched.plan.risk,
        fallback=None,
        project=request.project_hint,
        required_locks=[],
    )
    bundle = ensure_codex_handoff(
        inbox_dir=tmp_path / "inbox",
        task_id=request.request_id,
        plan=enriched.plan,
        decision=decision,
        project=request.project_hint,
        worktree=request.project_hint,
        errors=[],
        modified_files=[],
        command_summaries=[],
        artifact_refs=[],
    )
    body = bundle.read_text(encoding="utf-8")
    assert "memory-codex-1" in body
    assert "MEMORY_CONTENT_MUST_NOT_CROSS_BOUNDARY" not in body


def test_openai_response_explains_retrieval_without_repeating_content(tmp_path, monkeypatch):
    request = normalized("Read README.md and report its title.", str(tmp_path))
    planning = plan_request(request)

    class LocalStore:
        def retrieve_safe(self, **kwargs):
            return retrieved(content="PRIVATE_LOCAL_MEMORY")

    monkeypatch.setattr(integration, "MemoryStore", LocalStore)
    enriched = integration.attach_memory_to_planning(request, planning, RouteName.LOCAL_CODE)
    response = openai_response("done", "local_code", plan=enriched.plan)
    payload = json.loads(response.body)

    assert payload["local_agent_memory"]["record_refs"] == ["memory-1"]
    assert payload["local_agent_memory"]["items"][0]["why"]
    assert "PRIVATE_LOCAL_MEMORY" not in json.dumps(payload)
    assert response.headers["X-Local-Agent-Memory-Count"] == "1"


def test_openai_error_explains_retrieval_without_repeating_content(tmp_path, monkeypatch):
    request = normalized("Read README.md and report its title.", str(tmp_path))
    planning = plan_request(request)

    class LocalStore:
        def retrieve_safe(self, **kwargs):
            return retrieved(content="PRIVATE_LOCAL_MEMORY")

    monkeypatch.setattr(integration, "MemoryStore", LocalStore)
    enriched = integration.attach_memory_to_planning(request, planning, RouteName.LOCAL_CODE)
    response = openai_error_response(
        message="bounded local failure",
        code="failure.local_attempt_limit",
        route="codex_bundle",
        request_id=request.request_id,
        status_code=502,
        plan=enriched.plan,
    )
    payload = json.loads(response.body)

    assert payload["local_agent_memory"]["record_refs"] == ["memory-1"]
    assert payload["local_agent_memory"]["items"][0]["why"]
    assert "PRIVATE_LOCAL_MEMORY" not in json.dumps(payload)
    assert response.headers["X-Local-Agent-Memory-Count"] == "1"
