import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import create_app
from modelpilot.metrics import (
    AttemptRecord,
    MetricsStore,
    RoutingDecision,
    RoutingExplanation,
    RoutingSignal,
    SQLiteMetricsStore,
)
from modelpilot.providers.base import Provider
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import GatewayService


class MetricsApiProvider(Provider):
    name = "test"

    def __init__(self) -> None:
        super().__init__("test-key")

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        return {"model": model}


def make_client(store: object) -> TestClient:
    provider = MetricsApiProvider()
    gateway = GatewayService(
        ModelRouter(
            {"test": provider},
            [ModelCandidate("test", "test-model", 1, 1, 1, 1)],
        ),
        RequestLogStore(),
        cast(MetricsStore, store),
    )
    return TestClient(create_app(Settings(), gateway))


def make_attempt(
    request_id: str,
    *,
    attempt_index: int = 0,
    provider: str = "openai",
    model: str = "model-a",
    success: bool = True,
    latency_ms: float = 100,
    estimated_cost: Decimal | None = Decimal("0.001"),
    finished_at: datetime | None = None,
    error_type: str | None = None,
) -> AttemptRecord:
    finished = finished_at or datetime.now(UTC) - timedelta(minutes=1)
    return AttemptRecord(
        request_id=request_id,
        attempt_index=attempt_index,
        provider=provider,
        model=model,
        started_at=finished - timedelta(milliseconds=latency_ms),
        finished_at=finished,
        latency_ms=latency_ms,
        success=success,
        error_type=error_type,
        estimated_cost=estimated_cost,
        created_at=finished,
    )


def make_decision(
    request_id: str,
    *,
    created_at: datetime | None = None,
    served_provider: str | None = "gemini",
) -> RoutingDecision:
    created = created_at or datetime.now(UTC) - timedelta(minutes=1)
    signal = RoutingSignal(
        provider="openai",
        model="openai-model",
        quality_score=0.9,
        latency_score=0.8,
        reliability_score=0.7,
        cost_score=0.6,
        measured_sample_count=10,
        measured_confidence=5 / 45,
        final_score=0.75,
        rank=1,
        selected=True,
        sources={
            "quality": "configured",
            "latency": "blended",
            "reliability": "blended",
            "cost": "static_unavailable",
        },
    )
    explanation = RoutingExplanation(
        request_id=request_id,
        routing_version="v0.2",
        strategy="weighted_measured_v1",
        selected_provider="openai",
        selected_model="openai-model",
        served_provider=served_provider,
        served_model=f"{served_provider}-model" if served_provider else None,
        selected=signal,
        candidates=(signal,),
        created_at=created,
    )
    return RoutingDecision.from_explanation(explanation)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMetricsStore:
    return SQLiteMetricsStore(tmp_path / "metrics.sqlite3")


def test_empty_summary_has_zero_counts_and_nullable_measurements(
    store: SQLiteMetricsStore,
) -> None:
    with make_client(store) as client:
        response = client.get("/v1/metrics/summary")

    assert response.status_code == 200
    body = response.json()
    for field in [
        "request_count",
        "attempt_count",
        "success_count",
        "failure_count",
        "providers_count",
        "models_count",
        "routing_decision_count",
    ]:
        assert body[field] == 0
    assert body["success_rate"] is None
    assert body["average_latency_ms"] is None
    assert body["estimated_total_cost"] is None


def test_summary_aggregates_fallback_requests_counts_latency_and_decimal_cost(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(make_attempt("req_one", latency_ms=100))
    store.record_attempt(
        make_attempt(
            "req_two",
            provider="gemini",
            model="model-b",
            success=False,
            latency_ms=300,
            estimated_cost=None,
            error_type="timeout",
        )
    )
    store.record_attempt(
        make_attempt(
            "req_two",
            attempt_index=1,
            latency_ms=500,
            estimated_cost=Decimal("0.002000000000000003"),
        )
    )
    store.record_routing_decision(make_decision("req_two"))

    with make_client(store) as client:
        body = client.get("/v1/metrics/summary").json()

    assert body["request_count"] == 2
    assert body["attempt_count"] == 3
    assert body["success_count"] == 2
    assert body["failure_count"] == 1
    assert body["success_rate"] == pytest.approx(2 / 3)
    assert body["average_latency_ms"] == 300
    assert body["estimated_total_cost"] == "0.003000000000000003"
    assert body["providers_count"] == 2
    assert body["models_count"] == 2
    assert body["routing_decision_count"] == 1


def test_summary_excludes_nullable_cost_without_inventing_zero(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(make_attempt("req_none", estimated_cost=None))

    with make_client(store) as client:
        body = client.get("/v1/metrics/summary").json()

    assert body["estimated_total_cost"] is None


def test_summary_hours_default_minimum_and_maximum_boundaries(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(
        make_attempt(
            "old",
            finished_at=datetime.now(UTC) - timedelta(hours=25),
        )
    )

    with make_client(store) as client:
        default = client.get("/v1/metrics/summary")
        minimum = client.get("/v1/metrics/summary?hours=1")
        maximum = client.get("/v1/metrics/summary?hours=168")

    assert default.status_code == minimum.status_code == maximum.status_code == 200
    assert default.json()["request_count"] == 0
    assert maximum.json()["request_count"] == 1
    since = datetime.fromisoformat(default.json()["window"]["since"])
    until = datetime.fromisoformat(default.json()["window"]["until"])
    assert until - since == timedelta(hours=24)
    assert since.utcoffset() == until.utcoffset() == timedelta(0)


@pytest.mark.parametrize("hours", [0, 169, "invalid"])
def test_summary_rejects_invalid_hours(store: SQLiteMetricsStore, hours: object) -> None:
    with make_client(store) as client:
        response = client.get("/v1/metrics/summary", params={"hours": hours})

    assert response.status_code == 422


def test_provider_metrics_are_sorted_filterable_decimal_safe_and_utc(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(
        make_attempt(
            "req_z",
            provider="zeta",
            model="model-b",
            estimated_cost=Decimal("0.000000000000000003"),
        )
    )
    store.record_attempt(
        make_attempt(
            "req_a",
            provider="alpha",
            model="model-a",
            estimated_cost=Decimal("0.000000000000000001"),
        )
    )

    with make_client(store) as client:
        all_metrics = client.get("/v1/metrics/providers")
        by_provider = client.get("/v1/metrics/providers?provider=alpha")
        by_model = client.get("/v1/metrics/providers?model=model-b")

    assert all_metrics.status_code == 200
    assert [(item["provider"], item["model"]) for item in all_metrics.json()] == [
        ("alpha", "model-a"),
        ("zeta", "model-b"),
    ]
    alpha = by_provider.json()[0]
    assert alpha["estimated_average_cost"] == "0.000000000000000001"
    assert datetime.fromisoformat(alpha["window_start"]).utcoffset() == timedelta(0)
    assert [item["provider"] for item in by_model.json()] == ["zeta"]


def test_provider_filters_can_return_empty(store: SQLiteMetricsStore) -> None:
    with make_client(store) as client:
        provider = client.get("/v1/metrics/providers?provider=missing")
        model = client.get("/v1/metrics/providers?model=missing")

    assert provider.json() == []
    assert model.json() == []


def test_routing_decisions_return_saved_explanation_in_newest_order(
    store: SQLiteMetricsStore,
) -> None:
    now = datetime.now(UTC)
    older = make_decision("req_old", created_at=now - timedelta(minutes=2))
    newer = make_decision("req_new", created_at=now - timedelta(minutes=1))
    store.record_routing_decision(older)
    store.record_routing_decision(newer)

    with make_client(store) as client:
        response = client.get("/v1/metrics/routing-decisions")

    assert response.status_code == 200
    body = response.json()
    assert [item["request_id"] for item in body] == ["req_new", "req_old"]
    assert body[0]["selected_provider"] == "openai"
    assert body[0]["served_provider"] == "gemini"
    assert body[0]["explanation"] == newer.explanation.model_dump(mode="json")


def test_routing_decision_default_and_maximum_limits(store: SQLiteMetricsStore) -> None:
    now = datetime.now(UTC)
    for index in range(101):
        store.record_routing_decision(
            make_decision(f"req_{index:03}", created_at=now - timedelta(seconds=index))
        )

    with make_client(store) as client:
        default = client.get("/v1/metrics/routing-decisions")
        maximum = client.get("/v1/metrics/routing-decisions?limit=100")

    assert len(default.json()) == 20
    assert len(maximum.json()) == 100


@pytest.mark.parametrize("limit", [0, 101, "invalid"])
def test_routing_decisions_reject_invalid_limit(
    store: SQLiteMetricsStore, limit: object
) -> None:
    with make_client(store) as client:
        response = client.get(
            "/v1/metrics/routing-decisions", params={"limit": limit}
        )

    assert response.status_code == 422


def test_failures_return_only_safe_failed_attempts_newest_first_and_limited(
    store: SQLiteMetricsStore,
) -> None:
    now = datetime.now(UTC)
    store.record_attempt(make_attempt("req_success", finished_at=now, success=True))
    store.record_attempt(
        make_attempt(
            "req_old",
            success=False,
            finished_at=now - timedelta(minutes=2),
            estimated_cost=None,
            error_type="timeout",
        )
    )
    store.record_attempt(
        make_attempt(
            "req_new",
            success=False,
            finished_at=now - timedelta(minutes=1),
            estimated_cost=None,
            error_type="rate_limit",
        )
    )

    with make_client(store) as client:
        body = client.get("/v1/metrics/failures?limit=1").json()

    assert len(body) == 1
    assert body[0]["request_id"] == "req_new"
    assert body[0]["error_type"] == "rate_limit"
    assert set(body[0]) == {
        "request_id",
        "provider",
        "model",
        "finished_at",
        "created_at",
        "latency_ms",
        "error_type",
    }
    assert datetime.fromisoformat(body[0]["finished_at"]).utcoffset() == timedelta(0)


def test_failure_default_and_maximum_limits(store: SQLiteMetricsStore) -> None:
    now = datetime.now(UTC)
    for index in range(101):
        store.record_attempt(
            make_attempt(
                f"req_{index:03}",
                success=False,
                finished_at=now - timedelta(seconds=index),
                estimated_cost=None,
                error_type="provider_error",
            )
        )

    with make_client(store) as client:
        default = client.get("/v1/metrics/failures")
        maximum = client.get("/v1/metrics/failures?limit=100")

    assert len(default.json()) == 20
    assert len(maximum.json()) == 100


def test_failure_api_replaces_unrecognized_error_category(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(
        make_attempt(
            "req_unsafe",
            success=False,
            estimated_cost=None,
            error_type="Authorization: Bearer stored-secret",
        )
    )

    with make_client(store) as client:
        response = client.get("/v1/metrics/failures")

    assert response.json()[0]["error_type"] == "unknown_error"
    assert "stored-secret" not in response.text


@pytest.mark.parametrize("limit", [0, 101, "invalid"])
def test_failures_reject_invalid_limit(store: SQLiteMetricsStore, limit: object) -> None:
    with make_client(store) as client:
        response = client.get("/v1/metrics/failures", params={"limit": limit})

    assert response.status_code == 422


def test_metrics_responses_do_not_expose_sensitive_content(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(
        make_attempt(
            "req_private",
            success=False,
            estimated_cost=None,
            error_type="provider_error",
        )
    )
    store.record_routing_decision(make_decision("req_private"))

    with make_client(store) as client:
        payload = " ".join(
            client.get(path).text
            for path in [
                "/v1/metrics/summary",
                "/v1/metrics/providers",
                "/v1/metrics/routing-decisions",
                "/v1/metrics/failures",
            ]
        ).lower()

    for forbidden in [
        "prompt",
        "completion",
        "api_key",
        "authorization",
        "bearer",
        "secret",
        ".env",
    ]:
        assert forbidden not in payload


class FailingReadStore:
    def get_metrics_summary(self, since: datetime, until: datetime) -> None:
        raise sqlite3.OperationalError("Authorization: Bearer database-secret")

    def list_provider_metrics(self) -> list[object]:
        raise sqlite3.OperationalError("database unavailable")

    def list_recent_routing_decisions(self, limit: int) -> list[object]:
        raise sqlite3.OperationalError("database unavailable")

    def list_recent_failures(self, limit: int) -> list[object]:
        raise sqlite3.OperationalError("database unavailable")


@pytest.mark.parametrize(
    "path",
    [
        "/v1/metrics/summary",
        "/v1/metrics/providers",
        "/v1/metrics/routing-decisions",
        "/v1/metrics/failures",
    ],
)
def test_query_failure_returns_503_without_fake_empty_payload(
    path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level("WARNING", logger="modelpilot.metrics.api"),
        make_client(FailingReadStore()) as client,
    ):
        response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": "metrics data unavailable"}
    assert "metrics query failed" in caplog.text
    assert "database-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text


def test_metrics_routes_expose_no_write_methods(store: SQLiteMetricsStore) -> None:
    with make_client(store) as client:
        paths = client.get("/openapi.json").json()["paths"]

    for path, operations in paths.items():
        if path.startswith("/v1/metrics"):
            assert set(operations) == {"get"}


def test_metrics_routes_preserve_configured_cors(store: SQLiteMetricsStore) -> None:
    with make_client(store) as client:
        response = client.options(
            "/v1/metrics/summary",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
