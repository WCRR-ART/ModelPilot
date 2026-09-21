"""Explicit fake CLI execution is visible through the same live read API/resolver."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_benchmark_cli import prepare_cli
from test_benchmark_runner import TARGET, outcome
from test_health_aware_routing import opened
from test_routing_explanations import make_decision
from test_sqlite_metrics_store import make_attempt, make_pricing

from modelpilot.benchmarks import cli
from modelpilot.benchmarks.sqlite_store import SQLiteBenchmarkStore
from modelpilot.main import create_app
from modelpilot.metrics import SQLiteMetricsStore


def production_dump(store):
    with store._connect() as connection:
        return tuple(
            line for line in connection.iterdump()
            if not line.startswith('INSERT INTO "benchmark_')
        )


@pytest.mark.parametrize("outcomes,exit_code,score", [
    ([outcome()] * 6, 0, 1),
    ([outcome("wrong")] * 6, 0, 0),
    ([outcome(error="timeout")] + [outcome()] * 5, 0, 1),
    ([outcome(), outcome(error="authentication_error")], 3, 1),
])
def test_cli_store_live_api_quality_and_production_isolation(
    tmp_path, monkeypatch, capsys, outcomes, exit_code, score,
):
    args, provider, settings, constructions = prepare_cli(
        tmp_path, monkeypatch, count=6, outcomes=outcomes,
    )
    settings.quality_suite_path = Path(args[args.index("--suite") + 1])
    settings.metrics_db_path.parent.mkdir(parents=True)
    production = SQLiteMetricsStore(settings.metrics_db_path)
    production.record_attempt(make_attempt())
    production.upsert_pricing(make_pricing())
    production.upsert_health(opened())
    production.record_routing_decision(make_decision())

    def forbidden(*args, **kwargs):
        pytest.fail("no production routing or network calls permitted")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr("modelpilot.router.ModelRouter.rank", forbidden)
    monkeypatch.setattr("modelpilot.router.ModelRouter.rank_with_explanation", forbidden)
    monkeypatch.setattr("modelpilot.service.GatewayService.complete", forbidden)
    # Start before CLI: the resolver must see new evidence without a restart.
    with TestClient(create_app(settings)) as client:
        before = production_dump(production)
        assert client.get("/v1/benchmarks/runs").json() == []
        assert cli.main(args) == exit_code
        assert production_dump(production) == before
        runs = client.get("/v1/benchmarks/runs").json()
        assert len(runs) == 1 and runs[0]["run_id"] in capsys.readouterr().out
        detail = client.get(f"/v1/benchmarks/runs/{runs[0]['run_id']}").json()
        assert len(detail["case_results"]) == len(provider.calls) == len(outcomes)
        assert constructions == [TARGET.provider]
        response = client.get(
            "/v1/benchmarks/quality", params={"provider": TARGET.provider, "model": TARGET.model},
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["quality_score"] == score
        assert snapshot["source_run_ids"] == [runs[0]["run_id"]]
        assert snapshot["evaluated_cases"] == runs[0]["completed_cases"]
        assert snapshot["latest_run_completeness"] == runs[0]["completed_cases"] / 6
        assert production_dump(production) == before
        assert client.get("/health").status_code == 200
        assert client.get("/v1/metrics/providers").status_code == 200
        assert client.get("/v1/health/providers").status_code == 200
    with production._connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert len(SQLiteBenchmarkStore(settings.metrics_db_path).list_runs()) == 1
