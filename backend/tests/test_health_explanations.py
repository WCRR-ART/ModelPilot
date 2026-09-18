import asyncio
import json

import pytest
from test_attempt_recording import NOW, REQUEST, OutcomeProvider, make_gateway, make_outcome
from test_half_open_probes import AT, outcome, setup
from test_health_aware_routing import HealthStore, gateway, opened
from test_routing_explanations import make_decision

from modelpilot.health import ProviderHealth
from modelpilot.metrics import (
    SCHEMA_VERSION,
    RoutingExplanation,
    SQLiteMetricsStore,
)
from modelpilot.providers import ProviderErrorType
from modelpilot.service import AllProvidersFailed, NoProviderAvailable


def explain(store, now=NOW):
    service, _ = gateway(store, now)
    return service.router.rank_with_explanation(
        "auto", REQUEST.modelpilot.preferences, "req-fixed"
    )[1]


@pytest.mark.parametrize("kind", ["closed", "missing", "unavailable", "open", "half_open"])
def test_health_fields_and_exclusion_semantics(kind):
    records = []
    if kind == "closed":
        records = [ProviderHealth.initial(provider="openai", model="openai-model", now=NOW)]
    if kind in {"open", "half_open"}:
        records = [opened()]
    exp = explain(
        HealthStore(records, fail=kind == "unavailable"), AT if kind == "half_open" else NOW
    )
    if kind == "open":
        item = exp.excluded_candidates[0]
        assert item.provider == "openai"
        assert not item.health.eligible
        assert item.health.reason == "circuit_open"
        assert item.health.cooldown_until == AT
        assert item.health.consecutive_failures == 1
        assert "rank" not in item.model_dump()
        assert "final_score" not in item.model_dump()
        assert [c.provider for c in exp.candidates] == ["gemini", "deepseek"]
        assert [c.rank for c in exp.candidates] == [1, 2]
        assert exp.selected_provider == "gemini"
    else:
        health = exp.candidates[0].health
        assert health.eligible
        assert (
            health.reason
            == {
                "closed": "healthy",
                "missing": "health_unknown",
                "unavailable": "health_store_unavailable",
                "half_open": "half_open_probe_eligible",
            }[kind]
        )
        assert health.state == (
            None if kind == "unavailable" else "HALF_OPEN" if kind == "half_open" else "CLOSED"
        )
        assert health.probe is False
    assert exp.routing_version == "v0.3"
    assert RoutingExplanation.model_validate_json(exp.model_dump_json()) == exp


def test_deterministic_explanation():
    store = HealthStore([opened()])
    assert explain(store) == explain(store)


def test_filtered_plus_fallback_selected_and_served(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    store.upsert_health(opened())
    providers = [
        OutcomeProvider("openai", []),
        OutcomeProvider(
            "gemini", [make_outcome("gemini", success=False, error_type=ProviderErrorType.TIMEOUT)]
        ),
        OutcomeProvider("deepseek", [make_outcome("deepseek")]),
    ]
    service = make_gateway(providers, store)
    service.router.health_store = store
    service.router.clock = lambda: NOW
    response = asyncio.run(service.complete(REQUEST))
    exp = RoutingExplanation.model_validate(response["modelpilot"]["routing"])
    assert exp.selected_provider == "gemini"
    assert exp.served_provider == "deepseek"
    assert exp.excluded_candidates[0].provider == "openai"
    assert providers[0].calls == 0
    reopened = SQLiteMetricsStore(store._database)
    assert reopened.get_routing_decision(exp.request_id).explanation == exp
    assert SCHEMA_VERSION == 4


@pytest.mark.parametrize("error", [None, ProviderErrorType.TIMEOUT])
def test_probe_explanation_reflects_actual_claim_and_persists(tmp_path, error):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    store.upsert_health(opened())
    service = setup(
        store,
        [
            OutcomeProvider("openai", [outcome(error=error)]),
            OutcomeProvider("gemini", [outcome("gemini")]),
        ],
    )
    response = asyncio.run(service.complete(REQUEST))
    exp = RoutingExplanation.model_validate(response["modelpilot"]["routing"])
    assert exp.selected.health.reason == "half_open_probe_acquired"
    assert exp.selected.health.probe is True
    assert exp.selected.health.state == "HALF_OPEN"
    assert exp.selected.health.cooldown_until == AT
    assert exp.served_provider == ("openai" if error is None else "gemini")
    assert (
        SQLiteMetricsStore(store._database).get_routing_decision(exp.request_id).explanation == exp
    )


def test_busy_probe_moves_to_excluded_without_score(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    store.upsert_health(opened())
    a = OutcomeProvider("openai", [])
    service = setup(store, [a, OutcomeProvider("gemini", [outcome("gemini")])])
    lease = service.probes.try_acquire("openai", "openai-model")
    try:
        response = asyncio.run(service.complete(REQUEST))
    finally:
        service.probes.release(lease)
    routing = response["modelpilot"]["routing"]
    excluded = routing["excluded_candidates"][0]
    assert excluded["health"]["reason"] == "half_open_probe_in_flight"
    assert excluded["health"]["eligible"] is False
    assert excluded["health"]["probe"] is False
    assert "rank" not in excluded and "final_score" not in excluded
    assert a.calls == 0
    assert routing["selected_provider"] == routing["served_provider"] == "gemini"
    assert len(routing["candidates"]) == 1
    assert routing["candidates"][0]["rank"] == 1
    persisted = store.get_routing_decision(routing["request_id"])
    assert persisted.explanation.model_dump(mode="json") == routing


@pytest.mark.parametrize("busy", [False, True])
def test_all_unavailable_keeps_internal_evidence_without_changing_error(tmp_path, busy):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    store.upsert_health(opened())
    service = setup(store, [OutcomeProvider("openai", [])])
    if busy:
        service.probes.try_acquire("openai", "openai-model")
    else:
        service.router.clock = lambda: NOW
    with pytest.raises(NoProviderAvailable) as caught:
        asyncio.run(service.complete(REQUEST))
    exp = caught.value.routing
    assert exp.selected is None
    assert exp.candidates == ()
    assert exp.excluded_candidates[0].health.reason == (
        "half_open_probe_in_flight" if busy else "circuit_open"
    )
    assert str(caught.value) == "no configured provider can serve the requested model"


def test_old_v02_json_without_new_fields_is_readable(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    decision = make_decision()
    store.record_routing_decision(decision)
    raw = decision.explanation.model_dump(mode="json")
    raw.pop("excluded_candidates")
    raw["selected"].pop("health")
    for candidate in raw["candidates"]:
        candidate.pop("health")
    with store._connect() as connection:
        connection.execute("UPDATE routing_decisions SET explanation_json = ?", (json.dumps(raw),))
        connection.commit()
    restored = SQLiteMetricsStore(store._database).get_routing_decision(decision.request_id)
    assert restored == decision
    assert restored.explanation.selected.health is None
    assert restored.routing_version == "v0.2"


def test_explicit_response_core_and_privacy(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    original = make_outcome("openai")
    original.response.update(created=123, usage={"total_tokens": 15})
    core = dict(original.response)
    service = setup(store, [OutcomeProvider("openai", [original])])
    response = asyncio.run(service.complete(REQUEST.model_copy(update={"model": "openai-model"})))
    assert "routing" not in response["modelpilot"]
    assert {k: response[k] for k in core} == core
    assert response["modelpilot"]["served_by"]["provider"] == "openai"


def test_auto_core_response_unchanged_and_health_failure_has_no_secrets(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    original = make_outcome("openai")
    original.response.update(created=123, usage={"total_tokens": 15})
    core = dict(original.response)
    service = setup(store, [OutcomeProvider("openai", [original])])
    service.router.health_store = HealthStore(fail=True)
    response = asyncio.run(service.complete(REQUEST))
    assert {k: response[k] for k in core} == core
    exp = response["modelpilot"]["routing"]
    assert exp["selected"]["health"]["state"] is None
    raw = json.dumps(exp)
    for secret in [
        "raw-secret",
        "private-token",
        "database/private/path",
        "Authorization",
        "Bearer",
        "prompt",
        "completion",
    ]:
        assert secret not in raw


def test_all_failed_probe_persists_acquired_evidence(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    store.upsert_health(opened())
    service = setup(store, [OutcomeProvider("openai", [outcome(error=ProviderErrorType.TIMEOUT)])])
    with pytest.raises(AllProvidersFailed) as caught:
        asyncio.run(service.complete(REQUEST))
    decision = store.get_routing_decision(caught.value.request_id)
    assert decision.explanation.selected.health.probe
    assert decision.served_provider is None
