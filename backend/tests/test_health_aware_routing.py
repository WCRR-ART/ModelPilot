import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import NOW, REQUEST, OutcomeProvider, make_gateway, make_outcome
from test_dynamic_router import SnapshotStore, make_snapshot

from modelpilot.health import CircuitState, ProviderHealth
from modelpilot.health.eligibility import health_eligibility
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.main import create_app
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers import ProviderErrorType
from modelpilot.router import ModelCandidate
from modelpilot.schemas import RoutingPreferences
from modelpilot.service import NoProviderAvailable


def opened(provider="openai", model="openai-model") -> ProviderHealth:
    return ProviderHealth.initial(provider=provider, model=model, now=NOW).record_failure(
        ProviderErrorType.TIMEOUT, failure_threshold=1, cooldown=timedelta(seconds=60), now=NOW
    )


class HealthStore:
    def __init__(self, records=(), fail=False):
        self.records = {(h.provider, h.model): h for h in records}
        self.calls = []
        self.fail = fail

    def get_health(self, provider, model):
        self.calls.append((provider, model))
        if self.fail:
            raise RuntimeError("api_key=raw-secret token=private-token database/private/path")
        return self.records.get((provider, model))


def gateway(store, now=NOW):
    providers = [
        OutcomeProvider(
            "openai", [make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)]
        ),
        OutcomeProvider("gemini", [make_outcome("gemini")]),
        OutcomeProvider("deepseek", [make_outcome("deepseek")]),
    ]
    result = make_gateway(providers, None)
    result.router.health_store = store
    result.router.clock = lambda: now
    return result, providers


@pytest.mark.parametrize(
    "offset,eligible,state",
    [
        (timedelta(seconds=60, microseconds=-1), False, CircuitState.OPEN),
        (timedelta(seconds=60), True, CircuitState.HALF_OPEN),
        (timedelta(seconds=60, microseconds=1), True, CircuitState.HALF_OPEN),
    ],
)
def test_cooldown_boundaries(offset, eligible, state):
    health = opened()
    evidence = health_eligibility(health, now=NOW + offset)
    assert evidence.eligible is eligible
    assert evidence.state is state
    assert evidence.reason == ("half_open_probe_eligible" if eligible else "circuit_open")
    assert health.state is CircuitState.OPEN


@pytest.mark.parametrize("record", [None, ProviderHealth.initial(provider="p", model="m", now=NOW)])
def test_closed_and_missing_eligible(record):
    result = health_eligibility(record, now=NOW)
    assert result.eligible
    assert result.state is CircuitState.CLOSED
    assert result.reason == ("health_unknown" if record is None else "healthy")


@pytest.mark.parametrize("mode", ["missing", "closed", "half_open", "unavailable"])
@pytest.mark.parametrize("dimension", ["quality", "cost", "latency", "reliability"])
def test_scores_and_order_match_v02_for_all_preferences(mode, dimension, caplog):
    service, _ = gateway(None, NOW + timedelta(seconds=60))
    router = service.router
    router.metrics = SnapshotStore(
        {
            ("openai", "openai-model"): make_snapshot(
                "openai",
                "openai-model",
                sample_count=25,
                p50_latency_ms=2000,
            )
        }
    )
    prefs = RoutingPreferences(
        **{key: int(key == dimension) for key in ["quality", "cost", "latency", "reliability"]}
    )
    expected = router.rank("auto", prefs)
    records = []
    for candidate in router.candidates:
        if mode == "closed":
            records.append(
                ProviderHealth.initial(provider=candidate.provider, model=candidate.model, now=NOW)
            )
        elif mode == "half_open":
            records.append(
                opened(candidate.provider, candidate.model).advance(now=NOW + timedelta(seconds=60))
            )
    router.health_store = HealthStore(records, fail=mode == "unavailable")
    for _ in range(2):
        actual = router.rank("auto", prefs)
        assert [(r.candidate, r.score) for r in actual] == [
            (r.candidate, r.score) for r in expected
        ]
        assert [r.signal.model_dump(exclude={"health"}) for r in actual] == [
            r.signal.model_dump(exclude={"health"}) for r in expected
        ]
    assert "raw-secret" not in caplog.text
    assert "private-token" not in caplog.text
    assert "database/private/path" not in caplog.text
    if mode == "unavailable":
        assert "fail-open" in caplog.text


def test_excluded_candidate_never_scored_or_called():
    service, providers = gateway(HealthStore([opened()]))
    original = service.router._score_candidate
    scored = []

    def score(candidate, weights, snapshot, quality_snapshot=None):
        scored.append(candidate.provider)
        return original(candidate, weights, snapshot, quality_snapshot)

    service.router._score_candidate = score
    assert asyncio.run(service.complete(REQUEST))["model"] == "gemini-model"
    assert scored == ["gemini", "deepseek"]
    assert providers[0].calls == 0
    evidence = service.router.evaluate_health(service.router.candidates, now=NOW)
    assert evidence[("openai", "openai-model")].reason == "circuit_open"
    assert "final_score" not in evidence[("openai", "openai-model")].model_dump()


def test_fallback_skips_open_middle_candidate():
    service, providers = gateway(HealthStore([opened("gemini", "gemini-model")]))
    assert asyncio.run(service.complete(REQUEST))["model"] == "deepseek-model"
    assert [p.calls for p in providers] == [1, 0, 1]


def test_all_open_uses_existing_503_path():
    service, providers = gateway(
        HealthStore([opened(p, f"{p}-model") for p in ["openai", "gemini", "deepseek"]])
    )
    with pytest.raises(NoProviderAvailable):
        asyncio.run(service.complete(REQUEST))
    with TestClient(create_app(gateway=service)) as client:
        result = client.post("/v1/chat/completions", json=REQUEST.model_dump(mode="json"))
    assert result.status_code == 503
    assert [p.calls for p in providers] == [0, 0, 0]


def test_explicit_open_model_keeps_existing_behavior():
    store = HealthStore([opened("gemini", "gemini-model")])
    service, providers = gateway(store)
    request = REQUEST.model_copy(update={"model": "gemini-model"})
    assert asyncio.run(service.complete(request))["model"] == "gemini-model"
    assert [p.calls for p in providers] == [0, 1, 0]
    assert store.calls == []
    with pytest.raises(NoProviderAvailable):
        asyncio.run(service.complete(REQUEST.model_copy(update={"model": "unknown/model"})))


def test_provider_and_model_scope():
    service, _ = gateway(HealthStore([opened()]))
    service.router.candidates.append(ModelCandidate("openai", "other-model", 1, 1, 1, 1))
    service.router.candidates.append(ModelCandidate("gemini", "openai-model", 1, 1, 1, 1))
    keys = {
        (r.candidate.provider, r.candidate.model)
        for r in service.router.rank("auto", REQUEST.modelpilot.preferences)
    }
    assert ("openai", "openai-model") not in keys
    assert ("openai", "other-model") in keys
    assert ("gemini", "openai-model") in keys


def test_one_clock_and_one_read_per_pair():
    store = HealthStore()
    service, _ = gateway(store)
    clocks = []

    def clock():
        clocks.append(NOW)
        return NOW

    service.router.clock = clock
    service.router.candidates += service.router.candidates[:1]
    ranked = service.router.rank("auto", REQUEST.modelpilot.preferences)
    assert len(clocks) == 1
    assert len(store.calls) == len(set(store.calls)) == len(ranked) == 3


def test_store_failure_still_serves_inference():
    service, _ = gateway(HealthStore(fail=True))
    assert asyncio.run(service.complete(REQUEST))["model"] == "gemini-model"


def test_threshold_then_skip_then_cooldown_recovery(tmp_path: Path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    failed = make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)
    recovered = make_outcome("openai").model_copy(
        update={
            "finished_at": NOW + timedelta(seconds=60),
        }
    )
    a = OutcomeProvider("openai", [failed, failed, failed, recovered])
    b = OutcomeProvider("gemini", [make_outcome("gemini") for _ in range(4)])
    service = make_gateway([a, b], store)
    service.health = ProviderHealthManager(
        store, failure_threshold=3, cooldown=timedelta(seconds=60)
    )
    service.router.health_store = store
    service.router.clock = lambda: NOW
    service.router.candidates.append(service.router.candidates[0])
    for _ in range(3):
        assert asyncio.run(service.complete(REQUEST))["model"] == "gemini-model"
    assert a.calls == 3
    assert store.get_health("openai", "openai-model").state is CircuitState.OPEN
    assert asyncio.run(service.complete(REQUEST))["model"] == "gemini-model"
    assert a.calls == 3
    assert (
        SQLiteMetricsStore(store._database).get_health("openai", "openai-model").state
        is CircuitState.OPEN
    )
    service.router.clock = lambda: NOW + timedelta(seconds=60)
    assert asyncio.run(service.complete(REQUEST))["model"] == "openai-model"
    assert a.calls == 4
    assert store.get_health("openai", "openai-model").state is CircuitState.CLOSED
