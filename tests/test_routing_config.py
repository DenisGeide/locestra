import json

import pytest
from pydantic import ValidationError

from services.orchestration.config import ROUTING_CONFIG_PATH, load_routing_policy
from services.mcp_hub.config import load_registry, render_qwen_settings


def test_committed_routing_policy_is_versioned_bounded_and_deterministic():
    policy = load_routing_policy()

    assert policy.schema_version == "1.0"
    assert policy.policy_version == "2026-07-14.1"
    assert policy.thresholds.local_code_max_attempts == 2
    assert policy.thresholds.critical_complexity_markers == 1
    assert policy.llm_signal.enabled is False
    assert "local_code" in policy.planner_routes


def test_routing_policy_rejects_unknown_or_secret_like_fields(tmp_path):
    payload = json.loads(ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))
    payload["api_token"] = "must-never-be-accepted"
    candidate = tmp_path / "routing.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_routing_policy(candidate)


def test_routing_policy_rejects_duplicate_rules(tmp_path):
    payload = json.loads(ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))
    payload["rules"]["docs"].append(payload["rules"]["docs"][0])
    candidate = tmp_path / "routing.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="duplicates"):
        load_routing_policy(candidate)


def test_planner_routes_allowlist_controls_plan_creation(tmp_path):
    payload = json.loads(ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))
    payload["planner_routes"].remove("local_code")
    candidate = tmp_path / "routing.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="every executor route"):
        load_routing_policy(candidate)


def test_gateway_qwen_profiles_enforce_route_tool_boundaries():
    root = ROUTING_CONFIG_PATH.parents[1]
    code = json.loads((root / "config" / "qwen-code" / "settings.json").read_text(encoding="utf-8"))
    docs_base = json.loads(
        (root / "config" / "qwen-docs" / "settings.json").read_text(encoding="utf-8")
    )
    docs = render_qwen_settings("qwen-docs", load_registry())

    assert code["mcpServers"] == {}
    assert docs_base["mcpServers"] == {}
    assert set(docs["mcpServers"]) == {"context7"}
