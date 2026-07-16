import asyncio
from pathlib import Path

import pytest

from services.gateway import app as gateway
from services.gateway.app import (
    ChatRequest,
    classify,
    extract_project,
    get_worktree_lock,
    is_read_only,
    is_review_request,
    openai_response,
    strip_ambient_tool_catalog,
)


def test_regular_chat_uses_fast_model():
    assert classify("What is dependency injection?") == "fast_chat"


def test_deep_non_code_question_uses_strong_local_model():
    assert classify("Проанализируй подробно сложные trade-offs этой бизнес-стратегии") == "strong_chat"


def test_simple_code_uses_local_agent():
    assert classify("Проект: C:\\work\\app\nПрочитай README.md и запусти тесты") == "local_code"


def test_read_only_repository_task_uses_local_coding_agent():
    assert classify("Проект: C:\\work\\app\nНайди README.md, прочитай и перечисли команды. Ничего не изменяй.") == "local_code"


def test_file_creation_is_not_misread_as_read_only_due_to_a_scoped_constraint():
    prompt = "Проект: C:\\work\\app\nСоздай result.txt. Не изменяй README.md и ничего не коммить."
    assert classify(prompt) == "local_code"
    assert not is_read_only(prompt)


def test_requested_mutation_overrides_scoped_do_not_modify_constraint():
    prompt = "Project: C:\\work\\app\nCreate result.txt; do not modify files other than result.txt."
    assert not is_read_only(prompt)


def test_negated_mutations_keep_security_review_read_only():
    prompt = "Проведи security review. Ничего не изменяй и не создавай коммиты."
    assert is_read_only(prompt)


def test_super_complex_programming_goes_to_cloud_codex():
    assert classify("Проект: C:\\work\\app\nПроведи security review перед merge") == "codex"


def test_repository_review_without_explicit_project_uses_default_codex_worktree():
    prompt = "Review current changes before merge"
    assert classify(prompt) == "codex"
    assert is_review_request(prompt)
    assert is_read_only(prompt)


def test_security_fix_uses_writable_codex_exec_not_read_only_review():
    prompt = "Project: C:\\work\\app\nFix the security vulnerability and run tests"
    assert classify(prompt) == "codex"
    assert not is_review_request(prompt)
    assert not is_read_only(prompt)


def test_review_and_fix_is_a_writable_codex_task():
    prompt = "Review current changes before merge, fix every finding, and run tests"
    assert classify(prompt) == "codex"
    assert not is_review_request(prompt)
    assert not is_read_only(prompt)


def test_ascii_mutation_markers_require_real_word_boundaries():
    assert is_read_only("Review prefix parsing before merge")
    assert is_read_only("Audit the credit module code")
    assert is_read_only("Review the editor implementation before merge")


def test_equivalent_project_paths_share_a_worktree_lock(tmp_path):
    direct = get_worktree_lock(str(tmp_path))
    equivalent = get_worktree_lock(str(tmp_path / "."))
    assert direct is equivalent


def test_complex_non_code_does_not_spend_codex():
    assert classify("Объясни современную security policy компании") == "fast_chat"


def test_ordinary_word_containing_file_does_not_launch_coding_agent():
    assert classify("Compare these customer profiles") == "fast_chat"


def test_general_programming_question_without_repository_action_stays_chat():
    assert classify("Explain Python decorators with a short example") == "fast_chat"


def test_plural_repository_actions_use_local_coding_agent():
    assert classify("Run tests") == "local_code"
    assert classify("Fix failing tests") == "local_code"
    assert classify("Read files and list tests") == "local_code"


def test_repository_review_wording_is_not_misclassified_as_chat():
    prompt = "Review the code in this repository. Do not make changes."
    assert classify(prompt) == "codex"
    assert is_read_only(prompt)


def test_programming_intent_wins_over_names_of_optional_modules():
    assert classify("Fix documentation build") == "local_code"
    assert classify("Implement create image endpoint") == "local_code"
    assert classify("Fix browser test using https://example.com") == "local_code"


def test_non_programming_project_plan_stays_chat_and_system_design_uses_strong_chat():
    assert classify("Create a project plan for the marketing launch") == "fast_chat"
    assert classify("Design a distributed event-processing system in Rust") == "strong_chat"


def test_project_parser_handles_quoted_newline_and_semicolon_paths(tmp_path, monkeypatch):
    project = tmp_path / "repo with spaces"
    project.mkdir()
    monkeypatch.setattr(gateway, "DEFAULT_PROJECT", str(tmp_path))
    expected = str(project.resolve())
    assert extract_project(f'Project: "{project}"\nRun tests') == expected
    assert extract_project(f"Project: {project}\nRun tests") == expected
    assert extract_project(f"Project: {project}; run tests") == expected


def test_explicit_invalid_project_never_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "DEFAULT_PROJECT", str(tmp_path))
    assert extract_project(r"Project: C:\does-not-exist; run tests") is None


def test_browser_route():
    assert classify("Открой https://example.com через browser") == "browser"


def test_image_route():
    assert classify("Сгенерируй изображение синего робота") == "image"


def test_docs_route():
    assert classify("Найди в Context7 документацию FastAPI lifespan") == "docs"


def test_open_webui_auxiliary_prompts_are_never_actions():
    prompts = [
        """### Task:
Suggest 3-5 relevant follow-up questions based on this chat history.
<chat_history>USER: Generate image of a robot</chat_history>""",
        "Generate a concise title for this chat: Context7 security review before merge",
        "Create 1-3 broad tags for: open https://example.com with Playwright",
        "Generate search queries for: create image and edit C:\\work\\app",
    ]
    assert all(classify(prompt) == "auxiliary" for prompt in prompts)


def test_qwen_multiline_prompt_preserves_exact_literal_structure(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gateway,
        "normalize_task_for_agent",
        lambda prompt: (
            "Project: C:\\work\\app\nFind README.md and list the commands.\nDo not modify files.",
            "Return the final answer in Russian.",
        ),
    )

    def fake_run_process(command, project, timeout, input_text=None, **kwargs):
        captured["command"] = command
        captured["project"] = project
        captured["input_text"] = input_text
        captured["env_overrides"] = kwargs.get("env_overrides")
        return "README.md commands listed"

    monkeypatch.setattr(gateway, "run_process", fake_run_process)
    gateway.run_qwen_agent("многострочная задача", "C:\\work\\app", model="local-fast")

    command = captured["command"]
    prompt = captured["input_text"]
    assert "Project: C:\\work\\app\nFind README.md and list the commands.\nDo not modify files." in prompt
    assert "C:\\work\\app" in prompt
    assert "Find README.md and list the commands." in prompt
    assert command[command.index("--model") + 1] == "local-fast"
    assert command[command.index("--prompt") + 1] == ""
    assert captured["env_overrides"]["QWEN_HOME"].endswith("run\\qwen-homes\\qwen-code")


def test_openai_request_keeps_tool_fields():
    request = ChatRequest(
        messages=[{"role": "user", "content": "Use the tool"}],
        tools=[{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}],
        tool_choice="auto",
        parallel_tool_calls=True,
        stream_options={"include_usage": True},
        top_p=0.8,
        stop=["END"],
        seed=42,
    )
    payload = request.model_dump(exclude_none=True)
    assert payload["tools"][0]["function"]["name"] == "ping"
    assert payload["tool_choice"] == "auto"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["top_p"] == 0.8
    assert payload["stop"] == ["END"]
    assert payload["seed"] == 42


def test_large_automatic_webui_tool_catalog_is_removed_but_explicit_tools_are_preserved():
    ambient = {"tools": [{"type": "function", "function": {"name": f"tool_{i}"}} for i in range(34)]}
    assert strip_ambient_tool_catalog(ambient, "fast_chat") == 34
    assert "tools" not in ambient

    explicit = {
        "tools": [{"type": "function", "function": {"name": f"tool_{i}"}} for i in range(34)],
        "tool_choice": {"type": "function", "function": {"name": "tool_0"}},
    }
    assert strip_ambient_tool_catalog(explicit, "fast_chat") == 0
    assert len(explicit["tools"]) == 34


def test_synthetic_stream_preserves_tool_calls_and_null_content():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "ping", "arguments": "{}"},
        }
    ]
    response = openai_response(
        None,
        "fast_chat",
        stream=True,
        message={"role": "assistant", "content": None, "tool_calls": tool_calls},
    )

    async def collect() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(collect()).decode("utf-8")
    assert '"tool_calls"' in body
    assert '"index": 0' in body
    assert '"finish_reason": "tool_calls"' in body
    assert "data: [DONE]" in body


def test_synthetic_stream_preserves_upstream_finish_reason():
    response = openai_response("partial", "fast_chat", stream=True, finish_reason="length")

    async def collect() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(collect()).decode("utf-8")
    assert '"finish_reason": "length"' in body


def test_cloud_review_uses_specialized_codex_review(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_process(command, project, timeout, prefer_stdout=False, input_text=None):
        captured["command"] = command
        captured["prefer_stdout"] = prefer_stdout
        captured["input_text"] = input_text
        return "[P1] concrete finding - app.py:10"

    monkeypatch.setattr(gateway, "run_process", fake_run_process)
    result = gateway.run_codex_agent(
        "Проведи security review перед merge. Ничего не изменяй.",
        "C:\\work\\app",
        cloud=True,
        mode="review",
    )
    command = captured["command"]
    assert "review" in command
    assert "exec" not in command
    assert command[command.index("-m") + 1] == "gpt-5.6-sol"
    assert captured["prefer_stdout"] is True
    assert command[-1] == "-"
    assert "security review" in captured["input_text"]
    assert result.startswith("[P1]")


def test_codex_failure_removes_partial_last_message_file(tmp_path, monkeypatch):
    def failed_process(command, project, timeout, **kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("private partial task output", encoding="utf-8")
        raise RuntimeError("codex failed")

    monkeypatch.setattr(gateway.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(gateway, "run_process", failed_process)

    with pytest.raises(RuntimeError, match="codex failed"):
        gateway.run_codex_agent("fix tests", str(tmp_path), cloud=True)

    assert list(tmp_path.glob("local-agent-*.txt")) == []
