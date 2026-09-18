from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from modelpilot.health.models import CircuitState, ProviderHealth


class HealthEligibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    state: CircuitState
    reason: Literal[
        "healthy",
        "circuit_open",
        "half_open_probe_eligible",
        "health_unknown",
        "health_store_unavailable",
    ]


def health_eligibility(health: ProviderHealth | None, *, now: datetime) -> HealthEligibility:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if health is None:
        return HealthEligibility(eligible=True, state=CircuitState.CLOSED, reason="health_unknown")
    state = health.advance(now=now).state
    return HealthEligibility(
        eligible=state is not CircuitState.OPEN,
        state=state,
        reason=(
            "circuit_open"
            if state is CircuitState.OPEN
            else "half_open_probe_eligible"
            if state is CircuitState.HALF_OPEN
            else "healthy"
        ),
    )
