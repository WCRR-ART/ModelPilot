import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, Field

from modelpilot.health.eligibility import HealthEligibility, health_eligibility
from modelpilot.health.models import CircuitState
from modelpilot.service import GatewayService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/health", tags=["provider health"])


class ProviderHealthResponse(HealthEligibility):
    provider: str
    model: str
    state: CircuitState
    consecutive_failures: int = Field(ge=0)
    opened_at: AwareDatetime | None = None
    last_failure_at: AwareDatetime | None = None
    last_success_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    probe_in_flight: bool


@router.get("/providers", response_model=list[ProviderHealthResponse])
def provider_health(request: Request) -> list[ProviderHealthResponse]:
    service: GatewayService = request.app.state.gateway
    keys = sorted(
        {
            (candidate.provider, candidate.model)
            for candidate in service.router.candidates
            if candidate.provider in service.router.providers
            and service.router.providers[candidate.provider].configured
        }
    )
    if not keys:
        return []
    store = service.router.health_store
    if store is None:
        logger.warning("provider health store unavailable")
        raise HTTPException(status_code=503, detail="provider health unavailable")
    try:
        now = service.router.clock()
        responses = []
        for provider, model in keys:
            health = store.get_health(provider, model)
            evidence = health_eligibility(health, now=now)
            busy = service.probes.is_in_flight(provider, model) if service.probes else False
            values = evidence.model_dump()
            if health is None:
                values["consecutive_failures"] = 0
            if busy and evidence.state is CircuitState.HALF_OPEN:
                values.update(eligible=False, reason="half_open_probe_in_flight")
            responses.append(
                ProviderHealthResponse(
                    **values,
                    provider=provider,
                    model=model,
                    probe_in_flight=busy,
                    opened_at=health.opened_at if health else None,
                    last_failure_at=health.last_failure_at if health else None,
                    last_success_at=health.last_success_at if health else None,
                    updated_at=health.updated_at if health else None,
                )
            )
        return responses
    except Exception:
        logger.warning("provider health query failed")
        raise HTTPException(status_code=503, detail="provider health unavailable") from None
