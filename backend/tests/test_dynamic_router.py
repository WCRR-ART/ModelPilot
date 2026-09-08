import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modelpilot.logging import RequestLogStore
from modelpilot.metrics import ProviderMetricsSnapshot
from modelpilot.providers.base import Provider
from modelpilot.router import (
    ModelCandidate,
    ModelRouter,
    blend_score,
    confidence_for_samples,
    normalize_cost,
    normalize_latency,
)
from modelpilot.schemas import ChatCompletionRequest, RoutingPreferences
from modelpilot.service import GatewayService

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


class DynamicProvider(Provider):
    def __init__(self, name: str) -> None:
        super().__init__("test-key")
        self.name = name

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        return {
            "id": "chatcmpl-dynamic",
            "object": "chat.completion",
            "model": model,
            "choices": [],
        }


class SnapshotStore:
    def __init__(
        self,
        snapshots: dict[tuple[str, str], ProviderMetricsSnapshot] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.snapshots = snapshots or {}
        self.fail = fail
        self.queries: list[tuple[str, str]] = []
        self.list_calls = 0

    def get_provider_metrics(
        self, provider: str, model: str
    ) -> ProviderMetricsSnapshot | None:
        self.queries.append((provider, model))
        if self.fail:
            raise RuntimeError("metrics unavailable")
        return self.snapshots.get((provider, model))

    def list_provider_metrics(self) -> list[ProviderMetricsSnapshot]:
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("metrics unavailable")
        return list(self.snapshots.values())


def make_snapshot(
    provider: str,
    model: str,
    *,
    sample_count: int = 50,
    success_count: int | None = None,
    p50_latency_ms: float | None = 1000,
    priced_sample_count: int | None = None,
    p50_estimated_cost: Decimal | None = Decimal("0.001"),
) -> ProviderMetricsSnapshot:
    successes = sample_count if success_count is None else success_count
    priced = (
        successes if priced_sample_count is None and p50_estimated_cost is not None
        else (priced_sample_count or 0)
    )
    cost = p50_estimated_cost if priced else None
    return ProviderMetricsSnapshot(
        provider=provider,
        model=model,
        sample_count=sample_count,
        success_count=successes,
        failure_count=sample_count - successes,
        success_rate=successes / sample_count if sample_count else None,
        average_latency_ms=p50_latency_ms,
        p50_latency_ms=p50_latency_ms,
        p95_latency_ms=p50_latency_ms,
        priced_sample_count=priced,
        estimated_average_cost=cost,
        p50_estimated_cost=cost,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
    )


def make_router(
    candidates: list[ModelCandidate],
    snapshots: dict[tuple[str, str], ProviderMetricsSnapshot] | None = None,
    *,
    store: SnapshotStore | None = None,
) -> tuple[ModelRouter, SnapshotStore | None]:
    providers = {
        candidate.provider: DynamicProvider(candidate.provider) for candidate in candidates
    }
    selected_store = store or (SnapshotStore(snapshots) if snapshots is not None else None)
    return ModelRouter(providers, candidates, selected_store), selected_store


@pytest.mark.parametrize(
    ("sample_count", "expected"),
    [(0, 0), (4, 0), (5, 0), (6, 1 / 45), (49, 44 / 45), (50, 1), (100, 1)],
)
def test_confidence_boundaries(sample_count: int, expected: float) -> None:
    assert confidence_for_samples(sample_count) == pytest.approx(expected)


def test_latency_normalization_is_bounded_and_rewards_lower_latency() -> None:
    assert normalize_latency(500) == 1
    assert normalize_latency(2000) == 0.5
    assert normalize_latency(1_000_000) == pytest.approx(1 / 30)


def test_cost_normalization_is_decimal_safe_and_rewards_lower_cost() -> None:
    assert normalize_cost(Decimal("0.0005")) == 1
    assert normalize_cost(Decimal("0.002")) == 0.5
    assert 0 <= normalize_cost(Decimal("1000000000000.123456")) <= 1


def test_blend_score_supports_half_confidence() -> None:
    assert blend_score(0.2, 0.8, 0.5) == pytest.approx(0.5)


def test_no_metrics_matches_static_router() -> None:
    candidates = [
        ModelCandidate("a", "a-model", 0.9, 0.4, 0.8, 0.7),
        ModelCandidate("b", "b-model", 0.7, 0.9, 0.6, 0.8),
    ]
    static_router, _ = make_router(candidates)
    dynamic_router, _ = make_router(candidates, {})

    static = static_router.rank("auto", RoutingPreferences())
    dynamic = dynamic_router.rank("auto", RoutingPreferences())

    assert [(item.candidate.provider, item.score) for item in dynamic] == [
        (item.candidate.provider, item.score) for item in static
    ]


def test_confidence_zero_prevents_measured_data_changing_order() -> None:
    candidates = [
        ModelCandidate("a", "a-model", 0.8, 0.8, 0.9, 0.9),
        ModelCandidate("b", "b-model", 0.8, 0.8, 0.7, 0.7),
    ]
    snapshots = {
        ("a", "a-model"): make_snapshot(
            "a", "a-model", sample_count=4, success_count=0, p50_latency_ms=30_000
        ),
        ("b", "b-model"): make_snapshot(
            "b", "b-model", sample_count=4, success_count=4, p50_latency_ms=100
        ),
    }
    router, _ = make_router(candidates, snapshots)

    ranked = router.rank("auto", RoutingPreferences(quality=0, cost=0, latency=1, reliability=1))

    assert [item.candidate.provider for item in ranked] == ["a", "b"]


def test_confidence_one_fully_applies_measured_signals() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.2, 0.2, 0.2)
    snapshot = make_snapshot(
        "a",
        "a-model",
        sample_count=50,
        success_count=50,
        p50_latency_ms=500,
        p50_estimated_cost=Decimal("0.0005"),
    )
    router, _ = make_router([candidate], {("a", "a-model"): snapshot})

    ranked, explanation = router.rank_with_explanation(
        "auto", RoutingPreferences(), "req_test"
    )
    signal = ranked[0].signal

    assert explanation is not None
    assert signal.latency_score == 1
    assert signal.cost_score == 1
    assert signal.reliability_score > candidate.reliability
    assert signal.sources["latency"] == "measured"


def test_partial_confidence_blends_each_measured_signal() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.2, 0.2, 0.2)
    snapshot = make_snapshot(
        "a",
        "a-model",
        sample_count=6,
        success_count=6,
        p50_latency_ms=500,
        p50_estimated_cost=Decimal("0.0005"),
    )
    router, _ = make_router([candidate], {("a", "a-model"): snapshot})

    signal = router.rank("auto", RoutingPreferences())[0].signal

    assert signal.latency_score == pytest.approx(round(blend_score(0.2, 1, 1 / 45), 6))
    assert signal.sources["latency"] == "blended"
    assert signal.measured_confidence == pytest.approx(round(1 / 45, 6))


@pytest.mark.parametrize(
    ("missing", "static_field", "source"),
    [
        ("latency", "latency_score", "static_unavailable"),
        ("reliability", "reliability_score", "static_unavailable"),
        ("cost", "cost_score", "static_unavailable"),
    ],
)
def test_missing_dimension_uses_its_static_baseline(
    missing: str, static_field: str, source: str
) -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.3, 0.4, 0.5)
    kwargs: dict[str, object] = {}
    if missing == "latency":
        kwargs["p50_latency_ms"] = None
    if missing == "reliability":
        kwargs["success_count"] = 50
    if missing == "cost":
        kwargs.update(p50_estimated_cost=None, priced_sample_count=0)
    snapshot = make_snapshot("a", "a-model", **kwargs)
    if missing == "reliability":
        snapshot = snapshot.model_copy(update={"success_rate": None})
    router, _ = make_router([candidate], {("a", "a-model"): snapshot})

    signal = router.rank("auto", RoutingPreferences())[0].signal

    assert getattr(signal, static_field) == getattr(candidate, missing)
    assert signal.sources[missing] == source


def test_other_measured_dimensions_still_apply_when_cost_is_missing() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.3, 0.3, 0.3)
    snapshot = make_snapshot(
        "a",
        "a-model",
        p50_latency_ms=500,
        p50_estimated_cost=None,
        priced_sample_count=0,
    )
    router, _ = make_router([candidate], {("a", "a-model"): snapshot})

    signal = router.rank("auto", RoutingPreferences())[0].signal

    assert signal.cost_score == candidate.cost
    assert signal.latency_score == 1
    assert signal.reliability_score > candidate.reliability


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        (RoutingPreferences(quality=1, cost=0, latency=0, reliability=0), "quality"),
        (RoutingPreferences(quality=0, cost=1, latency=0, reliability=0), "cost"),
        (RoutingPreferences(quality=0, cost=0, latency=1, reliability=0), "fast"),
        (RoutingPreferences(quality=0, cost=0, latency=0, reliability=1), "reliable"),
    ],
)
def test_user_preference_weights_select_expected_candidate(
    preference: RoutingPreferences, expected: str
) -> None:
    candidates = [
        ModelCandidate("quality", "quality-model", 1, 0.1, 0.1, 0.1),
        ModelCandidate("cost", "cost-model", 0.1, 1, 0.1, 0.1),
        ModelCandidate("fast", "fast-model", 0.1, 0.1, 0.1, 0.1),
        ModelCandidate("reliable", "reliable-model", 0.1, 0.1, 0.1, 0.1),
    ]
    snapshots = {
        ("quality", "quality-model"): make_snapshot(
            "quality", "quality-model", success_count=0, p50_estimated_cost=None
        ),
        ("cost", "cost-model"): make_snapshot(
            "cost",
            "cost-model",
            success_count=49,
            p50_latency_ms=30_000,
            p50_estimated_cost=Decimal("0.0005"),
        ),
        ("fast", "fast-model"): make_snapshot(
            "fast",
            "fast-model",
            success_count=0,
            p50_latency_ms=500,
            p50_estimated_cost=None,
        ),
        ("reliable", "reliable-model"): make_snapshot(
            "reliable",
            "reliable-model",
            success_count=50,
            p50_latency_ms=30_000,
            p50_estimated_cost=None,
        ),
    }
    router, _ = make_router(candidates, snapshots)

    assert router.rank("auto", preference)[0].candidate.provider == expected


def test_priced_sample_count_controls_cost_confidence_independently() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.2, 0.2, 0.2)
    snapshot = make_snapshot(
        "a",
        "a-model",
        sample_count=50,
        priced_sample_count=4,
        p50_estimated_cost=Decimal("0.0001"),
    )
    router, _ = make_router([candidate], {("a", "a-model"): snapshot})

    signal = router.rank("auto", RoutingPreferences())[0].signal

    assert signal.cost_score == candidate.cost
    assert signal.latency_score == 1
    assert signal.reason_metadata["cost_confidence"] == 0


def test_metrics_query_failure_uses_static_ranking(caplog: pytest.LogCaptureFixture) -> None:
    candidates = [
        ModelCandidate("a", "a-model", 0.9, 0.9, 0.9, 0.9),
        ModelCandidate("b", "b-model", 0.5, 0.5, 0.5, 0.5),
    ]
    store = SnapshotStore(fail=True)
    router, _ = make_router(candidates, store=store)

    ranked = router.rank("auto", RoutingPreferences())

    assert [item.candidate.provider for item in ranked] == ["a", "b"]
    assert all(item.signal.sources["latency"] == "static_unavailable" for item in ranked)
    assert "failed to read routing metrics" in caplog.text


def test_metrics_query_failure_does_not_fail_api_request() -> None:
    candidate = ModelCandidate("a", "a-model", 1, 1, 1, 1)
    store = SnapshotStore(fail=True)
    router, _ = make_router([candidate], store=store)
    gateway = GatewayService(router, RequestLogStore())
    request = ChatCompletionRequest(
        model="auto", messages=[{"role": "user", "content": "hello"}]
    )

    result = asyncio.run(gateway.complete(request))

    assert result["model"] == "a-model"


def test_routing_signal_and_explanation_are_structured_and_serializable() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.7, 0.6, 0.5)
    router, _ = make_router([candidate], {})

    ranked, explanation = router.rank_with_explanation(
        "auto", RoutingPreferences(), "req_test"
    )

    assert explanation is not None
    payload = json.loads(explanation.model_dump_json())
    assert ranked[0].signal.final_score == ranked[0].score
    assert payload["strategy"] == "weighted_measured_v1"
    assert payload["selected"]["provider"] == "a"
    assert payload["candidates"][0]["sources"]["quality"] == "configured"


def test_ranking_is_deterministic_and_candidate_order_independent() -> None:
    candidates = [
        ModelCandidate("zeta", "z-model", 0.8, 0.8, 0.8, 0.8),
        ModelCandidate("alpha", "a-model", 0.8, 0.8, 0.8, 0.8),
    ]
    first, _ = make_router(candidates, {})
    second, _ = make_router(list(reversed(candidates)), {})

    first_runs = [
        [item.candidate.provider for item in first.rank("auto", RoutingPreferences())]
        for _ in range(3)
    ]
    second_result = [
        item.candidate.provider for item in second.rank("auto", RoutingPreferences())
    ]

    assert first_runs == [["alpha", "zeta"]] * 3
    assert second_result == first_runs[0]


def test_explicit_model_uses_static_scoring_and_has_no_auto_explanation() -> None:
    candidate = ModelCandidate("a", "a-model", 0.8, 0.2, 0.2, 0.2)
    snapshot = make_snapshot("a", "a-model", p50_latency_ms=500)
    router, store = make_router([candidate], {("a", "a-model"): snapshot})

    ranked, explanation = router.rank_with_explanation(
        "a-model", RoutingPreferences(), "req_test"
    )

    assert ranked[0].score == pytest.approx(0.35)
    assert explanation is None
    assert store is not None and store.list_calls == 0


def test_each_candidate_snapshot_is_frozen_for_one_ranking() -> None:
    candidates = [
        ModelCandidate("a", "a-model", 0.5, 0.5, 0.5, 0.5),
        ModelCandidate("b", "b-model", 0.5, 0.5, 0.5, 0.5),
    ]
    store = SnapshotStore(
        {
            (candidate.provider, candidate.model): make_snapshot(
                candidate.provider, candidate.model
            )
            for candidate in candidates
        }
    )
    router, _ = make_router(candidates, store=store)

    router.rank("auto", RoutingPreferences())

    assert store.list_calls == 1
    assert store.queries == []


def test_final_scores_remain_in_unit_range() -> None:
    candidate = ModelCandidate("a", "a-model", 1, 1, 1, 1)
    router, _ = make_router(
        [candidate],
        {("a", "a-model"): make_snapshot("a", "a-model", p50_latency_ms=0)},
    )

    signal = router.rank("auto", RoutingPreferences())[0].signal

    assert 0 <= signal.final_score <= 1


@pytest.mark.parametrize("invalid_score", [-0.01, 1.01, float("nan")])
def test_candidate_rejects_out_of_range_static_scores(invalid_score: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        ModelCandidate("a", "a-model", invalid_score, 0.5, 0.5, 0.5)
