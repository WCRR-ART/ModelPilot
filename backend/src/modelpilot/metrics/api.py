import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request

from modelpilot.metrics.models import (
    MetricsSummary,
    ProviderMetricsSnapshot,
    RecentFailure,
    RoutingDecision,
)
from modelpilot.metrics.store import MetricsStore
from modelpilot.providers.base import sanitize_error_message
from modelpilot.service import GatewayService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/metrics", tags=["metrics"])
ResultT = TypeVar("ResultT")


def _metrics_store(request: Request) -> MetricsStore:
    service: GatewayService = request.app.state.gateway
    if service.metrics is None:
        logger.warning("metrics query failed: metrics store is unavailable")
        raise HTTPException(status_code=503, detail="metrics data unavailable")
    return service.metrics


def _query(operation: str, callback: Callable[[], ResultT]) -> ResultT:
    try:
        return callback()
    except Exception as exc:
        logger.warning(
            "metrics query failed for %s: %s",
            operation,
            sanitize_error_message(str(exc)),
        )
        raise HTTPException(status_code=503, detail="metrics data unavailable") from exc


@router.get("/summary", response_model=MetricsSummary)
def metrics_summary(
    request: Request,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> MetricsSummary:
    store = _metrics_store(request)
    until = datetime.now(UTC)
    since = until - timedelta(hours=hours)
    return _query("summary", lambda: store.get_metrics_summary(since, until))


@router.get("/providers", response_model=list[ProviderMetricsSnapshot])
def provider_metrics(
    request: Request,
    provider: str | None = None,
    model: str | None = None,
) -> list[ProviderMetricsSnapshot]:
    store = _metrics_store(request)
    snapshots = _query("providers", store.list_provider_metrics)
    return [
        snapshot
        for snapshot in snapshots
        if (provider is None or snapshot.provider == provider)
        and (model is None or snapshot.model == model)
    ]


@router.get("/routing-decisions", response_model=list[RoutingDecision])
def routing_decisions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RoutingDecision]:
    store = _metrics_store(request)
    return _query(
        "routing decisions", lambda: store.list_recent_routing_decisions(limit)
    )


@router.get("/failures", response_model=list[RecentFailure])
def recent_failures(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RecentFailure]:
    store = _metrics_store(request)
    return _query("failures", lambda: store.list_recent_failures(limit))
