import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_attempt_recording import NOW, OutcomeProvider, make_outcome
from test_benchmark_quality import definition, make_run
from test_dynamic_router import DynamicProvider, SnapshotStore, make_snapshot
from test_health_aware_routing import HealthStore, opened

from modelpilot.benchmarks import (
    BenchmarkTarget,
    SQLiteBenchmarkStore,
    aggregate_quality,
    load_benchmark_suite,
)
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import build_gateway, create_app
from modelpilot.metrics import RoutingDecision, RoutingExplanation, SQLiteMetricsStore
from modelpilot.router import ModelCandidate, ModelRouter, blend_score
from modelpilot.schemas import RoutingPreferences
from modelpilot.service import GatewayService

QUALITY = RoutingPreferences(quality=1, cost=0, latency=0, reliability=0)
CANDIDATES = [
    ModelCandidate("openai", "openai-model", 0.9, 0.5, 0.6, 0.8),
    ModelCandidate("gemini", "gemini-model", 0.8, 0.7, 0.8, 0.9),
]
SMOKE = Path(__file__).resolve().parents[2] / "benchmarks/suites/smoke-v1.json"


def targeted_run(suite, values=None, *, provider="gemini", run_id="run-a", **kwargs):
    kwargs.setdefault("finished", NOW)
    original = make_run(suite, values, run_id=run_id, **kwargs)
    return original.model_copy(
        update={
            "provider": provider,
            "model": f"{provider}-model",
            "case_results": tuple(
                c.model_copy(update={"provider": provider, "model": f"{provider}-model"})
                for c in original.case_results
            ),
        }
    )


def snapshot(suite, values=None):
    return aggregate_quality(
        suite,
        [targeted_run(suite, values)],
        target=BenchmarkTarget(provider="gemini", model="gemini-model"),
        generated_at=NOW,
    )


class Source:
    def __init__(self, result, *, fail=False):
        self.result = result
        self.fail = fail
        self.calls = []
        self.suite_identity = (result.suite_id, result.suite_version, result.suite_fingerprint)

    def get_quality_snapshot(self, provider, model):
        self.calls.append((provider, model))
        if self.fail:
            raise RuntimeError("Authorization: Bearer SECRET PRIVATE/path api_key=SECRET")
        return self.result if provider == "gemini" else None


def router(source=None, *, health=None, metrics=None, now=NOW):
    return ModelRouter(
        {c.provider: DynamicProvider(c.provider) for c in CANDIDATES},
        CANDIDATES,
        metrics=metrics,
        health_store=health,
        quality_source=source,
        clock=lambda: now,
    )


def signal(router, preferences=QUALITY):
    return next(
        item.signal
        for item in router.rank("auto", preferences)
        if item.candidate.provider == "gemini"
    )


@pytest.mark.parametrize("n,values", [(1, ["timeout"]), (3, [1] * 3), (5, [1] * 5)])
def test_missing_null_and_zero_confidence_keep_static(n, values):
    baseline = signal(router())
    assert baseline.quality_score == 0.8 and baseline.quality_source == "static"
    source = Source(snapshot(definition(n), values))
    result = signal(router(source))
    assert result.quality_score == 0.8 and result.final_score == baseline.final_score
    assert result.benchmark_confidence == 0
    assert result.sources["quality"] == "configured"


def test_no_runs_resolver_returns_none_and_static(tmp_path):
    resolver = BenchmarkQualityResolver(definition(), SQLiteBenchmarkStore(tmp_path / "db"))
    assert resolver.get_quality_snapshot("gemini", "gemini-model") is None
    assert signal(router(resolver)).quality_score == 0.8


def test_half_confidence_formula_applied_exactly_once():
    suite = definition(60, weights=[3.0] * 50 + [10.0] * 10)
    measured = snapshot(suite, [1] * 50 + ["timeout"] * 10)
    assert measured.confidence == 0.5
    result = signal(router(Source(measured)))
    assert result.quality_score == 0.9
    assert result.quality_source == result.sources["quality"] == "blended"
    assert result.static_quality_score == 0.8
    assert result.benchmark_quality_score == 1 and result.benchmark_confidence == 0.5


@pytest.mark.parametrize("quality", [0, 0.25, 0.75, 1])
def test_full_confidence_replaces_only_quality(quality):
    result = signal(router(Source(snapshot(definition(50), [quality] * 50))))
    assert result.quality_score == quality and result.quality_source == "measured"
    assert (result.cost_score, result.latency_score, result.reliability_score) == (0.7, 0.8, 0.9)
    assert result.benchmark_source_run_ids == ("run-a",)
    assert result.benchmark_generated_at == NOW


@pytest.mark.parametrize("n", [6, 10, 20, 49])
def test_partial_confidence_exact_formula_and_bounds(n):
    measured = snapshot(definition(n), [0] * n)
    result = signal(router(Source(measured)))
    assert result.quality_score == blend_score(0.8, 0, measured.confidence)
    assert 0 <= result.quality_score <= 1


def test_failure_heavy_benchmark_is_weak_evidence():
    measured = snapshot(definition(100), [1] * 10 + ["timeout"] * 90)
    result = signal(router(Source(measured)))
    assert 0.8 < result.quality_score < 0.801


def test_smoke_perfect_results_do_not_change_ranking():
    suite = load_benchmark_suite(SMOKE)
    measured = snapshot(suite)
    assert measured.quality_score == 1 and measured.confidence == 0
    before = [(r.candidate.provider, r.score) for r in router().rank("auto", QUALITY)]
    after = [
        (r.candidate.provider, r.score) for r in router(Source(measured)).rank("auto", QUALITY)
    ]
    assert after == before


@pytest.mark.parametrize("dimension", ["cost", "latency", "reliability"])
def test_other_measured_dimensions_and_preference_rankings_unchanged(dimension):
    metrics = SnapshotStore(
        {(c.provider, c.model): make_snapshot(c.provider, c.model) for c in CANDIDATES}
    )
    preferences = RoutingPreferences(
        **{d: int(d == dimension) for d in ("quality", "cost", "latency", "reliability")}
    )
    before = router(metrics=metrics).rank("auto", preferences)
    after = router(Source(snapshot(definition(50))), metrics=metrics).rank("auto", preferences)
    assert [(r.candidate, r.score) for r in after] == [(r.candidate, r.score) for r in before]
    for a, b in zip(after, before, strict=True):
        for field in (
            "latency_score",
            "reliability_score",
            "cost_score",
            "measured_confidence",
            "measured_sample_count",
            "reason_metadata",
        ):
            assert getattr(a.signal, field) == getattr(b.signal, field)


def test_final_weighted_formula_and_determinism():
    route = router(Source(snapshot(definition(50), [0.4] * 50)))
    prefs = RoutingPreferences(quality=2, cost=3, latency=4, reliability=1)
    result = signal(route, prefs)
    assert result.final_score == round(0.4 * 0.2 + 0.7 * 0.3 + 0.8 * 0.4 + 0.9 * 0.1, 6)
    assert route.rank("auto", prefs) == route.rank("auto", prefs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "wrong"),
        ("model", "wrong"),
        ("suite_id", "wrong"),
        ("suite_version", "wrong"),
        ("suite_fingerprint", "b" * 64),
    ],
)
def test_wrong_snapshot_identity_fail_static(field, value, caplog):
    source = Source(snapshot(definition(50)))
    source.result = source.result.model_copy(update={field: value})
    assert signal(router(source)).quality_score == 0.8
    assert "using static quality" in caplog.text


def test_source_failure_sanitized_warning_and_unchanged_other_dimensions(caplog):
    source = Source(snapshot(definition()), fail=True)
    result = signal(router(source))
    assert result.quality_score == 0.8
    assert (result.cost_score, result.latency_score, result.reliability_score) == (0.7, 0.8, 0.9)
    assert "using static quality" in caplog.text
    assert all(s not in caplog.text for s in ("SECRET", "Authorization", "PRIVATE/path"))


def test_health_gate_skips_quality_queries_and_all_open_has_no_ranked_candidates():
    source = Source(snapshot(definition(50)))
    health = HealthStore([opened("gemini", "gemini-model")])
    route = router(source, health=health)
    ranked, explanation = route.rank_with_explanation("auto", QUALITY, "request")
    assert [r.candidate.provider for r in ranked] == ["openai"]
    assert source.calls == [("openai", "openai-model")]
    assert explanation.excluded_candidates[0].health.reason == "circuit_open"
    health.records[("openai", "openai-model")] = opened()
    source.calls.clear()
    assert route.rank("auto", QUALITY) == []
    assert source.calls == []


def test_half_open_no_quality_bonus_or_penalty():
    source = Source(snapshot(definition(50)))
    half = router(
        source,
        health=HealthStore([opened("gemini", "gemini-model")]),
        now=NOW + timedelta(seconds=60),
    )
    assert signal(half).quality_score == signal(router(source)).quality_score
    assert signal(half).health.state.value == "HALF_OPEN"


def test_one_lookup_per_pair_per_evaluation_and_explicit_model_never_reads():
    source = Source(snapshot(definition(50)))
    route = router(source)
    route.candidates = CANDIDATES + CANDIDATES
    route.rank("auto", QUALITY)
    assert source.calls == [("openai", "openai-model"), ("gemini", "gemini-model")]
    source.calls.clear()
    ranked, explanation = route.rank_with_explanation("gemini-model", QUALITY, "request")
    assert not source.calls and explanation is None
    assert ranked[0].signal.quality_score == 0.8


def test_overall_not_category_quality_used():
    suite = definition(100, categories=["coding"] * 50 + ["math"] * 50)
    measured = snapshot(suite, [0] * 50 + [1] * 50)
    assert measured.categories[0].quality_score == 0
    assert signal(router(Source(measured))).quality_score == 0.5


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unconfigured_or_blank_suite_valid_and_disabled(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("MODELPILOT_QUALITY_SUITE_PATH", raising=False)
    else:
        monkeypatch.setenv("MODELPILOT_QUALITY_SUITE_PATH", value)
    settings = Settings.from_env().model_copy(update={"metrics_db_path": tmp_path / "db"})
    assert settings.quality_suite_path is None
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.app.state.gateway.router.quality_source is None


def test_configured_suite_loads_once_and_is_not_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELPILOT_QUALITY_SUITE_PATH", str(SMOKE))
    settings = Settings.from_env().model_copy(update={"metrics_db_path": tmp_path / "db"})
    with TestClient(create_app(settings)) as client:
        source = client.app.state.gateway.router.quality_source
        assert source.suite.suite_id == "modelpilot-smoke"

        def forbidden(*args):
            pytest.fail("suite must not reload during routing")

        monkeypatch.setattr("modelpilot.main.load_benchmark_suite", forbidden)
        assert source.get_quality_snapshot("gemini", "gemini-model") is None


@pytest.mark.parametrize("path", ["missing", "invalid", "directory"])
def test_invalid_explicit_suite_fails_startup(tmp_path, path):
    suite_path = tmp_path / path
    if path == "invalid":
        # A directory is a distinct invalid file; malformed JSON fixture uses existing source text.
        suite_path = Path(__file__)
    elif path == "directory":
        suite_path = tmp_path
    settings = Settings(metrics_db_path=tmp_path / "db", quality_suite_path=suite_path)
    with pytest.raises((OSError, ValueError)), TestClient(create_app(settings)):
        pass
    assert not settings.metrics_db_path.exists()


def test_resolver_filtered_history_not_global_limit_and_latest_failure(tmp_path):
    suite = definition(50)
    store = SQLiteBenchmarkStore(tmp_path / "db")
    store.save_run(targeted_run(suite, run_id="old"))
    for i in range(105):
        store.save_run(
            targeted_run(
                suite,
                ["authentication_error"],
                run_id=f"new-{i:03}",
                finished=NOW + timedelta(seconds=i),
            )
        )
    resolver = BenchmarkQualityResolver(suite, store, clock=lambda: NOW)
    measured = resolver.get_quality_snapshot("gemini", "gemini-model")
    assert measured.evaluated_cases == 49 and measured.execution_failed_cases == 1
    assert measured.latest_run_completeness == 0
    assert set(measured.source_run_ids) == {"old", "new-104"}
    assert signal(router(resolver)).quality_score == blend_score(0.8, 1, measured.confidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("model", "other"),
        ("suite_id", "other"),
        ("suite_version", "other"),
        ("suite_fingerprint", "c" * 64),
    ],
)
def test_sqlite_query_excludes_wrong_identity(tmp_path, field, value):
    suite = definition(50)
    store = SQLiteBenchmarkStore(tmp_path / "db")
    run = targeted_run(suite)
    updates = {field: value}
    if field in ("provider", "model"):
        updates["case_results"] = tuple(
            c.model_copy(update={field: value}) for c in run.case_results
        )
    store.save_run(run.model_copy(update=updates))
    assert (
        BenchmarkQualityResolver(suite, store).get_quality_snapshot("gemini", "gemini-model")
        is None
    )


@pytest.mark.parametrize("failure", ["store", "aggregate", "corrupt"])
def test_runtime_resolver_failure_inference_succeeds(tmp_path, monkeypatch, failure, caplog):
    suite = definition(50)
    benchmark = SQLiteBenchmarkStore(tmp_path / "db")
    benchmark.save_run(targeted_run(suite))
    source = BenchmarkQualityResolver(suite, benchmark)

    def broken(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer SECRET")

    if failure == "store":
        monkeypatch.setattr(benchmark, "list_matching_runs", broken)
    elif failure == "aggregate":
        monkeypatch.setattr("modelpilot.benchmarks.resolver.aggregate_quality", broken)
    else:
        with benchmark._connect() as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE benchmark_case_results SET evaluation_score = 2")
    service = GatewayService(router(source), RequestLogStore())
    with TestClient(create_app(gateway=service)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "modelpilot": {"preferences": QUALITY.model_dump()},
            },
        )
    assert response.status_code == 200 and response.json()["model"] == "openai-model"
    assert "using static quality" in caplog.text and "SECRET" not in caplog.text


def test_real_store_integration_ranking_health_persistence_and_no_extra_side_effects(
    tmp_path, monkeypatch
):
    suite = definition(50)
    path = tmp_path / "db"
    benchmark = SQLiteBenchmarkStore(path)
    benchmark.save_run(targeted_run(suite))
    production = SQLiteMetricsStore(path)
    source = BenchmarkQualityResolver(suite, benchmark)
    route = router()
    assert route.rank("auto", QUALITY)[0].candidate.provider == "openai"
    route.quality_source = source
    route.health_store = production
    providers = {name: OutcomeProvider(name, [make_outcome(name)]) for name in ("openai", "gemini")}
    route.providers = providers
    service = GatewayService(route, RequestLogStore(), production)

    def forbidden(*args, **kwargs):
        pytest.fail("benchmark execution or write is forbidden during inference")

    monkeypatch.setattr(benchmark, "save_run", forbidden)
    monkeypatch.setattr("modelpilot.benchmarks.runner.BenchmarkRunner.run", forbidden)
    with production._connect() as connection:
        before = tuple(connection.iterdump())
    assert route.rank("auto", QUALITY)[0].candidate.provider == "gemini"
    with production._connect() as connection:
        assert tuple(connection.iterdump()) == before
    with TestClient(create_app(gateway=service)) as client:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "coding math"}],
            "modelpilot": {"preferences": QUALITY.model_dump()},
        }
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert {"id", "object", "model", "choices"} <= payload.keys()
        assert payload["modelpilot"]["provider"] == "gemini"
        request_id = payload["modelpilot"]["request_id"]
        decision = SQLiteMetricsStore(path).get_routing_decision(request_id)
        assert decision.explanation.selected.benchmark_confidence == 1
        assert decision.explanation.selected.benchmark_source_run_ids == ("run-a",)
        production.upsert_health(opened("gemini", "gemini-model"))
        assert client.post("/v1/chat/completions", json=body).json()["model"] == "openai-model"
        production.upsert_health(opened())
        assert client.post("/v1/chat/completions", json=body).status_code == 503
    assert len(benchmark.list_runs()) == 1
    with production._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0] == 2
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4


def test_old_routing_decision_without_new_fields_remains_readable(tmp_path):
    route = router()
    _, explanation = route.rank_with_explanation("auto", QUALITY, "old-request")
    data = explanation.model_dump(mode="json")
    for signal_data in [data["selected"], *data["candidates"]]:
        for key in list(signal_data):
            if key.startswith("benchmark_") or key in ("quality_source", "static_quality_score"):
                signal_data.pop(key)
    restored = RoutingExplanation.model_validate(data)
    assert restored.selected.quality_source == "static"
    store = SQLiteMetricsStore(tmp_path / "db")
    decision = RoutingDecision.from_explanation(restored)
    store.record_routing_decision(decision)
    assert store.get_routing_decision("old-request") == decision


def test_gateway_composition_wires_resolver_without_model_calls(tmp_path):
    async def exercise():
        settings = Settings(metrics_db_path=tmp_path / "db", quality_suite_path=SMOKE)
        async with httpx.AsyncClient() as client:
            gateway = build_gateway(settings, client)
            assert isinstance(gateway.router.quality_source, BenchmarkQualityResolver)
            assert gateway.router.rank("auto", QUALITY) == []

    asyncio.run(exercise())
