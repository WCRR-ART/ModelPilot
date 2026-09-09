from datetime import UTC, datetime, timedelta
from decimal import Decimal

from modelpilot.metrics import (
    AttemptRecord,
    MetricsStore,
    MetricsSummary,
    MetricsWindow,
    ModelPricing,
    ProviderMetricsSnapshot,
    RecentFailure,
    RoutingDecision,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


class FakeMetricsStore:
    """Test-only implementation that proves the Protocol remains structurally usable."""

    def __init__(self) -> None:
        self.attempts: list[AttemptRecord] = []
        self.snapshots: dict[tuple[str, str], ProviderMetricsSnapshot] = {}
        self.pricing: dict[tuple[str, str], ModelPricing] = {}
        self.decisions: dict[str, RoutingDecision] = {}

    def record_attempt(self, attempt: AttemptRecord) -> None:
        self.attempts.append(attempt)

    def get_recent_attempts(
        self,
        provider: str,
        model: str,
        limit: int,
        since: datetime,
    ) -> list[AttemptRecord]:
        matches = [
            attempt
            for attempt in self.attempts
            if attempt.provider == provider
            and attempt.model == model
            and attempt.finished_at >= since
        ]
        return sorted(matches, key=lambda item: item.finished_at, reverse=True)[:limit]

    def get_provider_metrics(
        self,
        provider: str,
        model: str,
    ) -> ProviderMetricsSnapshot | None:
        return self.snapshots.get((provider, model))

    def list_provider_metrics(self) -> list[ProviderMetricsSnapshot]:
        return list(self.snapshots.values())

    def get_pricing(self, provider: str, model: str) -> ModelPricing | None:
        return self.pricing.get((provider, model))

    def list_pricing(self) -> list[ModelPricing]:
        return list(self.pricing.values())

    def upsert_pricing(self, pricing: ModelPricing) -> None:
        self.pricing[(pricing.provider, pricing.model)] = pricing

    def record_routing_decision(self, decision: RoutingDecision) -> None:
        self.decisions[decision.request_id] = decision

    def get_routing_decision(self, request_id: str) -> RoutingDecision | None:
        return self.decisions.get(request_id)

    def list_recent_routing_decisions(self, limit: int) -> list[RoutingDecision]:
        return sorted(
            self.decisions.values(),
            key=lambda item: (item.created_at, item.request_id),
            reverse=True,
        )[:limit]

    def get_metrics_summary(
        self, since: datetime, until: datetime
    ) -> MetricsSummary:
        attempts = [
            attempt
            for attempt in self.attempts
            if since <= attempt.finished_at <= until
        ]
        successes = sum(attempt.success for attempt in attempts)
        costs = [
            attempt.estimated_cost
            for attempt in attempts
            if attempt.estimated_cost is not None
        ]
        return MetricsSummary(
            window=MetricsWindow(since=since, until=until),
            request_count=len({attempt.request_id for attempt in attempts}),
            attempt_count=len(attempts),
            success_count=successes,
            failure_count=len(attempts) - successes,
            success_rate=successes / len(attempts) if attempts else None,
            average_latency_ms=(
                sum(attempt.latency_ms for attempt in attempts) / len(attempts)
                if attempts
                else None
            ),
            estimated_total_cost=sum(costs, Decimal(0)) if costs else None,
            providers_count=len({attempt.provider for attempt in attempts}),
            models_count=len({attempt.model for attempt in attempts}),
            routing_decision_count=sum(
                since <= decision.created_at <= until
                for decision in self.decisions.values()
            ),
        )

    def list_recent_failures(self, limit: int) -> list[RecentFailure]:
        failures = [
            RecentFailure(
                request_id=attempt.request_id,
                provider=attempt.provider,
                model=attempt.model,
                finished_at=attempt.finished_at,
                created_at=attempt.created_at,
                latency_ms=attempt.latency_ms,
                error_type=attempt.error_type,
            )
            for attempt in self.attempts
            if not attempt.success
        ]
        return sorted(failures, key=lambda item: item.finished_at, reverse=True)[:limit]


def test_fake_store_implements_metrics_store_protocol() -> None:
    store = FakeMetricsStore()

    assert isinstance(store, MetricsStore)


def test_fake_store_records_and_filters_recent_attempts() -> None:
    store = FakeMetricsStore()
    recent = AttemptRecord(
        request_id="req_recent",
        provider="openai",
        model="test-model",
        started_at=NOW - timedelta(seconds=1),
        finished_at=NOW,
        latency_ms=1000,
        success=True,
        created_at=NOW,
    )
    old = recent.model_copy(
        update={
            "request_id": "req_old",
            "started_at": NOW - timedelta(days=10, seconds=1),
            "finished_at": NOW - timedelta(days=10),
            "created_at": NOW - timedelta(days=10),
        }
    )
    store.record_attempt(old)
    store.record_attempt(recent)

    result = store.get_recent_attempts(
        provider="openai",
        model="test-model",
        limit=100,
        since=NOW - timedelta(days=7),
    )

    assert result == [recent]


def test_fake_store_supports_metrics_and_pricing_contract() -> None:
    store = FakeMetricsStore()
    snapshot = ProviderMetricsSnapshot(
        provider="openai",
        model="test-model",
        sample_count=0,
        success_count=0,
        failure_count=0,
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
    )
    pricing = ModelPricing(
        provider="openai",
        model="test-model",
        input_cost_per_million_tokens=Decimal("1"),
        output_cost_per_million_tokens=Decimal("2"),
        source="test fixture",
        updated_at=NOW,
    )
    store.snapshots[(snapshot.provider, snapshot.model)] = snapshot
    store.upsert_pricing(pricing)

    assert store.get_provider_metrics("openai", "test-model") == snapshot
    assert store.list_provider_metrics() == [snapshot]
    assert store.get_pricing("openai", "test-model") == pricing
    assert store.list_pricing() == [pricing]
