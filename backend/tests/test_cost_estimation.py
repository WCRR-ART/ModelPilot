import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from modelpilot.logging import RequestLogStore
from modelpilot.metrics import (
    AttemptRecord,
    ModelPricing,
    SQLiteMetricsStore,
    estimate_cost,
)
from modelpilot.providers import Provider, ProviderErrorType, ProviderOutcome
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import AllProvidersFailed, GatewayService

NOW = datetime.now(UTC)
REQUEST = ChatCompletionRequest(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
)


def make_pricing(
    provider: str = "openai",
    model: str = "openai-model",
    *,
    input_price: str = "2",
    output_price: str = "4",
) -> ModelPricing:
    return ModelPricing(
        provider=provider,
        model=model,
        input_cost_per_million_tokens=Decimal(input_price),
        output_cost_per_million_tokens=Decimal(output_price),
        currency="USD",
        source="test fixture only",
        updated_at=NOW,
    )


def make_outcome(
    provider: str = "openai",
    *,
    model: str | None = None,
    success: bool = True,
    input_tokens: int | None = 1_000,
    output_tokens: int | None = 500,
) -> ProviderOutcome:
    resolved_model = model or f"{provider}-model"
    return ProviderOutcome(
        provider=provider,
        model=resolved_model,
        success=success,
        response=(
            {
                "id": f"chatcmpl-{provider}",
                "object": "chat.completion",
                "model": resolved_model,
                "choices": [],
            }
            if success
            else None
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        error_type=None if success else ProviderErrorType.PROVIDER_ERROR,
        error_message=None if success else "provider failed",
        status_code=200 if success else 503,
        started_at=NOW - timedelta(milliseconds=100),
        finished_at=NOW,
        latency_ms=100,
    )


class OutcomeProvider(Provider):
    def __init__(self, name: str, outcomes: list[ProviderOutcome]) -> None:
        super().__init__("test-key")
        self.name = name
        self.outcomes = outcomes
        self.calls = 0

    async def complete(self, request: ChatCompletionRequest, model: str) -> ProviderOutcome:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return outcome


class PricingStore:
    def __init__(self, pricing: list[ModelPricing] | None = None) -> None:
        self.pricing = {
            (item.provider, item.model): item for item in (pricing or [])
        }
        self.attempts: list[AttemptRecord] = []

    def get_pricing(self, provider: str, model: str) -> ModelPricing | None:
        return self.pricing.get((provider, model))

    def upsert_pricing(self, pricing: ModelPricing) -> None:
        self.pricing[(pricing.provider, pricing.model)] = pricing

    def record_attempt(self, attempt: AttemptRecord) -> None:
        self.attempts.append(attempt)


class FailingPricingLookupStore(PricingStore):
    def get_pricing(self, provider: str, model: str) -> ModelPricing | None:
        raise RuntimeError("pricing lookup unavailable")


def make_gateway(
    providers: list[OutcomeProvider],
    metrics: Any,
) -> GatewayService:
    candidate_count = len(providers)
    candidates = [
        ModelCandidate(
            provider.name,
            f"{provider.name}-model",
            score / candidate_count,
            score / candidate_count,
            score / candidate_count,
            score / candidate_count,
        )
        for provider, score in zip(providers, range(candidate_count, 0, -1), strict=True)
    ]
    return GatewayService(
        ModelRouter({provider.name: provider for provider in providers}, candidates),
        RequestLogStore(),
        metrics,
    )


def test_estimates_input_and_output_cost() -> None:
    assert estimate_cost(make_pricing(), 1_000, 500) == Decimal("0.004")


def test_estimates_cost_with_zero_input_tokens() -> None:
    assert estimate_cost(make_pricing(), 0, 500) == Decimal("0.002")


def test_estimates_cost_with_zero_output_tokens() -> None:
    assert estimate_cost(make_pricing(), 1_000, 0) == Decimal("0.002")


def test_zero_usage_is_a_real_zero_cost() -> None:
    assert estimate_cost(make_pricing(), 0, 0) == Decimal("0")


def test_missing_input_tokens_make_cost_unavailable() -> None:
    assert estimate_cost(make_pricing(), None, 500) is None


def test_missing_output_tokens_make_cost_unavailable() -> None:
    assert estimate_cost(make_pricing(), 1_000, None) is None


def test_missing_pricing_makes_cost_unavailable() -> None:
    assert estimate_cost(None, 1_000, 500) is None


def test_decimal_calculation_preserves_precision() -> None:
    pricing = make_pricing(
        input_price="0.123456789123456789",
        output_price="0.987654321987654321",
    )

    result = estimate_cost(pricing, 1, 1)

    assert result == Decimal("0.000001111111111111111110")


def test_exact_provider_and_model_pricing_is_used() -> None:
    correct = make_pricing(input_price="3", output_price="5")
    store = PricingStore(
        [
            make_pricing(model="other-model", input_price="100", output_price="100"),
            make_pricing(provider="gemini", input_price="200", output_price="200"),
            correct,
        ]
    )
    provider = OutcomeProvider("openai", [make_outcome()])

    asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].estimated_cost == Decimal("0.0055")


def test_pricing_is_not_matched_across_models() -> None:
    store = PricingStore([make_pricing(model="other-model")])
    provider = OutcomeProvider("openai", [make_outcome()])

    asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].estimated_cost is None


def test_pricing_is_not_matched_across_providers() -> None:
    store = PricingStore([make_pricing(provider="gemini")])
    provider = OutcomeProvider("openai", [make_outcome()])

    asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].estimated_cost is None


def test_updated_pricing_is_used_for_new_attempt() -> None:
    store = PricingStore([make_pricing(input_price="1", output_price="1")])
    provider = OutcomeProvider("openai", [make_outcome(), make_outcome()])
    gateway = make_gateway([provider], store)
    asyncio.run(gateway.complete(REQUEST))

    store.upsert_pricing(make_pricing(input_price="2", output_price="2"))
    asyncio.run(gateway.complete(REQUEST))

    assert store.attempts[1].estimated_cost == Decimal("0.003")


def test_pricing_update_does_not_recalculate_old_attempt() -> None:
    store = PricingStore([make_pricing(input_price="1", output_price="1")])
    provider = OutcomeProvider("openai", [make_outcome(), make_outcome()])
    gateway = make_gateway([provider], store)
    asyncio.run(gateway.complete(REQUEST))
    original_cost = store.attempts[0].estimated_cost

    store.upsert_pricing(make_pricing(input_price="9", output_price="9"))
    asyncio.run(gateway.complete(REQUEST))

    assert original_cost == Decimal("0.0015")
    assert store.attempts[0].estimated_cost == original_cost
    assert store.attempts[1].estimated_cost == Decimal("0.0135")


def test_successful_attempt_saves_estimated_cost() -> None:
    store = PricingStore([make_pricing()])
    provider = OutcomeProvider("openai", [make_outcome()])

    asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].success is True
    assert store.attempts[0].estimated_cost == Decimal("0.004")


def test_fallback_attempts_use_their_own_pricing() -> None:
    store = PricingStore(
        [
            make_pricing("openai", "openai-model", input_price="1", output_price="1"),
            make_pricing("gemini", "gemini-model", input_price="3", output_price="4"),
        ]
    )
    first = OutcomeProvider("openai", [make_outcome(success=False)])
    second = OutcomeProvider("gemini", [make_outcome("gemini")])

    asyncio.run(make_gateway([first, second], store).complete(REQUEST))

    assert [attempt.estimated_cost for attempt in store.attempts] == [
        Decimal("0.0015"),
        Decimal("0.005"),
    ]


def test_failed_attempt_without_usage_has_unavailable_cost() -> None:
    store = PricingStore([make_pricing()])
    provider = OutcomeProvider(
        "openai",
        [make_outcome(success=False, input_tokens=None, output_tokens=None)],
    )

    with pytest.raises(AllProvidersFailed):
        asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].estimated_cost is None


def test_failed_attempt_with_real_usage_can_have_estimated_cost() -> None:
    store = PricingStore([make_pricing()])
    provider = OutcomeProvider("openai", [make_outcome(success=False)])

    with pytest.raises(AllProvidersFailed):
        asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert store.attempts[0].estimated_cost == Decimal("0.004")


def test_pricing_lookup_failure_does_not_affect_success_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FailingPricingLookupStore()
    provider = OutcomeProvider("openai", [make_outcome()])

    with caplog.at_level(logging.WARNING, logger="modelpilot.service"):
        result = asyncio.run(make_gateway([provider], store).complete(REQUEST))

    assert result["model"] == "openai-model"
    assert provider.calls == 1
    assert store.attempts[0].estimated_cost is None
    assert "failed to look up pricing" in caplog.text


def test_estimated_cost_round_trips_through_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "metrics.sqlite3"
    store = SQLiteMetricsStore(database_path)
    store.upsert_pricing(make_pricing())
    provider = OutcomeProvider("openai", [make_outcome()])

    asyncio.run(make_gateway([provider], store).complete(REQUEST))
    reopened = SQLiteMetricsStore(database_path)
    attempts = reopened.get_recent_attempts(
        "openai", "openai-model", 10, NOW - timedelta(days=1)
    )

    assert attempts[0].estimated_cost == Decimal("0.004")
