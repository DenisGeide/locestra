import time

import pytest

from services.gateway.app import ChatRequest, normalize_request, route_preview
from services.orchestration.planner import plan_request
from services.orchestration.router import (
    CapabilitySnapshot,
    FailureHistory,
    assumed_capabilities,
    route_request,
)
from services.contracts import AvailabilityStatus, NormalizedRequestV1


# Fixed Stage 002 corpus. Accuracy describes only this versioned regression set.
ROUTING_CASES = [
    ("en-fast-hello", "Hello", "fast_chat", "none", "low", "ready"),
    ("ru-fast-hello", "Привет", "fast_chat", "none", "low", "ready"),
    ("en-fast-teaching", "Explain Python decorators", "fast_chat", "none", "low", "ready"),
    ("ru-fast-teaching", "Объясни dependency injection", "fast_chat", "none", "low", "ready"),
    ("en-strong-analysis", "Deep analysis of business trade-offs", "strong_chat", "none", "low", "ready"),
    ("ru-strong-analysis", "Проанализируй подробно сложные компромиссы бизнес-стратегии", "strong_chat", "none", "low", "ready"),
    ("en-security-policy", "Explain our company security policy", "fast_chat", "none", "low", "ready"),
    ("ru-security-policy", "Объясни политику безопасности компании", "fast_chat", "none", "low", "ready"),
    ("en-system-design", "Design a distributed system in Rust", "strong_chat", "none", "medium", "ready"),
    ("ru-system-design", "Спроектируй распределённую систему на Rust", "strong_chat", "none", "medium", "ready"),
    ("en-run-singular", "Run test", "local_code", "write", "medium", "ready"),
    ("en-run-plural", "Run tests", "local_code", "write", "medium", "ready"),
    ("en-read-singular", "Read file and list test", "local_code", "read_only", "medium", "ready"),
    ("en-read-plural", "Read files and list tests", "local_code", "read_only", "medium", "ready"),
    ("en-inspect-repository", "Inspect repository", "local_code", "read_only", "medium", "ready"),
    ("en-inspect-repositories", "Inspect repositories", "local_code", "read_only", "medium", "ready"),
    ("ru-inspect-repository", "Проверь репозиторий и перечисли файлы", "local_code", "read_only", "medium", "ready"),
    ("project-read", "Project: {project}; read README.md; do not change files", "local_code", "read_only", "medium", "ready"),
    ("en-fix-singular", "Fix failing test", "local_code", "write", "medium", "ready"),
    ("en-fix-plural", "Fix failing tests", "local_code", "write", "medium", "ready"),
    ("ru-fix-tests", "Исправь падающие тесты", "local_code", "write", "medium", "ready"),
    ("en-update-dependency", "Update dependency", "local_code", "write", "medium", "ready"),
    ("en-update-dependencies", "Update dependencies", "local_code", "write", "medium", "ready"),
    ("scoped-constraint", "Create result.txt; do not modify README.md", "local_code", "write", "medium", "ready"),
    ("collision-docs", "Fix documentation build", "local_code", "write", "medium", "ready"),
    ("collision-image", "Implement create image endpoint", "local_code", "write", "medium", "ready"),
    ("collision-browser", "Fix browser test using https://example.com", "local_code", "write", "medium", "ready"),
    ("collision-docs-read", "Project: {project}; find documentation build config", "local_code", "read_only", "medium", "ready"),
    ("collision-browser-read", "Project: {project}; inspect browser test at https://example.com", "local_code", "read_only", "medium", "ready"),
    ("collision-comfy-read", "Project: {project}; inspect ComfyUI workflow file", "local_code", "read_only", "medium", "ready"),
    ("non-code-project", "Project: {project}; create a marketing launch plan", "fast_chat", "none", "low", "ready"),
    ("incidental-path", r"How do I escape C:\Temp in Markdown?", "fast_chat", "none", "low", "ready"),
    ("review-current", "Review current changes before merge", "codex", "read_only", "high", "blocked"),
    ("review-repository", "Review repository; do not change files", "codex", "read_only", "high", "blocked"),
    ("review-and-fix", "Review code and fix every finding", "codex", "write", "high", "blocked"),
    ("ru-review-only", "Проведи ревью кода, ничего не изменяй", "codex", "read_only", "high", "blocked"),
    ("ru-review-fix", "Проведи ревью кода и исправь ошибки", "codex", "write", "high", "blocked"),
    ("security-review", "Security review of repository", "codex", "read_only", "high", "blocked"),
    ("security-fix", "Project: {project}; fix security vulnerability", "codex", "write", "high", "blocked"),
    ("migration", "Project: {project}; migrate schema and application", "codex", "write", "high", "blocked"),
    ("concurrency", "Project: {project}; fix concurrency race condition", "codex", "write", "high", "blocked"),
    ("production-refactor", "Large production refactor across services", "codex", "write", "high", "blocked"),
    ("policy-review-not-code", "Give a security review of company policy", "fast_chat", "none", "low", "ready"),
    ("review-api", "Review this API endpoint", "codex", "read_only", "high", "blocked"),
    ("educational-create", "How do I create a file in Python?", "fast_chat", "none", "low", "ready"),
    ("educational-commit", "What does git commit do?", "fast_chat", "none", "low", "ready"),
    ("educational-update", "Should I update dependencies?", "fast_chat", "none", "low", "ready"),
    ("educational-fix", "Explain how to fix this test", "fast_chat", "none", "low", "ready"),
    ("boundary-thread", "Explain thread files", "fast_chat", "none", "low", "ready"),
    ("boundary-blacklist", "Explain blacklist files", "fast_chat", "none", "low", "ready"),
    ("ru-boundary-codex", "Прочитай Кодекс поведения", "fast_chat", "none", "low", "ready"),
    ("ru-boundary-test", "Прочитай рецепт теста", "fast_chat", "none", "low", "ready"),
    ("ru-mention-api", "Объясни реализацию API", "fast_chat", "none", "low", "ready"),
    ("ru-mention-history", "Покажи историю изменений файла", "fast_chat", "none", "low", "ready"),
    ("negated-list", "Review code. Do not modify, create, or delete files.", "codex", "read_only", "high", "blocked"),
    ("negated-long", "Review code. Do not under any circumstances ever modify files.", "codex", "read_only", "high", "blocked"),
    ("ru-post-negation", "Проведи ревью кода; исправления не вноси", "codex", "read_only", "high", "blocked"),
    ("docs-context7", "Find Context7 docs for FastAPI lifespan", "docs", "read_only", "low", "ready"),
    ("docs-official", "Find official FastAPI documentation", "docs", "read_only", "low", "ready"),
    ("browser-open", "Open https://example.com", "browser", "read_only", "high", "ready"),
    ("browser-summarize", "Summarize https://example.com", "browser", "read_only", "high", "ready"),
    ("image-en", "Generate image of a blue robot", "image", "write", "low", "ready"),
    ("image-ru", "Сгенерируй изображение синего робота", "image", "write", "low", "ready"),
    ("auxiliary-title", "Generate a concise title for this chat: browser and image", "auxiliary", "none", "low", "ready"),
    ("auxiliary-collision", "Implement generate search queries endpoint", "local_code", "write", "medium", "ready"),
    ("override-local", "/local Fix tests", "local_code", "write", "medium", "ready"),
    ("override-local-critical", "/local migrate production database", "local_code", "write", "critical", "blocked"),
    ("override-codex-review", "/codex review repository", "codex", "read_only", "high", "blocked"),
    ("override-codex-write", "/codex fix repository", "codex", "write", "medium", "blocked"),
    ("override-image", "/image blue robot", "image", "write", "low", "ready"),
    ("override-browser", "/browser https://example.com", "browser", "read_only", "high", "ready"),
    ("override-browser-private", "/browser http://127.0.0.1/admin", "browser", "read_only", "high", "blocked"),
    ("override-rejected", "/codex hello", "fast_chat", "none", "low", "ready"),
    ("plural-creates", "Creates files in repository", "local_code", "write", "medium", "ready"),
    ("gerund-updating", "Updating dependencies in repository", "local_code", "write", "medium", "ready"),
    ("daily-oauth", "Implement OAuth login", "local_code", "write", "medium", "ready"),
    ("daily-auth", "Add authentication", "local_code", "write", "medium", "ready"),
    ("daily-ci", "Fix CI", "local_code", "write", "medium", "ready"),
    ("daily-pytest", "Run pytest", "local_code", "write", "medium", "ready"),
    ("daily-npm", "Run npm install", "local_code", "write", "medium", "ready"),
    ("daily-registration", "Create user registration", "local_code", "write", "medium", "ready"),
    ("ru-auth", "Добавь авторизацию", "local_code", "write", "medium", "ready"),
    ("ru-linter", "Исправь линтер", "local_code", "write", "medium", "ready"),
    ("ru-login-page", "Создай страницу входа", "local_code", "write", "medium", "ready"),
    ("negation-contrast", "Don't explain, just fix tests", "local_code", "write", "medium", "ready"),
    ("negation-scope", "Do not modify README.md, inspect files", "local_code", "read_only", "medium", "ready"),
    ("negation-never-mind", "Never mind, fix test", "local_code", "write", "medium", "ready"),
    ("suggest-fixes", "Review code and suggest fixes", "codex", "read_only", "high", "blocked"),
    ("explain-fixes", "Review code and explain fixes", "codex", "read_only", "high", "blocked"),
    ("fixes-not-required", "Review code, fixes are not required", "codex", "read_only", "high", "blocked"),
    ("no-need-fix", "No need to fix, just review code", "codex", "read_only", "high", "blocked"),
    ("explain-then-fix", "Explain and fix the test", "local_code", "write", "medium", "ready"),
    ("identify-then-fix", "Identify repository issues and fix them", "local_code", "write", "medium", "ready"),
    ("report-then-fix", "Report findings and fix tests", "local_code", "write", "medium", "ready"),
    ("recommend-then-apply", "Recommend a fix and apply it to code", "local_code", "write", "medium", "ready"),
    ("tool-ruff", "Run ruff", "local_code", "write", "medium", "ready"),
    ("tool-mypy", "Run mypy", "local_code", "write", "medium", "ready"),
    ("tool-linter", "Run the linter", "local_code", "write", "medium", "ready"),
    ("tool-cargo", "Run cargo check", "local_code", "write", "medium", "ready"),
    ("tool-eslint", "Run eslint", "local_code", "write", "medium", "ready"),
    ("tool-precommit", "Execute pre-commit", "local_code", "write", "medium", "ready"),
    ("tool-format", "Check formatting", "local_code", "write", "medium", "ready"),
    ("diagnose-unknown", "Investigate an unknown error in the repository", "codex", "read_only", "high", "blocked"),
    ("diagnose-intermittent", "Diagnose an intermittent test failure", "codex", "read_only", "high", "blocked"),
    ("diagnose-cross", "Root cause across services", "codex", "read_only", "high", "blocked"),
    ("aux-collision-title", "Implement an endpoint that asks models to generate a concise title for this chat", "local_code", "write", "medium", "ready"),
    ("aux-collision-fix", "Fix code that says generate a concise title for this chat", "local_code", "write", "medium", "ready"),
    ("permission-ceiling-review", "Review the code and fix every issue. Do not modify any files.", "codex", "read_only", "high", "blocked"),
    ("permission-ceiling-fix", "Fix the failing tests, but make no changes.", "local_code", "read_only", "medium", "blocked"),
    ("repo-analyze-architecture", "Project: {project}; analyze the project architecture and identify issues. Do not modify files.", "local_code", "read_only", "medium", "ready"),
    ("repo-understand-auth", "Project: {project}; understand how authentication works. Do not modify files.", "local_code", "read_only", "medium", "ready"),
    ("repo-explain-auth", "Explain how authentication works in this project. Do not modify files.", "local_code", "read_only", "medium", "ready"),
    ("ru-repo-analyze", "Project: {project}; проанализируй архитектуру проекта, ничего не изменяй.", "local_code", "read_only", "medium", "ready"),
    ("general-explain-api", "Explain API pagination", "fast_chat", "none", "low", "ready"),
    ("general-analyze-oauth", "Analyze OAuth flows", "fast_chat", "none", "low", "ready"),
    ("general-understand-dockerfile", "Understand Dockerfile syntax", "fast_chat", "none", "low", "ready"),
    ("general-explain-index", "Explain what a database index is", "fast_chat", "none", "low", "ready"),
]


def decide(prompt, tmp_path, *, messages=None, capabilities=None):
    project_prompt = prompt.format(project=tmp_path)
    request = ChatRequest(messages=messages or [{"role": "user", "content": project_prompt}])
    normalized = normalize_request(request, request_id="eval-request")
    planning = plan_request(normalized)
    decision = route_request(
        normalized,
        planning,
        capabilities=capabilities or assumed_capabilities(),
        fast_model="local-fast",
        strong_model="local-strong",
        agent_model="local-strong",
        codex_model="codex",
    )
    return normalized, planning, decision


def test_fixed_routing_corpus_has_at_least_fifty_cases_and_full_accuracy(tmp_path):
    failures = []
    for case_id, prompt, route, mode, risk, status in ROUTING_CASES:
        _, _, decision = decide(prompt, tmp_path)
        actual = (
            decision.route.value,
            decision.execution_mode.value,
            decision.risk.value,
            decision.decision_status.value,
        )
        expected = (route, mode, risk, status)
        if actual != expected:
            failures.append((case_id, expected, actual, decision.reason_codes))

    assert len(ROUTING_CASES) >= 50
    assert not failures, failures
    accuracy = (len(ROUTING_CASES) - len(failures)) / len(ROUTING_CASES)
    assert accuracy == 1.0


def test_audio_and_image_attachments_route_without_embedding_payload(tmp_path):
    audio_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribe this"},
                {"type": "input_audio", "input_audio": {"data": "PRIVATE_AUDIO", "format": "wav"}},
            ],
        }
    ]
    normalized, _, audio = decide("ignored", tmp_path, messages=audio_messages)
    assert audio.route == "voice"
    assert audio.executor == "whisper"
    assert audio.decision_status == "ready"
    assert "PRIVATE_AUDIO" not in normalized.model_dump_json()

    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE"}},
            ],
        }
    ]
    normalized, _, vision = decide("ignored", tmp_path, messages=image_messages)
    assert vision.route == "vision"
    assert vision.decision_status == "degraded"
    assert "PRIVATE_IMAGE" not in normalized.model_dump_json()


def test_historical_attachment_does_not_hijack_the_current_user_turn(tmp_path):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD_IMAGE"}},
            ],
        },
        {"role": "assistant", "content": "Previous image handled."},
        {"role": "user", "content": "Thanks. Explain Python decorators."},
    ]

    normalized, _, decision = decide("ignored", tmp_path, messages=messages)

    assert normalized.attachments == []
    assert decision.route == "fast_chat"


def test_historical_audio_is_not_replayed_by_current_turn():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribe"},
                {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
            ],
        },
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "Now explain the result"},
    ]

    with pytest.raises(ValueError, match="missing"):
        from services.gateway.app import inline_audio_payload

        inline_audio_payload(messages)


def test_cross_module_override_keeps_plan_and_decision_aligned(tmp_path):
    _, image_plan, image_decision = decide(
        "/image Find official FastAPI documentation",
        tmp_path,
    )
    assert image_plan.plan is not None
    assert image_plan.plan.action == "image"
    assert image_plan.plan.tools == ["comfyui"]
    assert image_decision.route == "image"
    assert image_decision.action == "image"

    audio_history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "/image blue robot"},
                {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
            ],
        }
    ]
    _, audio_plan, audio_decision = decide("ignored", tmp_path, messages=audio_history)
    assert audio_plan.plan is not None
    assert audio_plan.plan.action == "image"
    assert audio_plan.plan.tools == ["comfyui"]
    assert audio_decision.route == "image"

    image_history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "/voice transcribe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    _, voice_plan, voice_decision = decide("ignored", tmp_path, messages=image_history)
    assert voice_plan.plan is not None
    assert voice_plan.plan.action == "voice"
    assert voice_plan.plan.tools == ["whisper"]
    assert voice_decision.route == "voice"
    assert voice_decision.decision_status == "blocked"
    assert "voice.attachment_missing" in voice_decision.blocking_reason_codes


def test_image_first_message_still_parses_leading_override(tmp_path):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "text", "text": "/image create a variation"},
            ],
        }
    ]

    normalized, planning, decision = decide("ignored", tmp_path, messages=messages)

    assert normalized.routing_override == "image"
    assert normalized.user_message.startswith("create a variation")
    assert planning.plan is not None and planning.plan.action == "image"
    assert decision.route == "image"


def test_external_audio_url_is_not_advertised_as_executable_voice(tmp_path):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "/voice transcribe"},
                {"type": "audio_url", "audio_url": {"url": "https://example.com/private.wav"}},
            ],
        }
    ]

    normalized, _, decision = decide("ignored", tmp_path, messages=messages)

    assert normalized.attachments == []
    assert decision.route == "voice"
    assert decision.decision_status == "blocked"
    assert "voice.attachment_missing" in decision.blocking_reason_codes


@pytest.mark.parametrize(
    ("prompt", "override", "cleaned"),
    [
        ("/local Fix tests", "local", "Fix tests"),
        ("  /codex review repository", "codex", "review repository"),
        ("/image blue robot", "image", "blue robot"),
        ("/locality is a word", None, "/locality is a word"),
    ],
)
def test_override_prefix_boundary_and_stripping(tmp_path, prompt, override, cleaned):
    normalized, _, _ = decide(prompt, tmp_path)
    assert normalized.routing_override == override
    assert normalized.user_message == cleaned


def test_conflicting_override_is_blocked(tmp_path):
    normalized, _, decision = decide("/local /codex Fix tests", tmp_path)
    assert normalized.override_conflict is True
    assert decision.decision_status == "blocked"
    assert "override.conflict" in decision.blocking_reason_codes


def test_invalid_explicit_project_never_uses_default(tmp_path):
    normalized, _, decision = decide(r"Project: C:\does-not-exist; run tests", tmp_path)
    assert normalized.project_hint is None
    assert normalized.project_resolution.status == "invalid"
    assert decision.route == "local_code"
    assert decision.decision_status == "blocked"
    assert "project.explicit_invalid" in decision.blocking_reason_codes


@pytest.mark.parametrize(
    "path",
    [r"C:/does-not-exist/repo", r"\\server\missing\repo", r"/mnt/z/does-not-exist/repo"],
)
def test_embedded_invalid_absolute_paths_never_fall_back_or_probe_network(tmp_path, path):
    started = time.perf_counter()
    normalized, _, decision = decide(f"Fix tests in {path}", tmp_path)

    assert time.perf_counter() - started < 0.5
    assert normalized.project_hint is None
    assert normalized.project_resolution.status == "invalid"
    assert decision.decision_status == "blocked"
    assert "project.explicit_invalid" in decision.blocking_reason_codes


def test_project_path_words_do_not_contaminate_intent(tmp_path):
    project = tmp_path / "code-security-production-database-pytest"
    project.mkdir()
    normalized, planning, decision = decide(
        f"Project: {project}; create a marketing launch plan",
        tmp_path,
    )

    assert normalized.project_hint == str(project.resolve())
    assert planning.signals.repository_action is False
    assert decision.route == "fast_chat"
    assert decision.project is None


def test_capability_snapshot_is_an_injected_deterministic_signal(tmp_path):
    base = assumed_capabilities()
    statuses = dict(base.statuses)
    statuses["browser"] = AvailabilityStatus.UNAVAILABLE
    unavailable = CapabilitySnapshot(statuses=statuses, checked_at=base.checked_at)
    _, _, decision = decide("Open https://example.com", tmp_path, capabilities=unavailable)

    assert decision.route == "browser"
    assert decision.executor == "degraded_response"
    assert decision.decision_status == "degraded"
    assert "capability.browser.unavailable" in decision.reason_codes


def test_same_request_policy_and_health_snapshot_produce_identical_decision(tmp_path):
    prompt = f"Project: {tmp_path}; fix failing tests"
    request = ChatRequest(messages=[{"role": "user", "content": prompt}])
    normalized = normalize_request(request, request_id="stable-request")
    planning = plan_request(normalized)
    snapshot = assumed_capabilities()

    first = route_request(normalized, planning, capabilities=snapshot)
    second = route_request(normalized, planning, capabilities=snapshot)

    assert first == second


def test_local_failure_history_does_not_escalate_unrelated_chat(tmp_path):
    normalized, planning, _ = decide("Hello", tmp_path)
    decision = route_request(
        normalized,
        planning,
        capabilities=assumed_capabilities(),
        failures=FailureHistory(local_code_failures=2),
    )

    assert decision.route == "fast_chat"
    assert decision.decision_status == "ready"
    assert "failure.local_attempt_limit" not in decision.reason_codes


def test_legacy_normalized_request_without_resolution_still_routes_resolved_project(tmp_path):
    current, _, _ = decide(f"Project: {tmp_path}; fix tests", tmp_path)
    payload = current.model_dump()
    payload.pop("project_resolution")
    legacy = NormalizedRequestV1.model_validate(payload)
    planning = plan_request(legacy)
    decision = route_request(legacy, planning, capabilities=assumed_capabilities())

    assert legacy.project_hint == str(tmp_path.resolve())
    assert decision.route == "local_code"
    assert decision.decision_status == "ready"


def test_validation_denial_precedes_codex_bundle_fallback(tmp_path):
    normalized, planning, decision = decide(
        r"Project: C:\definitely-missing; security review repository",
        tmp_path,
    )

    assert normalized.project_resolution.status == "invalid"
    assert planning.signals.repository_action is True
    assert decision.executor == "degraded_response"
    assert decision.permission_disposition == "denied"
    assert "project.explicit_invalid" in decision.blocking_reason_codes


def test_override_conflict_precedes_codex_bundle_fallback(tmp_path):
    _, _, decision = decide("/local /codex security review repository", tmp_path)

    assert decision.executor == "degraded_response"
    assert decision.permission_disposition == "denied"
    assert "override.conflict" in decision.blocking_reason_codes


@pytest.mark.parametrize(
    "prohibition",
    [
        "do not make any changes",
        "do not make changes",
        "do not modify anything",
        "do not edit anything",
        "no changes please",
        "no edits",
        "don't touch files",
        "only report",
        "audit only",
    ],
)
def test_global_read_only_variants_never_reach_write_mode(tmp_path, prohibition):
    _, planning, decision = decide(
        f"Project: {{project}}; find and fix the bug, but {prohibition}",
        tmp_path,
    )

    assert planning.signals.permission_conflict is True
    assert decision.execution_mode == "read_only"
    assert decision.decision_status == "blocked"
    assert "permission.read_only_conflict" in decision.blocking_reason_codes


@pytest.mark.parametrize("prompt", ["update production config", "run production service"])
def test_local_override_cannot_bypass_high_risk_write_approval(tmp_path, prompt):
    natural = decide(f"Project: {{project}}; {prompt}", tmp_path)[2]
    forced = decide(f"/local Project: {{project}}; {prompt}", tmp_path)[2]

    assert natural.route == "codex"
    assert natural.permission_disposition == "approval_required"
    assert forced.route == "local_code"
    assert forced.decision_status == "blocked"
    assert forced.permission_disposition == "denied"
    assert "permission.high_risk_local_override_denied" in forced.blocking_reason_codes


def test_oversized_local_plan_fails_closed_before_executor(tmp_path):
    prompt = f"Project: {{project}}; fix the test. " + ("x" * 20_000)
    _, planning, decision = decide(prompt, tmp_path)

    assert planning.plan is not None
    assert decision.route == "local_code"
    assert decision.executor == "degraded_response"
    assert decision.decision_status == "blocked"
    assert "context.agent_input_exceeds_budget" in decision.blocking_reason_codes


def test_fast_routing_is_pure_and_warm_p95_is_below_ci_ceiling(tmp_path):
    durations = []
    for index in range(200):
        started = time.perf_counter()
        decide(f"Hello {index}", tmp_path)
        durations.append((time.perf_counter() - started) * 1000)
    p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
    assert p95 < 25.0


def test_public_route_preview_warm_p95_is_below_ci_ceiling():
    route_preview("Hello warmup")
    durations = []
    for index in range(100):
        started = time.perf_counter()
        result = route_preview(f"Hello preview {index}")
        durations.append((time.perf_counter() - started) * 1000)
        assert result["route"] == "fast_chat"
    p95 = sorted(durations)[94]
    assert p95 < 25.0


def test_maximum_contract_sized_no_match_input_is_linear_time(tmp_path):
    # Keep the payload close to the NormalizedRequestV1 message ceiling. This
    # specifically guards against unanchored lookaheads becoming O(n^2).
    prompt = "z" * 262_000
    started = time.perf_counter()
    normalized, planning, decision = decide(prompt, tmp_path)
    elapsed = time.perf_counter() - started

    assert normalized.user_message == prompt
    assert planning.signals.docs is False
    assert decision.route == "strong_chat"
    assert elapsed < 2.0
