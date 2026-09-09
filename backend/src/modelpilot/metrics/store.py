from datetime import datetime
from typing import Protocol, runtime_checkable

from modelpilot.metrics.models import (
    AttemptRecord,
    MetricsSummary,
    ModelPricing,
    ProviderMetricsSnapshot,
    RecentFailure,
    RoutingDecision,
)


@runtime_checkable
class MetricsStore(Protocol):
    def record_attempt(self, attempt: AttemptRecord) -> None: ...

    def get_recent_attempts(
        self,
        provider: str,
        model: str,
        limit: int,
        since: datetime,
    ) -> list[AttemptRecord]: ...

    def get_provider_metrics(
        self,
        provider: str,
        model: str,
    ) -> ProviderMetricsSnapshot | None: ...

    def list_provider_metrics(self) -> list[ProviderMetricsSnapshot]: ...

    def get_pricing(self, provider: str, model: str) -> ModelPricing | None: ...

    def list_pricing(self) -> list[ModelPricing]: ...

    def upsert_pricing(self, pricing: ModelPricing) -> None: ...

    def record_routing_decision(self, decision: RoutingDecision) -> None: ...

    def get_routing_decision(self, request_id: str) -> RoutingDecision | None: ...

    def list_recent_routing_decisions(self, limit: int) -> list[RoutingDecision]: ...

    def get_metrics_summary(
        self, since: datetime, until: datetime
    ) -> MetricsSummary: ...

    def list_recent_failures(self, limit: int) -> list[RecentFailure]: ...
