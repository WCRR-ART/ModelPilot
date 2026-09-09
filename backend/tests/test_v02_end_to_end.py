import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import create_app
from modelpilot.metrics import AttemptRecord, ModelPricing, RoutingDecision, SQLiteMetricsStore
from modelpilot.providers import Provider, ProviderErrorType, ProviderOutcome
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest
from modelpilot.service import GatewayService


class ScriptedProvider(Provider):
    def __init__(
        self,
        name: str,
        outcomes: list[bool],
        call_order: list[str],
        *,
        latency_ms: float = 125.5,
    ) -> None:
        super().__init__("fake-provider-key")
        self.name = name
        self.outcomes = outcomes
        self.call_order = call_order
        self.latency_ms = latency_ms
        self.calls = 0

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderOutcome:
        success = self.outcomes[self.calls]
        self.calls += 1
        self.call_order.append(self.name)
        finished_at = datetime.now(UTC)
        response: ChatCompletionPayload | None = None
        if success:
            response = {
                "id": f"chatcmpl-{self.name}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "fake response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 500,
                    "total_tokens": 1_500,
                },
            }
        return ProviderOutcome(
            provider=self.name,
            model=model,
            success=success,
            response=response,
            input_tokens=1_000 if success else None,
            output_tokens=500 if success else None,
            total_tokens=1_500 if success else None,
            error_type=None if success else ProviderErrorType.PROVIDER_ERROR,
            error_message=None if success else "normalized provider failure",
            status_code=200 if success else 503,
            started_at=finished_at - timedelta(milliseconds=self.latency_ms),
            finished_at=finished_at,
            latency_ms=self.latency_ms,
        )


def make_client(
    database: Path,
    provider_outcomes: list[tuple[str, bool]],
    *,
    candidates: list[ModelCandidate] | None = None,
) -> tuple[TestClient, SQLiteMetricsStore, dict[str, ScriptedProvider], list[str]]:
    call_order: list[str] = []
    providers = {
        name: ScriptedProvider(name, [success], call_order)
        for name, success in provider_outcomes
    }
    configured_candidates = candidates or [
        ModelCandidate(
            name,
            f"{name}-model",
            1 - index * 0.1,
            1 - index * 0.1,
            1 - index * 0.1,
            1 - index * 0.1,
        )
        for index, (name, _) in enumerate(provider_outcomes)
    ]
    store = SQLiteMetricsStore(database)
    gateway = GatewayService(
        ModelRouter(providers, configured_candidates, store),
        RequestLogStore(),
        store,
    )
    return TestClient(create_app(Settings(), gateway)), store, providers, call_order


def chat_payload(preference: str | None = None, *, model: str = "auto") -> dict:
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "not persisted"}],
    }
    if preference is not None:
        payload["modelpilot"] = {
            "preferences": {
                "quality": int(preference == "quality"),
                "cost": int(preference == "cost"),
                "latency": int(preference == "latency"),
                "reliability": int(preference == "reliability"),
            }
        }
    return payload


def recent_attempts(
    store: SQLiteMetricsStore, providers: list[str]
) -> list[AttemptRecord]:
    since = datetime.now(UTC) - timedelta(days=1)
    attempts = [
        attempt
        for provider in providers
        for attempt in store.get_recent_attempts(
            provider, f"{provider}-model", 100, since
        )
    ]
    return sorted(attempts, key=lambda attempt: attempt.attempt_index)


def record_history(
    store: SQLiteMetricsStore,
    provider: str,
    *,
    count: int,
    success_count: int,
    latency_ms: float,
    estimated_cost: Decimal | None,
) -> None:
    finished_at = datetime.now(UTC) - timedelta(minutes=1)
    for index in range(count):
        success = index < success_count
        store.record_attempt(
            AttemptRecord(
                request_id=f"history_{provider}_{index}",
                provider=provider,
                model=f"{provider}-model",
                started_at=finished_at - timedelta(milliseconds=latency_ms),
                finished_at=finished_at,
                latency_ms=latency_ms,
                success=success,
                error_type=None if success else "provider_error",
                estimated_cost=estimated_cost if success else None,
                created_at=finished_at,
            )
        )


def test_single_success_flows_from_http_through_sqlite_and_metrics_api(
    tmp_path: Path,
) -> None:
    database = tmp_path / "single.sqlite3"
    client, store, _, call_order = make_client(
        database, [("alpha", True)]
    )
    store.upsert_pricing(
        ModelPricing(
            provider="alpha",
            model="alpha-model",
            input_cost_per_million_tokens=Decimal("2"),
            output_cost_per_million_tokens=Decimal("4"),
            source="fake test pricing",
            updated_at=datetime.now(UTC),
        )
    )

    with client:
        response = client.post("/v1/chat/completions", json=chat_payload())
        summary = client.get("/v1/metrics/summary").json()
        provider_metrics = client.get("/v1/metrics/providers").json()
        decisions = client.get("/v1/metrics/routing-decisions").json()
        failures = client.get("/v1/metrics/failures").json()
        logs = client.get("/v1/logs").json()

    assert response.status_code == 200
    body = response.json()
    assert {"id", "object", "model", "choices", "usage", "modelpilot"} <= body.keys()
    assert body["usage"] == {
        "prompt_tokens": 1_000,
        "completion_tokens": 500,
        "total_tokens": 1_500,
    }
    request_id = body["modelpilot"]["request_id"]
    attempt = recent_attempts(store, ["alpha"])[0]
    assert call_order == ["alpha"]
    assert attempt.request_id == request_id
    assert attempt.latency_ms == 125.5
    assert attempt.estimated_cost == Decimal("0.004")
    assert attempt.input_tokens == 1_000
    assert body["modelpilot"]["routing"]["selected_provider"] == "alpha"
    assert body["modelpilot"]["routing"]["served_provider"] == "alpha"
    assert summary["request_count"] == summary["attempt_count"] == 1
    assert summary["estimated_total_cost"] == "0.004"
    assert provider_metrics[0]["sample_count"] == 1
    assert decisions[0]["request_id"] == request_id
    assert failures == []
    assert logs[0]["success"] is True
    persisted = database.read_bytes()
    assert b"not persisted" not in persisted
    assert b"fake response" not in persisted


@pytest.mark.parametrize(
    ("provider_outcomes", "expected_order"),
    [
        ([('alpha', False), ('beta', True)], ["alpha", "beta"]),
        (
            [('alpha', False), ('beta', False), ('gamma', True)],
            ["alpha", "beta", "gamma"],
        ),
    ],
    ids=["one-failure-then-success", "two-failures-then-success"],
)
def test_fallback_attempts_share_request_and_are_readable_from_metrics_api(
    tmp_path: Path,
    provider_outcomes: list[tuple[str, bool]],
    expected_order: list[str],
) -> None:
    client, store, _, call_order = make_client(
        tmp_path / f"fallback-{len(provider_outcomes)}.sqlite3", provider_outcomes
    )

    with client:
        response = client.post("/v1/chat/completions", json=chat_payload())
        summary = client.get("/v1/metrics/summary").json()
        failures = client.get("/v1/metrics/failures").json()
        decision = client.get("/v1/metrics/routing-decisions").json()[0]

    assert response.status_code == 200
    body = response.json()
    request_id = body["modelpilot"]["request_id"]
    attempts = recent_attempts(store, expected_order)
    assert call_order == expected_order
    assert len(attempts) == len(expected_order)
    assert {attempt.request_id for attempt in attempts} == {request_id}
    assert [attempt.success for attempt in attempts] == [False] * (
        len(expected_order) - 1
    ) + [True]
    assert [attempt.attempt_index for attempt in attempts] == list(
        range(len(expected_order))
    )
    assert decision["selected_provider"] == "alpha"
    assert decision["served_provider"] == expected_order[-1]
    assert body["modelpilot"]["served_by"]["provider"] == expected_order[-1]
    assert summary["request_count"] == 1
    assert summary["attempt_count"] == len(expected_order)
    assert summary["failure_count"] == len(expected_order) - 1
    assert {failure["provider"] for failure in failures} == set(expected_order[:-1])


def test_all_provider_failures_persist_before_existing_api_error(tmp_path: Path) -> None:
    names = ["alpha", "beta", "gamma"]
    client, store, _, call_order = make_client(
        tmp_path / "all-fail.sqlite3", [(name, False) for name in names]
    )

    with client:
        response = client.post("/v1/chat/completions", json=chat_payload())
        summary = client.get("/v1/metrics/summary").json()
        failures = client.get("/v1/metrics/failures").json()
        decision = client.get("/v1/metrics/routing-decisions").json()[0]

    assert response.status_code == 502
    request_id = response.json()["detail"]["request_id"]
    attempts = recent_attempts(store, names)
    assert call_order == names
    assert len(attempts) == 3
    assert {attempt.request_id for attempt in attempts} == {request_id}
    assert all(not attempt.success for attempt in attempts)
    assert summary["failure_count"] == summary["attempt_count"] == 3
    assert {failure["provider"] for failure in failures} == set(names)
    assert decision["selected_provider"] == "alpha"
    assert decision["served_provider"] is None


class FailingAttemptStore:
    def __init__(self) -> None:
        self.decisions: list[RoutingDecision] = []

    def list_provider_metrics(self) -> list:
        return []

    def get_pricing(self, provider: str, model: str) -> None:
        return None

    def record_attempt(self, attempt: AttemptRecord) -> None:
        raise RuntimeError("Authorization: Bearer fake-metrics-secret")

    def record_routing_decision(self, decision: RoutingDecision) -> None:
        self.decisions.append(decision)


def test_metrics_write_failure_is_isolated_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call_order: list[str] = []
    first = ScriptedProvider("alpha", [True], call_order)
    second = ScriptedProvider("beta", [True], call_order)
    store = FailingAttemptStore()
    gateway = GatewayService(
        ModelRouter(
            {"alpha": first, "beta": second},
            [
                ModelCandidate("alpha", "alpha-model", 1, 1, 1, 1),
                ModelCandidate("beta", "beta-model", 0.5, 0.5, 0.5, 0.5),
            ],
            store,
        ),
        RequestLogStore(),
        store,
    )

    with (
        caplog.at_level(logging.WARNING, logger="modelpilot.service"),
        TestClient(create_app(Settings(), gateway)) as client,
    ):
        response = client.post("/v1/chat/completions", json=chat_payload())

    assert response.status_code == 200
    assert response.json()["model"] == "alpha-model"
    assert call_order == ["alpha"]
    assert len(store.decisions) == 1
    assert "failed to record provider attempt" in caplog.text
    assert "fake-metrics-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text


def test_cold_start_history_keeps_static_order(tmp_path: Path) -> None:
    candidates = [
        ModelCandidate("alpha", "alpha-model", 0.9, 0.9, 0.9, 0.9),
        ModelCandidate("beta", "beta-model", 0.8, 0.8, 0.8, 0.8),
    ]
    client, store, _, call_order = make_client(
        tmp_path / "cold-start.sqlite3",
        [("alpha", True), ("beta", True)],
        candidates=candidates,
    )
    record_history(
        store,
        "alpha",
        count=4,
        success_count=0,
        latency_ms=10_000,
        estimated_cost=None,
    )
    record_history(
        store,
        "beta",
        count=4,
        success_count=4,
        latency_ms=100,
        estimated_cost=Decimal("0.0001"),
    )

    with client:
        response = client.post("/v1/chat/completions", json=chat_payload("latency"))

    candidate = response.json()["modelpilot"]["routing"]["selected"]
    assert response.status_code == 200
    assert call_order == ["alpha"]
    assert candidate["provider"] == "alpha"
    assert candidate["sources"]["latency"] == "static_cold_start"
    assert candidate["measured_confidence"] == 0


@pytest.mark.parametrize(
    ("preference", "expected_provider"),
    [("cost", "cheap"), ("latency", "fast"), ("reliability", "reliable")],
)
def test_dynamic_routing_uses_persisted_metrics_for_preferences(
    tmp_path: Path, preference: str, expected_provider: str
) -> None:
    names = ["cheap", "fast", "reliable"]
    candidates = [
        ModelCandidate(name, f"{name}-model", 0.5, 0.5, 0.5, 0.5)
        for name in names
    ]
    client, store, _, call_order = make_client(
        tmp_path / f"dynamic-{preference}.sqlite3",
        [(name, True) for name in names],
        candidates=candidates,
    )
    record_history(
        store,
        "cheap",
        count=50,
        success_count=45,
        latency_ms=2_000,
        estimated_cost=Decimal("0.0001"),
    )
    record_history(
        store,
        "fast",
        count=50,
        success_count=40,
        latency_ms=100,
        estimated_cost=Decimal("0.01"),
    )
    record_history(
        store,
        "reliable",
        count=50,
        success_count=50,
        latency_ms=3_000,
        estimated_cost=Decimal("0.005"),
    )

    with client:
        response = client.post(
            "/v1/chat/completions", json=chat_payload(preference)
        )

    assert response.status_code == 200
    assert call_order == [expected_provider]
    assert response.json()["modelpilot"]["routing"]["selected_provider"] == expected_provider


def test_missing_cost_metrics_use_static_cost_while_other_signals_are_measured(
    tmp_path: Path,
) -> None:
    candidate = ModelCandidate("alpha", "alpha-model", 0.8, 0.3, 0.2, 0.2)
    client, store, _, _ = make_client(
        tmp_path / "missing-cost.sqlite3",
        [("alpha", True)],
        candidates=[candidate],
    )
    record_history(
        store,
        "alpha",
        count=50,
        success_count=50,
        latency_ms=100,
        estimated_cost=None,
    )

    with client:
        response = client.post("/v1/chat/completions", json=chat_payload())

    signal = response.json()["modelpilot"]["routing"]["selected"]
    assert response.status_code == 200
    assert signal["sources"]["latency"] == "measured"
    assert signal["sources"]["reliability"] == "measured"
    assert signal["sources"]["cost"] == "static_unavailable"
    assert signal["cost_score"] == candidate.cost


def test_explicit_model_remains_compatible_without_auto_decision(tmp_path: Path) -> None:
    client, _, _, call_order = make_client(
        tmp_path / "explicit.sqlite3", [("alpha", True), ("beta", True)]
    )

    with client:
        response = client.post(
            "/v1/chat/completions", json=chat_payload(model="beta-model")
        )
        decisions = client.get("/v1/metrics/routing-decisions").json()

    assert response.status_code == 200
    assert call_order == ["beta"]
    assert response.json()["modelpilot"]["served_by"]["provider"] == "beta"
    assert "routing" not in response.json()["modelpilot"]
    assert decisions == []
