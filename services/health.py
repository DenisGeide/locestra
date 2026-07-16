"""Pure health aggregation for required and optional platform capabilities."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

HEALTH_SCHEMA_VERSION = "1.0"

HealthName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
HealthDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_048),
]


class CapabilityStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ON_DEMAND = "on_demand"
    DISABLED = "disabled"


class CanonicalHealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


class StrictHealthModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class CapabilityHealthV1(StrictHealthModel):
    schema_version: Literal["1.0"] = HEALTH_SCHEMA_VERSION
    name: HealthName
    required: bool
    status: CapabilityStatus
    detail: HealthDetail | None = None
    checked_at: datetime
    latency_ms: float | None = Field(default=None, ge=0, le=86_400_000)

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "checked_at")


class HealthReportV1(StrictHealthModel):
    schema_version: Literal["1.0"] = HEALTH_SCHEMA_VERSION
    service: HealthName
    live: bool
    ready: bool
    status: CanonicalHealthStatus
    checked_at: datetime
    capabilities: list[CapabilityHealthV1] = Field(max_length=128)

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "checked_at")

    @model_validator(mode="after")
    def validate_consistency(self) -> "HealthReportV1":
        if self.ready and not self.live:
            raise ValueError("a non-live service cannot be ready")
        if not self.live or not self.ready:
            if self.status is not CanonicalHealthStatus.UNAVAILABLE:
                raise ValueError("a non-ready service must report unavailable")
        elif self.status is CanonicalHealthStatus.UNAVAILABLE:
            raise ValueError("unavailable status conflicts with a live and ready service")
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("capability names must be unique")
        return self


def aggregate_health(
    service: str,
    capabilities: Iterable[CapabilityHealthV1],
    *,
    live: bool = True,
    checked_at: datetime | None = None,
) -> HealthReportV1:
    """Aggregate capability observations without performing any probes.

    Required capabilities are ready when they are ``ok`` or still serviceable
    but ``degraded``.  A missing, disabled, or on-demand required dependency is
    not ready.  Optional ``unavailable``/``degraded`` capabilities make the
    canonical report degraded, while intentionally ``disabled`` or
    ``on_demand`` optional capabilities are neutral.
    """

    observed = list(capabilities)
    names = [capability.name for capability in observed]
    if len(names) != len(set(names)):
        raise ValueError("capability names must be unique")

    required_ready_statuses = {CapabilityStatus.OK, CapabilityStatus.DEGRADED}
    required_ready = all(
        capability.status in required_ready_statuses
        for capability in observed
        if capability.required
    )
    ready = live and required_ready

    if not ready:
        status = CanonicalHealthStatus.UNAVAILABLE
    else:
        required_degraded = any(
            capability.required and capability.status is CapabilityStatus.DEGRADED
            for capability in observed
        )
        optional_degraded = any(
            not capability.required
            and capability.status in {CapabilityStatus.DEGRADED, CapabilityStatus.UNAVAILABLE}
            for capability in observed
        )
        status = (
            CanonicalHealthStatus.DEGRADED
            if required_degraded or optional_degraded
            else CanonicalHealthStatus.OK
        )

    observed_at = checked_at or datetime.now(timezone.utc)
    return HealthReportV1(
        service=service,
        live=live,
        ready=ready,
        status=status,
        checked_at=observed_at,
        capabilities=observed,
    )


__all__ = [
    "HEALTH_SCHEMA_VERSION",
    "CanonicalHealthStatus",
    "CapabilityHealthV1",
    "CapabilityStatus",
    "HealthReportV1",
    "aggregate_health",
]
