from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import NOW, OutcomeProvider, make_gateway
from test_health_aware_routing import opened

from modelpilot.health import ProviderHealth
from modelpilot.health.eligibility import health_eligibility
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.main import create_app
from modelpilot.metrics import SCHEMA_VERSION, SQLiteMetricsStore

ENDPOINT = "/v1/health/providers"


@pytest.fixture
def context(tmp_path):
    store = SQLiteMetricsStore(tmp_path / "health.db")
    providers = [OutcomeProvider(name, []) for name in ["openai", "gemini", "deepseek"]]
    service = make_gateway(providers, store)
    service.router.health_store = store
    service.router.clock = lambda: NOW
    service.probes = HalfOpenProbeCoordinator()
    with TestClient(create_app(gateway=service)) as client:
        yield store, service, client


def test_configured_missing_records_sorted_and_nullable(context):
    _, _, client = context
    response = client.get(ENDPOINT)
    assert response.status_code == 200
    rows = response.json()
    assert [r["provider"] for r in rows] == ["deepseek", "gemini", "openai"]
    for row in rows:
        assert row["state"] == "CLOSED"
        assert row["reason"] == "health_unknown"
        assert row["eligible"] is True
        assert row["consecutive_failures"] == 0
        assert row["probe_in_flight"] is False
        for field in [
            "opened_at",
            "cooldown_until",
            "last_failure_at",
            "last_success_at",
            "updated_at",
        ]:
            assert row[field] is None
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_get_only(context, method):
    assert getattr(context[2], method)(ENDPOINT).status_code == 405


@pytest.mark.parametrize(
    "seconds,state,reason",
    [
        (59, "OPEN", "circuit_open"),
        (60, "HALF_OPEN", "half_open_probe_eligible"),
        (61, "HALF_OPEN", "half_open_probe_eligible"),
    ],
)
def test_effective_state_boundaries_read_only(context, monkeypatch, seconds, state, reason):
    store, service, client = context
    health = opened()
    store.upsert_health(health)
    now = NOW + timedelta(seconds=seconds)
    service.router.clock = lambda: now
    before = store.list_health()

    def forbidden(*args, **kwargs):
        pytest.fail("GET must not write health or acquire/release a probe")

    monkeypatch.setattr(store, "upsert_health", forbidden)
    monkeypatch.setattr(service.probes, "try_acquire", forbidden)
    monkeypatch.setattr(service.probes, "release", forbidden)
    for _ in range(2):
        row = client.get(ENDPOINT).json()[2]
        assert row["state"] == state
        assert row["reason"] == reason
        assert row["eligible"] == (state != "OPEN")
        assert row["consecutive_failures"] == 1
        assert row["probe_in_flight"] is False
        expected = health_eligibility(health, now=now).model_dump(mode="json")
        assert all(row[key] == value for key, value in expected.items())
        for field in ["opened_at", "cooldown_until", "last_failure_at", "updated_at"]:
            value = datetime.fromisoformat(row[field])
            assert value.utcoffset() == timedelta(0)
            assert value == getattr(health, field)
        assert row["last_success_at"] is None
    assert store.list_health() == before
    assert SCHEMA_VERSION == 3


def test_closed_success_timestamps(context):
    store, _, client = context
    health = ProviderHealth.initial(provider="openai", model="openai-model", now=NOW)
    store.upsert_health(health.record_success(now=NOW))
    row = client.get(ENDPOINT).json()[2]
    assert row["state"] == "CLOSED"
    assert row["reason"] == "healthy"
    assert row["eligible"] is True
    assert datetime.fromisoformat(row["last_success_at"]) == NOW
    assert row["last_failure_at"] is None
    assert row["cooldown_until"] is None


def test_probe_query_preserves_existing_lease(context, monkeypatch):
    store, service, client = context
    now = NOW + timedelta(seconds=60)
    store.upsert_health(opened().advance(now=now))
    service.router.clock = lambda: now
    lease = service.probes.try_acquire("openai", "openai-model")

    def forbidden(*args, **kwargs):
        pytest.fail("GET must not change leases")

    with monkeypatch.context() as patch:
        patch.setattr(service.probes, "try_acquire", forbidden)
        patch.setattr(service.probes, "release", forbidden)
        row = client.get(ENDPOINT).json()[2]
    assert row["state"] == "HALF_OPEN"
    assert row["probe_in_flight"] is True
    assert row["eligible"] is False
    assert row["reason"] == "half_open_probe_in_flight"
    assert service.probes.try_acquire("openai", "openai-model") is None
    service.probes.release(lease)
    row = client.get(ENDPOINT).json()[2]
    assert row["probe_in_flight"] is False
    assert row["eligible"] is True
    assert service.probes.try_acquire("openai", "openai-model") is not None


def test_disabled_providers_and_stale_records_excluded(context):
    store, service, client = context
    store.upsert_health(opened())
    service.router.providers["openai"].api_key = ""
    assert [r["provider"] for r in client.get(ENDPOINT).json()] == ["deepseek", "gemini"]
    service.router.providers.clear()
    assert client.get(ENDPOINT).json() == []


@pytest.mark.parametrize("missing_store", [False, True])
def test_store_failure_is_explicit_and_sanitized(context, monkeypatch, caplog, missing_store):
    store, service, client = context
    secret = "Authorization: Bearer private-secret /private/path/health.db"

    def fail(*args):
        raise RuntimeError(secret)

    if missing_store:
        service.router.health_store = None
    else:
        monkeypatch.setattr(store, "get_health", fail)
    response = client.get(ENDPOINT)
    assert response.status_code == 503
    assert response.json() == {"detail": "provider health unavailable"}
    assert "provider health" in caplog.text
    assert secret not in response.text + caplog.text
    assert "Traceback" not in caplog.text


def test_response_field_allowlist(context):
    row = context[2].get(ENDPOINT).json()[0]
    assert set(row) == {
        "provider",
        "model",
        "state",
        "eligible",
        "reason",
        "probe",
        "consecutive_failures",
        "opened_at",
        "cooldown_until",
        "last_failure_at",
        "last_success_at",
        "updated_at",
        "probe_in_flight",
    }
    assert "test-key" not in str(row)


def test_coordinator_concurrent_reads_are_side_effect_free():
    coordinator = HalfOpenProbeCoordinator()
    lease = coordinator.try_acquire("openai", "model")
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: coordinator.is_in_flight("openai", "model"), range(100)))
    assert all(results)
    assert not coordinator.is_in_flight("gemini", "model")
    assert coordinator.try_acquire("openai", "model") is None
    coordinator.release(lease)
    assert not coordinator.is_in_flight("openai", "model")


def test_sorting_includes_model_and_deduplicates(context):
    _, service, client = context
    from dataclasses import replace

    candidate = service.router.candidates[0]
    service.router.candidates.extend([replace(candidate, model="a-model"), candidate])
    rows = client.get(ENDPOINT).json()
    keys = [(row["provider"], row["model"]) for row in rows]
    assert keys == sorted(set(keys))
