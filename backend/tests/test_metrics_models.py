import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from modelpilot.metrics import (
    AttemptRecord,
    ModelPricing,
    ProviderMetricsSnapshot,
    RoutingExplanation,
    RoutingSignal,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def make_attempt(**overrides: object) -> AttemptRecord:
    values: dict[str, object] = {
        "request_id": "req_test",
        "provider": "openai",
        "model": "test-model",
        "started_at": NOW,
        "finished_at": NOW + timedelta(milliseconds=125),
        "latency_ms": 125.0,
        "success": True,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "estimated_cost": Decimal("0.00125"),
        "created_at": NOW,
    }
    values.update(overrides)
    return AttemptRecord.model_validate(values)


def make_signal(**overrides: object) -> RoutingSignal:
    values: dict[str, object] = {
        "provider": "openai",
        "model": "test-model",
        "quality_score": 0.8,
        "latency_score": 0.7,
        "reliability_score": 0.9,
        "cost_score": 0.6,
        "measured_sample_count": 25,
        "measured_confidence": 0.5,
        "final_score": 0.75,
        "sources": {
            "quality": "configured",
            "latency": "measured",
            "reliability": "measured",
            "cost": "measured_estimate",
        },
        "reason_metadata": {"window_attempts": 100, "pricing_available": True},
    }
    values.update(overrides)
    return RoutingSignal.model_validate(values)


def test_attempt_record_accepts_successful_request() -> None:
    attempt = make_attempt()

    assert attempt.success is True
    assert attempt.total_tokens == 15
    assert attempt.estimated_cost == Decimal("0.00125")


def test_attempt_record_accepts_failed_request() -> None:
    attempt = make_attempt(
        success=False,
        error_type="timeout",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost=None,
    )

    assert attempt.success is False
    assert attempt.error_type == "timeout"


def test_attempt_record_preserves_unavailable_usage_and_cost() -> None:
    attempt = make_attempt(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost=None,
    )

    assert attempt.input_tokens is None
    assert attempt.output_tokens is None
    assert attempt.total_tokens is None
    assert attempt.estimated_cost is None


def test_attempt_record_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        make_attempt(latency_ms=-0.01)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
def test_attempt_record_rejects_negative_tokens(field: str) -> None:
    with pytest.raises(ValidationError):
        make_attempt(**{field: -1})


def test_attempt_record_requires_aware_ordered_timestamps() -> None:
    with pytest.raises(ValidationError):
        make_attempt(started_at=datetime(2026, 9, 7, 12), finished_at=datetime(2026, 9, 7, 13))

    with pytest.raises(ValidationError):
        make_attempt(started_at=NOW, finished_at=NOW - timedelta(milliseconds=1))


def test_provider_metrics_snapshot_validates_counts_and_rate() -> None:
    snapshot = ProviderMetricsSnapshot(
        provider="openai",
        model="test-model",
        sample_count=4,
        success_count=3,
        failure_count=1,
        success_rate=0.75,
        average_latency_ms=200,
        p50_latency_ms=180,
        p95_latency_ms=350,
        priced_sample_count=3,
        estimated_average_cost=Decimal("0.002"),
        p50_estimated_cost=Decimal("0.002"),
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
    )

    assert snapshot.sample_count == snapshot.success_count + snapshot.failure_count


@pytest.mark.parametrize("success_rate", [-0.01, 1.01])
def test_provider_metrics_snapshot_rejects_out_of_range_rate(success_rate: float) -> None:
    with pytest.raises(ValidationError):
        ProviderMetricsSnapshot(
            provider="openai",
            model="test-model",
            sample_count=1,
            success_count=1,
            failure_count=0,
            success_rate=success_rate,
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
        )


def test_provider_metrics_snapshot_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValidationError):
        ProviderMetricsSnapshot(
            provider="openai",
            model="test-model",
            sample_count=3,
            success_count=3,
            failure_count=1,
            success_rate=0.75,
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
        )


def test_empty_provider_metrics_snapshot_uses_nullable_aggregates() -> None:
    snapshot = ProviderMetricsSnapshot(
        provider="openai",
        model="test-model",
        sample_count=0,
        success_count=0,
        failure_count=0,
        success_rate=None,
        average_latency_ms=None,
        p50_latency_ms=None,
        p95_latency_ms=None,
        estimated_average_cost=None,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
    )

    assert snapshot.success_rate is None
    assert snapshot.average_latency_ms is None


def test_empty_provider_metrics_snapshot_rejects_fake_zero_aggregates() -> None:
    with pytest.raises(ValidationError):
        ProviderMetricsSnapshot(
            provider="openai",
            model="test-model",
            sample_count=0,
            success_count=0,
            failure_count=0,
            success_rate=0,
            average_latency_ms=0,
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
        )


def test_model_pricing_accepts_configured_decimal_prices() -> None:
    pricing = ModelPricing(
        provider="test-provider",
        model="test-model",
        input_cost_per_million_tokens=Decimal("1.25"),
        output_cost_per_million_tokens=Decimal("2.50"),
        currency="USD",
        source="test fixture",
        updated_at=NOW,
    )

    assert pricing.input_cost_per_million_tokens == Decimal("1.25")
    assert pricing.source == "test fixture"


@pytest.mark.parametrize(
    "field",
    ["input_cost_per_million_tokens", "output_cost_per_million_tokens"],
)
def test_model_pricing_rejects_negative_prices(field: str) -> None:
    values: dict[str, object] = {
        "provider": "test-provider",
        "model": "test-model",
        "input_cost_per_million_tokens": Decimal("1"),
        "output_cost_per_million_tokens": Decimal("2"),
        "source": "test fixture",
        "updated_at": NOW,
    }
    values[field] = Decimal("-0.01")

    with pytest.raises(ValidationError):
        ModelPricing.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quality_score", -0.01),
        ("latency_score", 1.01),
        ("reliability_score", -0.01),
        ("cost_score", 1.01),
        ("measured_confidence", 1.01),
        ("final_score", -0.01),
    ],
)
def test_routing_signal_rejects_out_of_range_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_signal(**{field: value})


def test_routing_explanation_serializes_structured_signals() -> None:
    signal = make_signal()
    explanation = RoutingExplanation(
        strategy="weighted_measured_v1",
        selected=signal,
        candidates=(signal,),
        created_at=NOW,
    )

    payload = json.loads(explanation.model_dump_json())

    assert payload["selected"]["final_score"] == 0.75
    assert payload["selected"]["sources"]["latency"] == "measured"
    assert payload["selected"]["reason_metadata"]["pricing_available"] is True
    assert payload["created_at"] == "2026-09-07T12:00:00Z"
