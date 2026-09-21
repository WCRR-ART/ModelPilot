"""Release evidence is synthetic: real components, temporary SQLite, fake providers only."""

import json
import platform
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import NOW, OutcomeProvider, make_outcome
from test_benchmark_cli import prepare_cli
from test_benchmark_quality import definition, make_run
from test_benchmark_runner import TARGET, FakeProvider, outcome
from test_health_aware_routing import opened
from test_sqlite_benchmark_store import legacy_database

from modelpilot.benchmarks import BenchmarkRunner, SQLiteBenchmarkStore, cli, load_benchmark_suite
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.logging import RequestLogStore
from modelpilot.main import create_app
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import RoutingPreferences
from modelpilot.service import GatewayService

BACKUP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup-sqlite.py"
QUALITY = RoutingPreferences(quality=1, cost=0, latency=0, reliability=0)
CHAT = {
    "model": "auto",
    "messages": [{"role": "user", "content": "synthetic release acceptance"}],
    "modelpilot": {"preferences": QUALITY.model_dump()},
}


@pytest.fixture(autouse=True)
def forbid_cloud(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("release hardening must never call external providers")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def rows(path, tables=("attempts", "pricing", "provider_health", "routing_decisions")):
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}


def wire(settings, suite):
    store = SQLiteMetricsStore(settings.metrics_db_path)
    benchmarks = SQLiteBenchmarkStore(settings.metrics_db_path)
    providers = {
        name: OutcomeProvider(name, [make_outcome(name)] * 3) for name in ("openai", "gemini")
    }
    # Synthetic static qualities deliberately put the measured target second.
    candidates = [
        ModelCandidate("openai", "openai-model", 0.8, 0.5, 0.5, 0.9),
        ModelCandidate("gemini", "gemini-model", 0.9, 0.5, 0.5, 0.9),
    ]
    route = ModelRouter(
        providers,
        candidates,
        store,
        health_store=store,
        quality_source=BenchmarkQualityResolver(suite, benchmarks, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    service = GatewayService(
        route,
        RequestLogStore(),
        store,
        ProviderHealthManager(store, failure_threshold=3, cooldown=timedelta(seconds=60)),
        probes=HalfOpenProbeCoordinator(),
    )
    return store, benchmarks, service, providers


@pytest.mark.parametrize("count,confidence,selected", [(50, 1, "openai"), (3, 0, "gemini")])
def test_cli_to_reopened_store_api_quality_router_chat_and_health(
    tmp_path,
    monkeypatch,
    capsys,
    count,
    confidence,
    selected,
):
    args, benchmark_provider, settings, _ = prepare_cli(tmp_path, monkeypatch, count=count)
    suite_path = Path(args[args.index("--suite") + 1])
    settings.quality_suite_path = suite_path
    settings.metrics_db_path.parent.mkdir(parents=True)
    initial = SQLiteMetricsStore(settings.metrics_db_path)
    before = rows(settings.metrics_db_path)
    assert cli.main([*args, "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert rows(settings.metrics_db_path) == before
    assert initial.list_provider_metrics() == []
    loaded = load_benchmark_suite(suite_path)
    store, benchmarks, service, providers = wire(settings, loaded)
    run = benchmarks.get_run(summary["run_id"])
    assert run is not None and len(benchmark_provider.calls) == count
    assert [
        [(m.role, m.content) for m in request.messages] for request in benchmark_provider.calls
    ] == [[(m.role, m.content) for m in case.messages] for case in loaded.cases]
    assert len(run.case_results) == count
    benchmark_before = rows(settings.metrics_db_path, ("benchmark_runs", "benchmark_case_results"))
    with TestClient(create_app(settings, service, benchmark_store=benchmarks)) as client:
        listing = client.get("/v1/benchmarks/runs").json()
        detail = client.get(f"/v1/benchmarks/runs/{run.run_id}").json()
        quality = client.get(
            "/v1/benchmarks/quality",
            params=TARGET.model_dump(),
        ).json()
        assert listing[0]["run_id"] == summary["run_id"] == detail["run_id"]
        assert detail["suite_fingerprint"] == quality["suite_fingerprint"] == run.suite_fingerprint
        assert quality["source_run_ids"] == [run.run_id]
        assert quality["quality_score"] == 1 and quality["confidence"] == confidence
        assert quality["coverage"] == quality["execution_completeness"] == 1
        for index, case in enumerate(detail["case_results"]):
            assert case.pop("case_index") == index
            assert case == run.case_results[index].model_dump(mode="json")
        assert rows(settings.metrics_db_path) == before
        assert all(provider.calls == 0 for provider in providers.values())
        assert service.router.rank("auto", QUALITY)[0].candidate.provider == selected
        response = client.post("/v1/chat/completions", json=CHAT)
        assert response.status_code == 200
        data = response.json()
        assert {"id", "object", "model", "choices"} <= data.keys()
        assert data["modelpilot"]["provider"] == selected
        decision = SQLiteMetricsStore(settings.metrics_db_path).get_routing_decision(
            data["modelpilot"]["request_id"],
        )
        target_signal = next(s for s in decision.explanation.candidates if s.provider == "openai")
        assert target_signal.quality_score == (1 if confidence else 0.8)
        assert target_signal.benchmark_confidence == confidence
        assert target_signal.benchmark_source_run_ids == (run.run_id,)
        assert len(rows(settings.metrics_db_path)["attempts"]) == 1
        assert len(rows(settings.metrics_db_path)["routing_decisions"]) == 1
        assert len(rows(settings.metrics_db_path)["provider_health"]) == 1

        store.upsert_health(opened())
        frozen = rows(settings.metrics_db_path)
        calls = {name: p.calls for name, p in providers.items()}
        service.router.clock = lambda: NOW + timedelta(seconds=60)
        # GET exposes HALF_OPEN eligibility but neither writes state nor acquires a probe.
        health = client.get("/v1/health/providers").json()
        assert next(h for h in health if h["provider"] == "openai")["state"] == "HALF_OPEN"
        assert client.get("/v1/benchmarks/runs").status_code == 200
        assert client.get("/v1/benchmarks/quality", params=TARGET.model_dump()).status_code == 200
        assert rows(settings.metrics_db_path) == frozen
        assert not service.probes.is_in_flight("openai", "openai-model")
        assert {name: p.calls for name, p in providers.items()} == calls
        service.router.clock = lambda: NOW
        gated = client.post("/v1/chat/completions", json=CHAT)
        assert gated.json()["modelpilot"]["provider"] == "gemini"
        excluded = gated.json()["modelpilot"]["routing"]["excluded_candidates"]
        assert excluded[0]["provider"] == "openai"
        assert excluded[0]["health"]["reason"] == "circuit_open"
        assert providers["openai"].calls == calls["openai"]
    assert rows(settings.metrics_db_path, ("benchmark_runs", "benchmark_case_results")) == (
        benchmark_before
    )
    assert len(rows(settings.metrics_db_path)["attempts"]) == 2
    assert len(rows(settings.metrics_db_path)["routing_decisions"]) == 2


def test_repeated_cli_runs_latest_failures_and_auth_partial_evidence(
    tmp_path,
    monkeypatch,
    capsys,
):
    values = [outcome()] * 45 + [outcome("wrong")] * 5 + [outcome(error="timeout")] * 10
    args, _, settings, _ = prepare_cli(tmp_path, monkeypatch, count=60, outcomes=values)
    loaded = load_benchmark_suite(args[args.index("--suite") + 1])
    monkeypatch.setattr(
        cli,
        "BenchmarkRunner",
        lambda: BenchmarkRunner(clock=lambda: NOW, id_factory=lambda: "release-first"),
    )
    assert cli.main(args) == 0
    benchmarks = SQLiteBenchmarkStore(settings.metrics_db_path)
    store, _, service, _ = wire(settings, loaded)
    with TestClient(create_app(settings, service, benchmark_store=benchmarks)) as client:
        detail = client.get("/v1/benchmarks/runs/release-first").json()
        assert detail["case_results"][45]["evaluation"]["score"] == 0
        assert detail["case_results"][50]["evaluation"] is None
        assert detail["case_results"][50]["error_type"] == "timeout"
        quality = client.get("/v1/benchmarks/quality", params=TARGET.model_dump()).json()
        assert quality["quality_score"] == 0.9
        assert quality["confidence"] == pytest.approx((50 / 60) ** 2)
        assert quality["categories"][0]["confidence"] == quality["confidence"]
        frozen = rows(settings.metrics_db_path)
        for index in (1, 2):
            provider = FakeProvider(values)
            monkeypatch.setattr(cli, "create_provider", lambda *args, value=provider: value)
            monkeypatch.setattr(
                cli,
                "BenchmarkRunner",
                lambda sequence=index: BenchmarkRunner(
                    clock=lambda: NOW + timedelta(seconds=sequence),
                    id_factory=lambda: f"release-repeat-{sequence}",
                ),
            )
            assert cli.main(args) == 0
        repeated = client.get("/v1/benchmarks/quality", params=TARGET.model_dump()).json()
        assert repeated["evaluated_cases"] == 50
        assert repeated["confidence"] == quality["confidence"]
        assert repeated["source_run_ids"] == ["release-repeat-2"]
        auth = FakeProvider([outcome(error="authentication_error")])
        monkeypatch.setattr(cli, "create_provider", lambda *args: auth)
        monkeypatch.setattr(
            cli,
            "BenchmarkRunner",
            lambda: BenchmarkRunner(
                clock=lambda: NOW + timedelta(seconds=3),
                id_factory=lambda: "release-auth",
            ),
        )
        assert cli.main(args) == 3
        partial = client.get("/v1/benchmarks/runs/release-auth").json()
        assert partial["terminated_early"] and len(partial["case_results"]) == 1
        assert partial["case_results"][0]["evaluation"] is None
        current = client.get("/v1/benchmarks/quality", params=TARGET.model_dump()).json()
        assert current["evaluated_cases"] == 49
        assert current["quality_score"] == pytest.approx(44 / 49)
        assert current["coverage"] == 1
        assert current["execution_completeness"] == 49 / 60
        assert current["confidence"] == pytest.approx((44 / 45) * (49 / 60) ** 2)
        assert current["latest_run_id"] == "release-auth"
        assert current["latest_run_completeness"] == 0
        assert current["latest_run_coverage"] == 1 / 60
        assert set(current["source_run_ids"]) == {"release-auth", "release-repeat-2"}
        assert rows(settings.metrics_db_path) == frozen
        assert store.list_provider_metrics() == []
    assert "authentication_error" in capsys.readouterr().out


def run_backup(source, destination):
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_restores_committed_wal_records_without_plain_file_copy(tmp_path):
    source = tmp_path / "live.sqlite"
    store = SQLiteBenchmarkStore(source)
    baseline = make_run(definition(2), run_id="before-wal")
    latest = make_run(definition(2), [0, "rate_limit"], run_id="committed-in-wal")
    store.save_run(baseline)
    with closing(sqlite3.connect(source)) as anchor:
        anchor.execute("PRAGMA wal_autocheckpoint = 0")
        anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.save_run(latest)
        assert Path(str(source) + "-wal").stat().st_size > 0
        before = tuple(anchor.iterdump())
        backup = tmp_path / "backup.sqlite"
        command = run_backup(source, backup)
        assert command.returncode == 0 and "schema=4" in command.stdout
        assert tuple(anchor.iterdump()) == before
        with closing(sqlite3.connect(backup)) as snapshot:
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert tuple(snapshot.iterdump()) == before
    restored = tmp_path / "restored.sqlite"
    assert run_backup(backup, restored).returncode == 0
    reopened = SQLiteBenchmarkStore(restored)
    assert reopened.get_run(latest.run_id) == latest
    assert reopened.get_run(baseline.run_id) == baseline
    assert reopened.get_run(latest.run_id).case_results[1].evaluation is None


def test_pre_upgrade_backup_restores_schema_three_and_is_not_downgrade(tmp_path):
    source = tmp_path / "schema-three.sqlite"
    legacy = legacy_database(source, 3)
    with legacy._connect() as connection:
        original = tuple(connection.iterdump())
    backup = tmp_path / "before-upgrade.sqlite"
    assert run_backup(source, backup).returncode == 0
    migrated = SQLiteBenchmarkStore(source)
    migrated.save_run(make_run(definition(), run_id="post-upgrade"))
    restored = tmp_path / "rollback-copy.sqlite"
    assert run_backup(backup, restored).returncode == 0
    # A rollback copy stays schema 3; do not initialize the current schema-4 store on it.
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (3,)
        assert tuple(connection.iterdump()) == original
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'benchmark_%'",
            ).fetchall()
            == []
        )
    legacy_copy = object.__new__(SQLiteMetricsStore)
    legacy_copy._database, legacy_copy._timeout_seconds = str(restored), 5.0
    assert legacy_copy.list_pricing() == legacy.list_pricing()
    with legacy._connect() as connection:
        request_id = connection.execute("SELECT request_id FROM routing_decisions").fetchone()[0]
    assert legacy_copy.get_routing_decision(request_id) == legacy.get_routing_decision(request_id)
    assert legacy_copy.get_health("openai", "openai-model") == legacy.get_health(
        "openai",
        "openai-model",
    )
    with migrated._connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert migrated.get_run("post-upgrade") is not None


@pytest.mark.parametrize("kind", ["existing", "same", "wal", "missing-source", "invalid-source"])
def test_backup_refuses_unsafe_destinations_without_overwriting(tmp_path, kind):
    source, destination = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    SQLiteBenchmarkStore(source)
    before = source.read_bytes()
    if kind == "existing":
        destination.write_bytes(b"preserve-existing-data")
    elif kind == "same":
        destination = source
    elif kind == "wal":
        Path(str(destination) + "-wal").write_bytes(b"preserve-sidecar")
    elif kind == "missing-source":
        source = tmp_path / "missing.sqlite"
    else:
        source = tmp_path / "not-sqlite"
        source.write_bytes(b"invalid database")
    command = run_backup(source, destination)
    assert command.returncode == 1
    if kind == "existing":
        assert destination.read_bytes() == b"preserve-existing-data"
    elif kind == "same":
        assert source.read_bytes() == before
    else:
        assert not destination.exists()
    if kind == "wal":
        assert Path(str(destination) + "-wal").read_bytes() == b"preserve-sidecar"


def test_bounded_synthetic_history_read_measurement(tmp_path, capsys):
    suite = definition(50)
    store = SQLiteBenchmarkStore(tmp_path / "synthetic-history.sqlite")
    for index in range(100):
        store.save_run(
            make_run(
                suite,
                run_id=f"synthetic-{index:03}",
                finished=NOW + timedelta(seconds=index),
            )
        )
    resolver = BenchmarkQualityResolver(suite, store)
    timings = []
    for _ in range(3):
        start = perf_counter()
        snapshot = resolver.get_quality_snapshot("fake", "model-a")
        timings.append(perf_counter() - start)
        assert snapshot.evaluated_cases == 50 and snapshot.confidence == 1
        assert snapshot.source_run_ids == ("synthetic-099",)
    # Record actual bounded synthetic timing, never a flaky hardware-dependent threshold.
    with capsys.disabled():
        print(
            f"\nSynthetic history only: Python {platform.python_version()}, "
            f"SQLite {sqlite3.sqlite_version}, {platform.system()}, 100 runs x 50 cases; "
            f"resolver seconds={','.join(f'{value:.4f}' for value in timings)}"
        )
