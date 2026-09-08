import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modelpilot.logging import RequestLogStore
from modelpilot.metrics import (
    SCHEMA_VERSION,
    AttemptRecord,
    DuplicateRoutingDecisionError,
    ModelPricing,
    ProviderMetricsSnapshot,
    RoutingDecision,
    RoutingExplanation,
    RoutingSignal,
    SQLiteMetricsStore,
)
from modelpilot.providers.base import Provider, ProviderError
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import AllProvidersFailed, GatewayService

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


class ExplanationProvider(Provider):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        super().__init__(f"{name}-test-key")
        self.name = name
        self.fails = fails

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        if self.fails:
            raise ProviderError("simulated provider failure")
        return {
            "id": f"chatcmpl-{self.name}",
            "object": "chat.completion",
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }


class DecisionStore:
    def __init__(
        self,
        snapshots: list[ProviderMetricsSnapshot] | None = None,
        *,
        fail_writes: bool = False,
    ) -> None:
        self.snapshots = snapshots or []
        self.fail_writes = fail_writes
        self.decisions: list[RoutingDecision] = []

    def list_provider_metrics(self) -> list[ProviderMetricsSnapshot]:
        return self.snapshots

    def record_routing_decision(self, decision: RoutingDecision) -> None:
        if self.fail_writes:
            raise RuntimeError("Authorization: Bearer decision-secret")
        self.decisions.append(decision)


def make_snapshot(
    provider: str,
    model: str,
    *,
    sample_count: int = 50,
    success_count: int = 50,
    p50_latency_ms: float | None = 500,
    priced_sample_count: int = 50,
    p50_cost: Decimal | None = Decimal("0.0005"),
) -> ProviderMetricsSnapshot:
    return ProviderMetricsSnapshot(
        provider=provider,
        model=model,
        sample_count=sample_count,
        success_count=success_count,
        failure_count=sample_count - success_count,
        success_rate=success_count / sample_count,
        average_latency_ms=p50_latency_ms,
        p50_latency_ms=p50_latency_ms,
        p95_latency_ms=p50_latency_ms,
        priced_sample_count=priced_sample_count,
        estimated_average_cost=p50_cost,
        p50_estimated_cost=p50_cost,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
    )


def make_gateway(
    providers: list[ExplanationProvider],
    store: DecisionStore,
) -> GatewayService:
    candidates = [
        ModelCandidate(
            provider.name,
            f"{provider.name}-model",
            1 - index * 0.1,
            1 - index * 0.1,
            1 - index * 0.1,
            1 - index * 0.1,
        )
        for index, provider in enumerate(providers)
    ]
    router = ModelRouter(
        {provider.name: provider for provider in providers}, candidates, store
    )
    return GatewayService(router, RequestLogStore(), store)


def make_request(model: str = "auto", content: str = "hello") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model, messages=[{"role": "user", "content": content}]
    )


def make_explanation(
    request_id: str = "req_decision",
    *,
    created_at: datetime = NOW,
    served_provider: str | None = "alpha",
    served_model: str | None = "alpha-model",
) -> RoutingExplanation:
    signal = RoutingSignal(
        provider="alpha",
        model="alpha-model",
        quality_score=0.9,
        latency_score=0.8,
        reliability_score=0.7,
        cost_score=0.6,
        measured_sample_count=12,
        measured_confidence=7 / 45,
        final_score=0.75,
        rank=1,
        selected=True,
        sources={
            "quality": "configured",
            "latency": "blended",
            "reliability": "blended",
            "cost": "static_unavailable",
        },
    )
    return RoutingExplanation(
        request_id=request_id,
        routing_version="v0.2",
        strategy="weighted_measured_v1",
        selected_provider="alpha",
        selected_model="alpha-model",
        served_provider=served_provider,
        served_model=served_model,
        selected=signal,
        candidates=(signal,),
        created_at=created_at,
    )


def make_decision(**overrides: object) -> RoutingDecision:
    created_at = overrides.get("created_at", NOW)
    assert isinstance(created_at, datetime)
    explanation = make_explanation(
        request_id=str(overrides.get("request_id", "req_decision")),
        created_at=created_at,
    )
    values: dict[str, object] = {
        "request_id": explanation.request_id,
        "routing_version": explanation.routing_version,
        "selected_provider": explanation.selected_provider,
        "selected_model": explanation.selected_model,
        "served_provider": explanation.served_provider,
        "served_model": explanation.served_model,
        "created_at": explanation.created_at,
        "explanation": explanation,
    }
    values.update(overrides)
    return RoutingDecision.model_validate(values)


def test_auto_response_contains_structured_explanation_matching_router_order() -> None:
    store = DecisionStore()
    gateway = make_gateway(
        [ExplanationProvider("alpha"), ExplanationProvider("beta")], store
    )

    result = asyncio.run(gateway.complete(make_request()))
    routing = result["modelpilot"]["routing"]

    assert result["id"] == "chatcmpl-alpha"
    assert result["object"] == "chat.completion"
    assert result["choices"] == []
    assert result["usage"]["total_tokens"] == 5
    assert routing["request_id"] == result["modelpilot"]["request_id"]
    assert routing["routing_version"] == "v0.2"
    assert [item["provider"] for item in routing["candidates"]] == ["alpha", "beta"]
    assert [item["rank"] for item in routing["candidates"]] == [1, 2]
    assert len({item["rank"] for item in routing["candidates"]}) == 2
    assert routing["selected_provider"] == "alpha"
    assert routing["selected_model"] == "alpha-model"
    assert routing["selected"]["final_score"] == routing["candidates"][0]["final_score"]
    assert routing["candidates"][0]["selected"] is True
    assert routing["candidates"][1]["selected"] is False
    assert routing["candidates"][0]["sources"]["quality"] == "configured"
    assert routing["candidates"][0]["measured_confidence"] == 0
    assert routing["candidates"][0]["measured_sample_count"] == 0


def test_no_fallback_selected_candidate_equals_served_by() -> None:
    store = DecisionStore()
    result = asyncio.run(
        make_gateway([ExplanationProvider("alpha")], store).complete(make_request())
    )

    assert result["modelpilot"]["served_by"] == {
        "provider": "alpha",
        "model": "alpha-model",
    }
    assert result["modelpilot"]["routing"]["served_provider"] == "alpha"
    assert store.decisions[0].served_provider == "alpha"


def test_fallback_preserves_initial_selection_and_records_actual_server() -> None:
    store = DecisionStore()
    gateway = make_gateway(
        [ExplanationProvider("alpha", fails=True), ExplanationProvider("beta")], store
    )

    result = asyncio.run(gateway.complete(make_request()))
    routing = result["modelpilot"]["routing"]

    assert routing["selected_provider"] == "alpha"
    assert routing["served_provider"] == "beta"
    assert result["modelpilot"]["served_by"]["provider"] == "beta"
    assert [item["provider"] for item in routing["candidates"]] == ["alpha", "beta"]
    assert store.decisions[0].selected_provider == "alpha"
    assert store.decisions[0].served_provider == "beta"


def test_all_providers_fail_still_persists_routing_decision() -> None:
    store = DecisionStore()
    gateway = make_gateway(
        [
            ExplanationProvider("alpha", fails=True),
            ExplanationProvider("beta", fails=True),
        ],
        store,
    )

    with pytest.raises(AllProvidersFailed):
        asyncio.run(gateway.complete(make_request()))

    assert len(store.decisions) == 1
    assert store.decisions[0].selected_provider == "alpha"
    assert store.decisions[0].served_provider is None


def test_explicit_model_has_served_by_without_auto_explanation_or_decision() -> None:
    store = DecisionStore()
    gateway = make_gateway([ExplanationProvider("alpha")], store)

    result = asyncio.run(gateway.complete(make_request("alpha-model")))

    assert result["modelpilot"]["served_by"]["provider"] == "alpha"
    assert "routing" not in result["modelpilot"]
    assert store.decisions == []


def test_blended_and_missing_metric_sources_are_returned_without_recalculation() -> None:
    snapshot = make_snapshot(
        "alpha",
        "alpha-model",
        sample_count=6,
        success_count=5,
        p50_latency_ms=750,
        priced_sample_count=0,
        p50_cost=None,
    )
    store = DecisionStore([snapshot])
    gateway = make_gateway([ExplanationProvider("alpha")], store)

    result = asyncio.run(gateway.complete(make_request()))
    candidate = result["modelpilot"]["routing"]["candidates"][0]

    assert candidate["sources"]["latency"] == "blended"
    assert candidate["sources"]["reliability"] == "blended"
    assert candidate["sources"]["cost"] == "static_unavailable"
    assert candidate["measured_sample_count"] == 6
    assert candidate["measured_confidence"] == pytest.approx(round(1 / 45, 6))
    assert candidate["final_score"] == store.decisions[0].explanation.candidates[0].final_score


def test_routing_persistence_failure_does_not_affect_success_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = DecisionStore(fail_writes=True)
    gateway = make_gateway([ExplanationProvider("alpha")], store)

    with caplog.at_level(logging.WARNING, logger="modelpilot.service"):
        result = asyncio.run(gateway.complete(make_request()))

    assert result["model"] == "alpha-model"
    assert "failed to persist routing decision" in caplog.text
    assert "decision-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text


def test_explanation_serialization_contains_no_request_or_credential_content() -> None:
    secret_prompt = "private prompt text"
    store = DecisionStore()
    gateway = make_gateway([ExplanationProvider("alpha")], store)

    asyncio.run(gateway.complete(make_request(content=secret_prompt)))
    serialized = store.decisions[0].explanation.model_dump_json()

    assert secret_prompt not in serialized
    assert "alpha-test-key" not in serialized
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized
    assert "token" not in serialized.lower()
    assert "prompt" not in serialized.lower()
    assert "completion" not in serialized.lower()


def test_routing_explanation_json_round_trip() -> None:
    explanation = make_explanation()

    restored = RoutingExplanation.model_validate_json(explanation.model_dump_json())

    assert restored == explanation


def test_sqlite_persists_and_reopens_complete_explanation(tmp_path: Path) -> None:
    database = tmp_path / "metrics.sqlite3"
    decision = make_decision()
    SQLiteMetricsStore(database).record_routing_decision(decision)

    reopened = SQLiteMetricsStore(database)

    assert reopened.get_routing_decision(decision.request_id) == decision


def test_new_database_uses_schema_two_with_routing_table(tmp_path: Path) -> None:
    database = tmp_path / "metrics.sqlite3"
    SQLiteMetricsStore(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'routing_decisions'"
        ).fetchone()

    assert version == SCHEMA_VERSION == 2
    assert table == ("routing_decisions",)


def test_v1_to_v2_migration_preserves_attempts_and_pricing(tmp_path: Path) -> None:
    database = tmp_path / "metrics.sqlite3"
    old_store = SQLiteMetricsStore(database)
    attempt = AttemptRecord(
        request_id="req_old",
        provider="alpha",
        model="alpha-model",
        started_at=NOW - timedelta(milliseconds=100),
        finished_at=NOW,
        latency_ms=100,
        success=True,
        created_at=NOW,
    )
    pricing = ModelPricing(
        provider="alpha",
        model="alpha-model",
        input_cost_per_million_tokens=Decimal("1"),
        output_cost_per_million_tokens=Decimal("2"),
        source="test fixture",
        updated_at=NOW,
    )
    old_store.record_attempt(attempt)
    old_store.upsert_pricing(pricing)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE routing_decisions")
        connection.execute("UPDATE schema_version SET version = 1 WHERE singleton = 1")

    migrated = SQLiteMetricsStore(database)

    assert migrated.get_recent_attempts("alpha", "alpha-model", 10, NOW - timedelta(days=1)) == [
        attempt
    ]
    assert migrated.get_pricing("alpha", "alpha-model") == pricing
    migrated.record_routing_decision(make_decision())
    assert SQLiteMetricsStore(database).get_routing_decision("req_decision") is not None


def test_duplicate_routing_request_id_is_rejected(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite3")
    decision = make_decision()
    store.record_routing_decision(decision)

    with pytest.raises(DuplicateRoutingDecisionError):
        store.record_routing_decision(decision)


def test_recent_routing_decisions_are_stably_newest_first(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite3")
    older = make_decision(request_id="req_a", created_at=NOW - timedelta(minutes=1))
    newer_b = make_decision(request_id="req_b", created_at=NOW)
    newer_c = make_decision(request_id="req_c", created_at=NOW)
    for decision in [newer_b, older, newer_c]:
        store.record_routing_decision(decision)

    restored = store.list_recent_routing_decisions(limit=3)

    assert [item.request_id for item in restored] == ["req_c", "req_b", "req_a"]
    assert store.list_recent_routing_decisions(limit=2) == restored[:2]


def test_recent_routing_decisions_reject_non_positive_limit(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite3")

    with pytest.raises(ValueError, match="limit"):
        store.list_recent_routing_decisions(limit=0)


def test_missing_routing_decision_returns_none(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite3")

    assert store.get_routing_decision("missing") is None
