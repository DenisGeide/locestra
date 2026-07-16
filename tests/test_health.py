from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from services.health import (
    CanonicalHealthStatus,
    CapabilityHealthV1,
    CapabilityStatus,
    HealthReportV1,
    aggregate_health,
)


NOW = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)


def capability(name: str, status: str, *, required: bool) -> CapabilityHealthV1:
    return CapabilityHealthV1(
        name=name,
        required=required,
        status=status,
        checked_at=NOW,
        detail=None,
        latency_ms=1.0,
    )


def test_all_required_capabilities_available_is_live_ready_and_ok():
    report = aggregate_health(
        "gateway",
        [
            capability("sqlite", "ok", required=True),
            capability("fast_model", "ok", required=True),
            capability("strong_model", "ok", required=True),
            capability("browser", "ok", required=False),
        ],
        checked_at=NOW,
    )

    assert report.live is True
    assert report.ready is True
    assert report.status is CanonicalHealthStatus.OK
    assert report.schema_version == "1.0"


def test_unavailable_optional_capability_degrades_without_blocking_readiness():
    report = aggregate_health(
        "gateway",
        [
            capability("sqlite", "ok", required=True),
            capability("voice", "unavailable", required=False),
        ],
        checked_at=NOW,
    )

    assert report.live is True
    assert report.ready is True
    assert report.status is CanonicalHealthStatus.DEGRADED


def test_unavailable_required_capability_blocks_readiness():
    report = aggregate_health(
        "gateway",
        [
            capability("sqlite", "ok", required=True),
            capability("fast_model", "unavailable", required=True),
        ],
        checked_at=NOW,
    )

    assert report.live is True
    assert report.ready is False
    assert report.status is CanonicalHealthStatus.UNAVAILABLE


def test_optional_on_demand_and_disabled_capabilities_are_neutral():
    report = aggregate_health(
        "gateway",
        [
            capability("sqlite", "ok", required=True),
            capability("comfyui", "on_demand", required=False),
            capability("telegram", "disabled", required=False),
        ],
        checked_at=NOW,
    )

    assert report.ready is True
    assert report.status is CanonicalHealthStatus.OK
    assert report.capabilities[1].status is CapabilityStatus.ON_DEMAND
    assert report.capabilities[2].status is CapabilityStatus.DISABLED


def test_required_degraded_capability_remains_ready_but_degraded():
    report = aggregate_health(
        "gateway",
        [capability("sqlite", "degraded", required=True)],
        checked_at=NOW,
    )

    assert report.ready is True
    assert report.status is CanonicalHealthStatus.DEGRADED


def test_non_live_service_is_never_ready():
    report = aggregate_health(
        "gateway",
        [capability("sqlite", "ok", required=True)],
        live=False,
        checked_at=NOW,
    )

    assert report.live is False
    assert report.ready is False
    assert report.status is CanonicalHealthStatus.UNAVAILABLE


def test_duplicate_capability_names_are_rejected():
    duplicate = capability("sqlite", "ok", required=True)
    with pytest.raises(ValueError, match="unique"):
        aggregate_health("gateway", [duplicate, duplicate], checked_at=NOW)


def test_health_models_reject_naive_timestamps_and_inconsistent_state():
    with pytest.raises(ValidationError):
        CapabilityHealthV1(
            name="sqlite",
            required=True,
            status="ok",
            checked_at=datetime(2026, 7, 14, 17, 0),
        )

    with pytest.raises(ValidationError):
        HealthReportV1(
            service="gateway",
            live=False,
            ready=True,
            status="ok",
            checked_at=NOW,
            capabilities=[],
        )
