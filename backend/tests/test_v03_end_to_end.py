"""Release gates use fake outcomes only; no external provider calls or sleeps."""

import asyncio
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import OutcomeProvider, make_gateway, make_outcome

from modelpilot import __version__
from modelpilot.health import CircuitState
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.main import create_app
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers import ProviderErrorType

START = datetime.now(UTC) - timedelta(minutes=5)
CHAT = "/v1/chat/completions"
HEALTH = "/v1/health/providers"
PAYLOAD = {
    "model": "auto",
    "messages": [{"role": "user", "content": "synthetic release gate"}],
    "modelpilot": {"preferences": {"quality": 1, "latency": 0, "cost": 0, "reliability": 0}},
}


class Clock:
    now = START

    def __call__(self):
        return self.now


class FakeProvider(OutcomeProvider):
    def __init__(self, name, clock):
        super().__init__(name, [])
        self.clock = clock
        self.error = None

    async def complete(self, request, model):
        self.calls += 1
        return make_outcome(
            self.name, success=self.error is None, error_type=self.error
        ).model_copy(
            update={
                "model": model,
                "started_at": self.clock.now - timedelta(milliseconds=125.5),
                "finished_at": self.clock.now,
            }
        )


def wire(store, providers, clock):
    service = make_gateway(providers, store)
    service.router.metrics = store
    service.router.health_store = store
    service.router.clock = clock
    service.health = ProviderHealthManager(
        store, failure_threshold=3, cooldown=timedelta(seconds=60)
    )
    service.probes = HalfOpenProbeCoordinator()
    return service


def open_through_http(client, a, store):
    a.error = ProviderErrorType.TIMEOUT
    for count in range(1, 4):
        response = client.post(CHAT, json=PAYLOAD)
        assert response.status_code == 200
        extension = response.json()["modelpilot"]
        assert extension["routing"]["selected_provider"] == "openai"
        assert extension["served_by"]["provider"] == "gemini"
        health = store.get_health("openai", "openai-model")
        assert health.consecutive_failures == count
        assert health.state == (CircuitState.OPEN if count == 3 else CircuitState.CLOSED)
        row = client.get(HEALTH).json()[1]
        assert row["state"] == health.state
        assert row["consecutive_failures"] == count
    assert health.opened_at == health.last_failure_at == START
    assert health.cooldown_until == START + timedelta(seconds=60)
    return health


@pytest.mark.parametrize("recovery_error", [None, ProviderErrorType.TIMEOUT])
def test_http_threshold_restart_isolation_and_recovery(tmp_path, recovery_error):
    clock = Clock()
    path = tmp_path / "release.db"
    store = SQLiteMetricsStore(path)
    a, b = FakeProvider("openai", clock), FakeProvider("gemini", clock)
    service = wire(store, [a, b], clock)
    with TestClient(create_app(gateway=service)) as client:
        opened = open_through_http(client, a, store)

    # Every SQLite operation closes its connection; reconstruct store AND service.
    store = SQLiteMetricsStore(path)
    service = wire(store, [a, b], clock)
    assert store.get_health("openai", "openai-model") == opened
    with TestClient(create_app(gateway=service)) as client:
        clock.now = START + timedelta(seconds=59)
        response = client.post(CHAT, json=PAYLOAD)
        excluded = response.json()["modelpilot"]["routing"]["excluded_candidates"][0]
        assert excluded["health"]["reason"] == "circuit_open"
        assert excluded.get("rank") is None and excluded.get("final_score") is None
        assert a.calls == 3
        for offset in [60, 61]:
            clock.now = START + timedelta(seconds=offset)
            row = client.get(HEALTH).json()[1]
            assert row["state"] == "HALF_OPEN" and row["eligible"]
            assert store.get_health("openai", "openai-model") == opened
        a.error = recovery_error
        response = client.post(CHAT, json=PAYLOAD)
        assert response.status_code == 200
        route = response.json()["modelpilot"]["routing"]
        assert route["selected"]["health"]["probe"] is True
        assert route["selected"]["health"]["reason"] == "half_open_probe_acquired"
        assert a.calls == 4
        assert not service.probes.is_in_flight("openai", "openai-model")
        health = SQLiteMetricsStore(path).get_health("openai", "openai-model")
        if recovery_error is None:
            assert health.state is CircuitState.CLOSED
            assert health.consecutive_failures == 0
            assert health.last_success_at == clock.now
            assert client.post(CHAT, json=PAYLOAD).json()["modelpilot"]["provider"] == "openai"
            assert a.calls == 5
        else:
            assert health.state is CircuitState.OPEN
            assert health.cooldown_until == clock.now + timedelta(seconds=60)
            assert health.last_failure_at == clock.now
            assert health.consecutive_failures == 4
            assert client.post(CHAT, json=PAYLOAD).json()["modelpilot"]["provider"] == "gemini"
            assert a.calls == 4
        for endpoint in ["summary", "providers", "routing-decisions", "failures"]:
            assert client.get(f"/v1/metrics/{endpoint}").status_code == 200
        attempts = store.get_recent_attempts("openai", "openai-model", 100, START - timedelta(1))
        assert len(attempts) == a.calls
        assert sum(not attempt.success for attempt in attempts) == (3 if not recovery_error else 4)


def test_twenty_http_requests_share_one_recovery_probe(tmp_path):
    async def run():
        clock = Clock()
        store = SQLiteMetricsStore(tmp_path / "concurrent.db")
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        class BlockingRecovery(FakeProvider):
            async def complete(self, request, model):
                nonlocal active, maximum
                if self.error is None:
                    active += 1
                    maximum = max(maximum, active)
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        active -= 1
                return await super().complete(request, model)

        a, b = BlockingRecovery("openai", clock), FakeProvider("gemini", clock)
        service = wire(store, [a, b], clock)
        app = create_app(gateway=service)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            a.error = ProviderErrorType.TIMEOUT
            for _ in range(3):
                assert (await client.post(CHAT, json=PAYLOAD)).status_code == 200
            clock.now = START + timedelta(seconds=60)
            a.error = None
            first = asyncio.create_task(client.post(CHAT, json=PAYLOAD))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                row = (await client.get(HEALTH)).json()[1]
                assert row["probe_in_flight"] and not row["eligible"]
                others = await asyncio.wait_for(
                    asyncio.gather(*(client.post(CHAT, json=PAYLOAD) for _ in range(19))),
                    timeout=10,
                )
                for response in others:
                    assert response.status_code == 200
                    route = response.json()["modelpilot"]["routing"]
                    excluded = route["excluded_candidates"][0]
                    assert excluded["health"]["reason"] == "half_open_probe_in_flight"
                    assert excluded.get("rank") is None and excluded.get("final_score") is None
                    assert route["served_provider"] == "gemini"
            finally:
                release.set()
                result = await asyncio.wait_for(first, timeout=5)
            assert result.status_code == 200
            assert a.calls == 4  # Three threshold failures, exactly one recovery probe.
            assert maximum == 1
            assert not service.probes.is_in_flight("openai", "openai-model")
            assert store.get_health("openai", "openai-model").state is CircuitState.CLOSED

    asyncio.run(run())


@pytest.mark.parametrize("method", ["upsert_health", "record_attempt"])
def test_probe_persistence_failures_remain_independent(tmp_path, monkeypatch, method):
    clock = Clock()
    store = SQLiteMetricsStore(tmp_path / "fault.db")
    a, b = FakeProvider("openai", clock), FakeProvider("gemini", clock)
    service = wire(store, [a, b], clock)
    with TestClient(create_app(gateway=service)) as client:
        opened = open_through_http(client, a, store)
        clock.now = START + timedelta(seconds=60)
        a.error = None
        previous_b = b.calls

        def fail(*args):
            raise RuntimeError("synthetic persistence failure")

        monkeypatch.setattr(store, method, fail)
        response = client.post(CHAT, json=PAYLOAD)
        assert response.status_code == 200
        assert b.calls == previous_b
        assert not service.probes.is_in_flight("openai", "openai-model")
        health = store.get_health("openai", "openai-model")
        attempts = store.get_recent_attempts("openai", "openai-model", 100, START - timedelta(1))
        if method == "upsert_health":
            assert health == opened
            assert len(attempts) == 4 and attempts[0].success
        else:
            assert health.state is CircuitState.CLOSED
            assert len(attempts) == 3


def test_release_version_metadata_is_consistent():
    root = Path(__file__).resolve().parents[2]
    assert __version__ == "0.4.0"
    package = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    assert package["project"]["version"] == __version__
    for name in ["package.json", "package-lock.json"]:
        metadata = json.loads((root / "frontend" / name).read_text(encoding="utf-8"))
        assert metadata["version"] == __version__
        if "packages" in metadata:
            assert metadata["packages"][""]["version"] == __version__
