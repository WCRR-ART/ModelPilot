import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_attempt_recording import NOW, REQUEST, OutcomeProvider, make_gateway, make_outcome

from modelpilot.config import Settings
from modelpilot.health import CircuitState, ProviderHealth
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.main import build_gateway
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers import ProviderErrorType
from modelpilot.service import AllProvidersFailed


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMetricsStore:
    return SQLiteMetricsStore(tmp_path / "test.db")


def manager(store: SQLiteMetricsStore, threshold: int = 3, seconds: int = 60):
    return ProviderHealthManager(
        store, failure_threshold=threshold, cooldown=timedelta(seconds=seconds)
    )


def failure(provider="openai", error=ProviderErrorType.TIMEOUT):
    return make_outcome(provider, success=False, error_type=error)


def test_initial_success_and_failure_reset(store):
    updater = manager(store)
    first = updater.update(make_outcome("openai"))
    assert first.state is CircuitState.CLOSED
    assert first.last_success_at == NOW
    assert updater.update(failure()).consecutive_failures == 1
    reset = updater.update(make_outcome("openai"))
    assert reset.consecutive_failures == 0
    assert reset.updated_at == NOW
    assert reset.updated_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("error", list(ProviderErrorType))
def test_error_classification(store, error):
    health = manager(store).update(failure(error=error))
    assert health.consecutive_failures == (
        0 if error is ProviderErrorType.AUTHENTICATION_ERROR else 1
    )


@pytest.mark.parametrize("success", [True, False])
def test_half_open_result(store, success):
    opened = manager(store, threshold=1, seconds=0).update(failure())
    store.upsert_health(opened.advance(now=NOW))
    result = manager(store).update(make_outcome("openai") if success else failure())
    assert result.state is (CircuitState.CLOSED if success else CircuitState.OPEN)
    assert result.consecutive_failures == (0 if success else 2)
    assert result.cooldown_until == (None if success else NOW + timedelta(seconds=60))


def test_threshold_reopen_and_router_still_calls_open_provider(store, caplog):
    provider = OutcomeProvider("openai", [failure(), failure(), failure(), make_outcome("openai")])
    gateway = make_gateway([provider], store)
    gateway.health = manager(store)
    for count in range(1, 4):
        with pytest.raises(AllProvidersFailed):
            asyncio.run(gateway.complete(REQUEST))
        health = store.get_health("openai", "openai-model")
        assert health.consecutive_failures == count
        assert health.state is (CircuitState.OPEN if count == 3 else CircuitState.CLOSED)
    reopened = SQLiteMetricsStore(store._database)
    assert reopened.get_health("openai", "openai-model") == health
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(gateway.complete(REQUEST))
    assert result["model"] == "openai-model"
    assert provider.calls == 4
    assert store.get_health("openai", "openai-model") == health
    assert "health transition rejected" in caplog.text


def test_fallback_and_auth_attempt_observability(store):
    providers = [
        OutcomeProvider("openai", [failure(error=ProviderErrorType.AUTHENTICATION_ERROR)]),
        OutcomeProvider("gemini", [failure("gemini", ProviderErrorType.RATE_LIMIT)]),
        OutcomeProvider("deepseek", [make_outcome("deepseek")]),
    ]
    gateway = make_gateway(providers, store)
    gateway.health = manager(store)
    result = asyncio.run(gateway.complete(REQUEST))
    assert result["model"] == "deepseek-model"
    assert store.get_health("openai", "openai-model").consecutive_failures == 0
    assert store.get_health("gemini", "gemini-model").consecutive_failures == 1
    assert store.get_health("deepseek", "deepseek-model").last_success_at == NOW
    attempts = store.get_recent_attempts("openai", "openai-model", 10, NOW)
    assert attempts[0].error_type == "authentication_error"


def test_models_are_isolated(store):
    updater = manager(store, threshold=1)
    updater.update(failure())
    other = make_outcome("openai").model_copy(update={"model": "other-model"})
    updater.update(other)
    assert store.get_health("openai", "openai-model").state is CircuitState.OPEN
    assert store.get_health("openai", "other-model").state is CircuitState.CLOSED


@pytest.mark.parametrize("metrics_fail,health_fail", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("success", [True, False])
def test_independent_persistence_failures(
    store, monkeypatch, caplog, metrics_fail, health_fail, success
):
    def broken_metrics(*args):
        raise RuntimeError("token=metrics-secret")

    def broken_health(*args):
        raise RuntimeError("raw-api-key raw-token sensitive/database/path")

    if metrics_fail:
        monkeypatch.setattr(store, "record_attempt", broken_metrics)
    if health_fail:
        monkeypatch.setattr(store, "upsert_health", broken_health)
    first = OutcomeProvider("openai", [make_outcome("openai") if success else failure()])
    second = OutcomeProvider("gemini", [make_outcome("gemini")])
    gateway = make_gateway([first, second], store)
    gateway.health = manager(store)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(gateway.complete(REQUEST))
    assert result["model"] == ("openai-model" if success else "gemini-model")
    assert first.calls == 1
    assert second.calls == (0 if success else 1)
    assert bool(store.get_recent_attempts("openai", "openai-model", 10, NOW)) is not metrics_fail
    assert (store.get_health("openai", "openai-model") is None) is health_fail
    assert "raw-api-key" not in caplog.text
    assert "raw-token" not in caplog.text
    assert "sensitive/database/path" not in caplog.text
    assert "metrics-secret" not in caplog.text
    if health_fail:
        assert "health persistence failed" in caplog.text


def test_health_read_failure_is_isolated(store, monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("secret read error")

    monkeypatch.setattr(store, "get_health", broken)
    gateway = make_gateway([OutcomeProvider("openai", [make_outcome("openai")])], store)
    gateway.health = manager(store)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(gateway.complete(REQUEST))["model"] == "openai-model"
    assert "health persistence failed" in caplog.text
    assert "secret read error" not in caplog.text


@pytest.mark.parametrize("threshold,seconds", [(1, 12), (2, 0)])
def test_environment_and_application_wiring(store, monkeypatch, threshold, seconds):
    monkeypatch.setenv("MODELPILOT_CIRCUIT_FAILURE_THRESHOLD", str(threshold))
    monkeypatch.setenv("MODELPILOT_CIRCUIT_COOLDOWN_SECONDS", str(seconds))
    settings = Settings.from_env()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as c:
            return build_gateway(settings, c, store)

    gateway = asyncio.run(run())
    assert gateway.health.store is store
    for _ in range(threshold):
        health = gateway.health.update(failure())
    assert health.state is CircuitState.OPEN
    assert health.cooldown_until == NOW + timedelta(seconds=seconds)
    if seconds == 0:
        assert gateway.health.update(make_outcome("openai")).state is CircuitState.CLOSED


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODELPILOT_CIRCUIT_FAILURE_THRESHOLD", "0"),
        ("MODELPILOT_CIRCUIT_FAILURE_THRESHOLD", "-1"),
        ("MODELPILOT_CIRCUIT_FAILURE_THRESHOLD", "1.5"),
        ("MODELPILOT_CIRCUIT_COOLDOWN_SECONDS", "-1"),
        ("MODELPILOT_CIRCUIT_COOLDOWN_SECONDS", "nan"),
    ],
)
def test_invalid_config_fails_at_load(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_defaults():
    assert Settings().circuit_failure_threshold == 3
    assert Settings().circuit_cooldown_seconds == 60


def test_open_failure_rejected_without_filtering_or_ranking_changes(store, caplog):
    opened = manager(store, threshold=1).update(failure())
    providers = [
        OutcomeProvider("openai", [failure()]),
        OutcomeProvider("gemini", [make_outcome("gemini")]),
    ]
    gateway = make_gateway(providers, store)
    before = gateway.router.rank("auto", REQUEST.modelpilot.preferences)
    gateway.health = manager(store)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(gateway.complete(REQUEST))
    assert result["model"] == "gemini-model"
    assert providers[0].calls == 1
    assert store.get_health("openai", "openai-model") == opened
    assert "health transition rejected" in caplog.text
    assert gateway.router.rank("auto", REQUEST.modelpilot.preferences) == before


def test_default_application_wires_shared_sqlite_and_records_success(tmp_path):
    settings = Settings(
        metrics_db_path=tmp_path / "nested" / "modelpilot.db",
        openai_api_key="fake-key",
    )

    async def run():
        payload = {"id": "fake", "object": "chat.completion", "choices": []}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ) as client:
            gateway = build_gateway(settings, client)
            assert gateway.health.store is gateway.metrics
            await gateway.complete(REQUEST)

    asyncio.run(run())
    health = SQLiteMetricsStore(settings.metrics_db_path).get_health(
        "openai", settings.openai_model
    )
    assert health.state is CircuitState.CLOSED
    assert health.last_success_at is not None


def test_deterministic_completion_time_and_rejected_stale_outcome(store):
    outcome = make_outcome("openai")
    updater = manager(store)
    first = updater.update(outcome)
    assert updater.update(outcome) == first
    store.upsert_health(
        ProviderHealth.initial(
            provider="openai", model="openai-model", now=NOW + timedelta(seconds=1)
        )
    )
    with pytest.raises(ValueError, match="earlier than updated_at"):
        updater.update(outcome)
