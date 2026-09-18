import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import NOW, REQUEST, OutcomeProvider, make_gateway, make_outcome
from test_health_aware_routing import opened

from modelpilot.config import Settings
from modelpilot.health import CircuitState, ProviderHealth
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.main import build_gateway, create_app
from modelpilot.metrics import SCHEMA_VERSION, SQLiteMetricsStore
from modelpilot.providers import ProviderError, ProviderErrorType
from modelpilot.service import AllProvidersFailed

AT = NOW + timedelta(seconds=60)


def outcome(name="openai", error=None):
    return make_outcome(name, success=error is None, error_type=error).model_copy(
        update={"started_at": AT, "finished_at": AT}
    )


def setup(store, providers):
    service = make_gateway(providers, store)
    service.router.health_store = store
    service.router.clock = lambda: AT
    service.health = ProviderHealthManager(
        store, failure_threshold=3, cooldown=timedelta(seconds=60)
    )
    service.probes = HalfOpenProbeCoordinator()
    return service


@pytest.fixture
def store(tmp_path: Path):
    result = SQLiteMetricsStore(tmp_path / "probes.db")
    result.upsert_health(opened())
    return result


@pytest.mark.parametrize("error", [None, ProviderErrorType.TIMEOUT])
def test_twenty_concurrent_requests_single_probe(store, error):
    async def run():
        fallback_done = asyncio.Event()
        active = 0
        maximum = 0

        class Probe(OutcomeProvider):
            async def complete(self, request, model):
                nonlocal active, maximum
                self.calls += 1
                active += 1
                maximum = max(maximum, active)
                try:
                    await fallback_done.wait()
                    return outcome(error=error)
                finally:
                    active -= 1

        class Backup(OutcomeProvider):
            async def complete(self, request, model):
                self.calls += 1
                if self.calls == 19:
                    fallback_done.set()
                return outcome("gemini")

        a, b = Probe("openai", []), Backup("gemini", [])
        service = setup(store, [a, b])
        results = await asyncio.wait_for(
            asyncio.gather(*(service.complete(REQUEST) for _ in range(20))), timeout=5
        )
        assert a.calls == maximum == 1
        assert b.calls == (19 if error is None else 20)
        assert len(results) == 20
        health = store.get_health("openai", "openai-model")
        assert health.state is (CircuitState.CLOSED if error is None else CircuitState.OPEN)
        if error is None:
            assert health.consecutive_failures == 0
            await service.complete(REQUEST)
            assert a.calls == 2
        else:
            assert health.cooldown_until == AT + timedelta(seconds=60)
            await service.complete(REQUEST)
            assert a.calls == 1
        lease = service.probes.try_acquire("openai", "openai-model")
        assert lease is not None
        service.probes.release(lease)

    asyncio.run(run())


@pytest.mark.parametrize("error", list(ProviderErrorType))
def test_normalized_failure_releases_and_preserves_classification(store, error):
    a = OutcomeProvider("openai", [outcome(error=error)])
    service = setup(store, [a])
    with pytest.raises(AllProvidersFailed):
        asyncio.run(service.complete(REQUEST))
    health = store.get_health("openai", "openai-model")
    if error is ProviderErrorType.AUTHENTICATION_ERROR:
        assert health.state is CircuitState.HALF_OPEN
    else:
        assert health.state is CircuitState.OPEN
        assert health.cooldown_until == AT + timedelta(seconds=60)
    assert service.probes.try_acquire("openai", "openai-model") is not None


@pytest.mark.parametrize("error", [RuntimeError, ValueError, ProviderError])
def test_unexpected_or_legacy_provider_exception_releases(store, error):
    class Broken(OutcomeProvider):
        async def complete(self, request, model):
            raise error("injected")

    service = setup(store, [Broken("openai", [])])
    expected = AllProvidersFailed if error is ProviderError else error
    with pytest.raises(expected):
        asyncio.run(service.complete(REQUEST))
    assert service.probes.try_acquire("openai", "openai-model") is not None


def test_cancellation_releases(store):
    async def run():
        entered = asyncio.Event()
        blocker = asyncio.Event()

        class Blocked(OutcomeProvider):
            async def complete(self, request, model):
                entered.set()
                await blocker.wait()

        service = setup(store, [Blocked("openai", [])])
        task = asyncio.create_task(service.complete(REQUEST))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert service.probes.try_acquire("openai", "openai-model") is not None

    asyncio.run(run())


@pytest.mark.parametrize("method", ["record_attempt", "upsert_health"])
def test_persistence_failure_releases_and_returns_success(store, monkeypatch, method):
    def broken(*args):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(store, method, broken)
    service = setup(store, [OutcomeProvider("openai", [outcome()])])
    assert asyncio.run(service.complete(REQUEST))["model"] == "openai-model"
    assert service.probes.try_acquire("openai", "openai-model") is not None


def test_busy_no_alternatives_returns_503(store):
    a = OutcomeProvider("openai", [])
    service = setup(store, [a])
    lease = service.probes.try_acquire("openai", "openai-model")
    with TestClient(create_app(gateway=service)) as client:
        response = client.post("/v1/chat/completions", json=REQUEST.model_dump(mode="json"))
    assert response.status_code == 503
    assert a.calls == 0
    service.probes.release(lease)


def test_candidate_not_called_does_not_claim(store):
    store.upsert_health(opened("gemini", "gemini-model"))
    store.upsert_health(ProviderHealth.initial(provider="openai", model="openai-model", now=NOW))
    service = setup(store, [OutcomeProvider("openai", [outcome()]), OutcomeProvider("gemini", [])])
    assert asyncio.run(service.complete(REQUEST))["model"] == "openai-model"
    assert service.probes.try_acquire("gemini", "gemini-model") is not None


@pytest.mark.parametrize("state", ["closed", "open_before", "explicit", "store_failure"])
def test_paths_that_do_not_claim(store, monkeypatch, state):
    a = OutcomeProvider("openai", [outcome()])
    b = OutcomeProvider("gemini", [outcome("gemini")])
    service = setup(store, [a, b])

    def unexpected(*args):
        pytest.fail("must not acquire probe")

    monkeypatch.setattr(service.probes, "try_acquire", unexpected)
    request = REQUEST
    if state == "closed":
        store.upsert_health(
            ProviderHealth.initial(provider="openai", model="openai-model", now=NOW)
        )
    elif state == "open_before":
        service.router.clock = lambda: NOW
    elif state == "explicit":
        request = REQUEST.model_copy(update={"model": "openai-model"})
    else:

        def broken(*args):
            raise RuntimeError("health unavailable")

        monkeypatch.setattr(store, "get_health", broken)
    result = asyncio.run(service.complete(request))
    assert result["model"] == ("gemini-model" if state == "open_before" else "openai-model")


def test_independent_keys_can_hold_leases_concurrently():
    async def run():
        coordinator = HalfOpenProbeCoordinator()
        all_claimed = asyncio.Event()
        keys = [("openai", "a"), ("openai", "b"), ("gemini", "a")]
        claimed = []

        async def probe(key):
            lease = coordinator.try_acquire(*key)
            assert lease is not None
            claimed.append(key)
            if len(claimed) == 3:
                all_claimed.set()
            try:
                await all_claimed.wait()
            finally:
                coordinator.release(lease)

        await asyncio.wait_for(asyncio.gather(*(probe(k) for k in keys)), timeout=2)
        assert set(claimed) == set(keys)

    asyncio.run(run())


def test_stale_release_cannot_release_new_owner():
    coordinator = HalfOpenProbeCoordinator()
    first = coordinator.try_acquire("p", "m")
    coordinator.release(first)
    second = coordinator.try_acquire("p", "m")
    coordinator.release(first)
    assert coordinator.try_acquire("p", "m") is None
    coordinator.release(second)
    assert coordinator.try_acquire("p", "m") is not None


def test_application_shared_coordinator_and_no_schema_change(store):
    async def run():
        async with httpx.AsyncClient() as client:
            service = build_gateway(Settings(), client, store)
            assert isinstance(service.probes, HalfOpenProbeCoordinator)
            with TestClient(create_app(gateway=service)) as app_client:
                coordinator = app_client.app.state.gateway.probes
                app_client.get("/health")
                app_client.get("/health")
                assert app_client.app.state.gateway.probes is coordinator

    asyncio.run(run())
    assert SCHEMA_VERSION == 4
    assert SQLiteMetricsStore(store._database).get_health("openai", "openai-model") == opened()
    with store._connect() as connection:
        assert "probe_in_flight" not in {
            row[1] for row in connection.execute("PRAGMA table_info(provider_health)")
        }


def test_response_processing_exception_does_not_leak(store):
    invalid = outcome().model_copy(update={"response": None})
    service = setup(store, [OutcomeProvider("openai", [invalid])])
    with pytest.raises(RuntimeError, match="did not contain a response"):
        asyncio.run(service.complete(REQUEST))
    assert service.probes.try_acquire("openai", "openai-model") is not None


def test_old_ranking_rechecks_new_cooldown_before_claim(store, monkeypatch):
    a = OutcomeProvider("openai", [])
    b = OutcomeProvider("gemini", [outcome("gemini")])
    service = setup(store, [a, b])
    ranked, explanation = service.router.rank_with_explanation(
        "auto", REQUEST.modelpilot.preferences, "old-ranking"
    )
    store.upsert_health(
        opened().record_failure(
            ProviderErrorType.TIMEOUT, failure_threshold=3, cooldown=timedelta(seconds=60), now=AT
        )
    )
    monkeypatch.setattr(
        service.router, "rank_with_explanation", lambda *args: (ranked, explanation)
    )
    assert asyncio.run(service.complete(REQUEST))["model"] == "gemini-model"
    assert a.calls == 0


def test_atomic_claim_from_concurrent_threads():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    coordinator = HalfOpenProbeCoordinator()
    barrier = Barrier(20)

    def claim(_):
        barrier.wait(timeout=5)
        return coordinator.try_acquire("gemini", "model")

    with ThreadPoolExecutor(max_workers=20) as pool:
        leases = list(pool.map(claim, range(20)))
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    coordinator.release(winners[0])
