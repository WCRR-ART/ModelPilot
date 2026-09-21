"""Real SQLite read-path tests; no provider network access."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_benchmark_quality import NOW, definition, make_run
from test_health_aware_routing import opened
from test_routing_explanations import make_decision
from test_sqlite_metrics_store import make_attempt, make_pricing

from modelpilot.benchmarks import SQLiteBenchmarkStore, aggregate_quality
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.benchmarks.results import BenchmarkTarget
from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import create_app
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.router import ModelRouter
from modelpilot.service import GatewayService

ROOT = "/v1/benchmarks"


def client_for(store, suite=None, production=None):
    source = BenchmarkQualityResolver(suite, store, clock=lambda: NOW) if suite else None
    gateway = GatewayService(
        ModelRouter({}, [], quality_source=source), RequestLogStore(), production,
    )
    return TestClient(create_app(Settings(), gateway, benchmark_store=store))


def changed_run(run, **changes):
    target = {key: value for key, value in changes.items() if key in ("provider", "model")}
    if target:
        changes["case_results"] = tuple(c.model_copy(update=target) for c in run.case_results)
    return run.model_copy(update=changes)


@pytest.fixture
def store(tmp_path):
    return SQLiteBenchmarkStore(tmp_path / "bench.sqlite")


def test_empty_and_one_run_summary(store):
    with client_for(store) as client:
        assert client.get(f"{ROOT}/runs").json() == []
        run = make_run(definition())
        store.save_run(run)
        response = client.get(f"{ROOT}/runs")
        assert response.status_code == 200
        item, = response.json()
        expected = run.model_dump(mode="json", exclude={"case_results", "config"})
        assert item == expected
        assert "case_results" not in item and "config" not in item
        assert item["started_at"].endswith("Z") and item["finished_at"].endswith("Z")


def test_order_default_maximum_limit(store):
    suite = definition()
    for i in range(101):
        store.save_run(make_run(suite, run_id=f"run-{i:03}", finished=NOW + timedelta(seconds=i)))
    # Tie break matches the existing Store ordering, not creation order.
    store.save_run(make_run(suite, run_id="run-z", finished=NOW + timedelta(seconds=100)))
    with client_for(store) as client:
        default = client.get(f"{ROOT}/runs").json()
        assert len(default) == 20
        assert [r["run_id"] for r in default[:3]] == ["run-z", "run-100", "run-099"]
        assert len(client.get(f"{ROOT}/runs", params={"limit": 100}).json()) == 100


@pytest.mark.parametrize("limit", [0, -1, 101, "bad", "1.5"])
def test_invalid_limits(store, limit):
    with client_for(store) as client:
        assert client.get(f"{ROOT}/runs", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("filters", [
    {"provider": "selected"}, {"model": "selected"}, {"suite_id": "selected"},
    {"suite_version": "selected"},
    {"provider": "selected", "model": "selected", "suite_id": "selected", "suite_version": "2"},
])
def test_filters_apply_before_limit(store, filters):
    suite = definition()
    wanted = changed_run(make_run(suite, run_id="wanted"), **filters)
    store.save_run(wanted)
    for i in range(21):
        store.save_run(make_run(suite, run_id=f"new-{i}", finished=NOW + timedelta(seconds=i + 1)))
    with client_for(store) as client:
        response = client.get(f"{ROOT}/runs", params={**filters, "limit": 1})
        assert response.status_code == 200
        assert [r["run_id"] for r in response.json()] == ["wanted"]
        assert client.get(f"{ROOT}/runs", params={"provider": "' OR 1=1 --"}).json() == []


def test_detail_retains_case_order_zero_failure_usage_and_no_raw_data(store):
    suite = definition(3)
    run = make_run(suite, [1, 0, "timeout"])
    store.save_run(run)
    with client_for(store) as client:
        response = client.get(f"{ROOT}/runs/{run.run_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["config"] == run.config.model_dump()
        cases = data.pop("case_results")
        for index, (case, stored) in enumerate(zip(cases, run.case_results, strict=True)):
            assert case.pop("case_index") == index
            assert case == stored.model_dump(mode="json")
        assert cases[1]["evaluation"]["score"] == 0
        assert cases[1]["evaluation"]["passed"] is False
        assert cases[2]["evaluation"] is None and cases[2]["error_type"] == "timeout"
        assert all(
            c["input_tokens"] is c["output_tokens"] is c["total_tokens"] is None for c in cases
        )
        for forbidden in (
            "prompt", "messages", "raw_output", "completion", "Authorization", "Bearer",
        ):
            assert forbidden not in response.text
        assert "fixture prompt" not in response.text
        assert client.get(f"{ROOT}/runs/missing").status_code == 404


def test_quality_matches_existing_aggregation(store):
    suite = definition(60)
    run = make_run(suite, [1] * 45 + [0] * 5 + ["timeout"] * 10)
    store.save_run(run)
    with client_for(store, suite) as client:
        response = client.get(f"{ROOT}/quality", params={"provider": "fake", "model": "model-a"})
        assert response.status_code == 200
        expected = aggregate_quality(
            suite, [run], target=BenchmarkTarget(provider="fake", model="model-a"),
            generated_at=NOW,
        )
        assert response.json() == expected.model_dump(mode="json")
        assert response.json()["quality_score"] == 0.9
        assert response.json()["coverage"] == 1
        assert response.json()["execution_completeness"] == 50 / 60
        assert response.json()["source_run_ids"] == [run.run_id]
        assert response.json()["categories"]


@pytest.mark.parametrize("change", [
    {"provider": "another"}, {"model": "another"}, {"suite_id": "another"},
    {"suite_version": "another"}, {"suite_fingerprint": "f" * 64},
])
def test_quality_scope_isolation(store, change):
    suite = definition(50)
    store.save_run(changed_run(make_run(suite), **change))
    with client_for(store, suite) as client:
        response = client.get(f"{ROOT}/quality", params={"provider": "fake", "model": "model-a"})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "no_quality_evidence"


def test_missing_evidence_and_unconfigured_suite(store):
    with client_for(store, definition()) as client:
        response = client.get(f"{ROOT}/quality?provider=fake&model=model-a")
        assert response.status_code == 404
    with client_for(store) as client:
        response = client.get(f"{ROOT}/quality?provider=fake&model=model-a")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "quality_suite_not_configured"


@pytest.mark.parametrize("params", [
    {}, {"provider": "fake"}, {"model": "model-a"},
    {"provider": "", "model": "model-a"}, {"provider": "auto", "model": "model-a"},
    {"provider": "fake", "model": "auto"}, {"provider": "fake", "model": " model-a "},
])
def test_quality_requires_explicit_target(store, params):
    with client_for(store, definition()) as client:
        assert client.get(f"{ROOT}/quality", params=params).status_code == 422


@pytest.mark.parametrize("method,path", [
    ("list_runs", "/runs"), ("get_run", "/runs/run-a"),
    ("list_matching_runs", "/quality?provider=fake&model=model-a"),
    ("aggregate", "/quality?provider=fake&model=model-a"),
])
def test_read_failures_explicit_and_sanitized(store, monkeypatch, caplog, method, path):
    suite = definition()
    store.save_run(make_run(suite))

    def fail(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer SECRET env DB=C:/private/file prompt=PRIVATE")

    if method == "aggregate":
        monkeypatch.setattr("modelpilot.benchmarks.resolver.aggregate_quality", fail)
    else:
        monkeypatch.setattr(store, method, fail)
    with client_for(store, suite) as client:
        response = client.get(ROOT + path)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "benchmark_data_unavailable"
        assert "benchmark read failed" in caplog.text
        for forbidden in ("SECRET", "PRIVATE", "Authorization", "C:/private", "Bearer"):
            assert forbidden not in response.text + caplog.text


def test_gets_are_read_only_and_no_execution_endpoint(store, tmp_path, monkeypatch):
    suite = definition(6)
    store.save_run(make_run(suite))
    # Both store implementations use the same configured path.
    production = SQLiteMetricsStore(tmp_path / "bench.sqlite")
    production.record_attempt(make_attempt())
    production.upsert_pricing(make_pricing())
    production.upsert_health(opened())
    production.record_routing_decision(make_decision())

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP reads must never execute or write")

    monkeypatch.setattr(store, "save_run", forbidden)
    monkeypatch.setattr("modelpilot.benchmarks.runner.BenchmarkRunner.run", forbidden)
    monkeypatch.setattr("modelpilot.providers.openai.OpenAIProvider.complete", forbidden)
    monkeypatch.setattr("modelpilot.providers.gemini.GeminiProvider.complete", forbidden)
    monkeypatch.setattr("modelpilot.providers.deepseek.DeepSeekProvider.complete", forbidden)
    with client_for(store, suite, production) as client:
        with store._connect() as connection:
            before = tuple(connection.iterdump())
        for path in ("/runs", "/runs/run-a", "/quality?provider=fake&model=model-a"):
            assert client.get(ROOT + path).status_code == 200
        for path in ("/run", "/runs", "/quality", "/runs/run-a"):
            assert client.post(ROOT + path).status_code in (404, 405)
        with store._connect() as connection:
            assert tuple(connection.iterdump()) == before
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4


def test_normal_app_wires_read_store_without_quality_suite(tmp_path):
    settings = Settings(metrics_db_path=tmp_path / "data" / "db.sqlite")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get(f"{ROOT}/runs").json() == []
        assert client.get(f"{ROOT}/quality?provider=openai&model=model-a").status_code == 503


def test_explicit_api_store_also_supplies_quality(store, tmp_path):
    suite = definition(6)
    other = SQLiteBenchmarkStore(tmp_path / "other.sqlite")
    store.save_run(make_run(suite))
    gateway = GatewayService(
        ModelRouter({}, [], quality_source=BenchmarkQualityResolver(suite, other)),
        RequestLogStore(),
    )
    with TestClient(create_app(Settings(), gateway, benchmark_store=store)) as client:
        assert len(client.get(f"{ROOT}/runs").json()) == 1
        assert client.get(f"{ROOT}/quality?provider=fake&model=model-a").status_code == 200
    assert gateway.router.quality_source.store is other
