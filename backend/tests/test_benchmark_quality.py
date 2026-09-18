import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError
from test_health_aware_routing import opened
from test_metrics_api import make_client
from test_routing_explanations import make_decision
from test_sqlite_metrics_store import make_attempt, make_pricing

from modelpilot.benchmarks import (
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkCategory,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkSuite,
    BenchmarkTarget,
    CaseEvaluation,
    QualitySnapshot,
    SQLiteBenchmarkStore,
    aggregate_quality,
    load_benchmark_suite,
)
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers.base import ProviderErrorType
from modelpilot.sqlite_database import SCHEMA_VERSION

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)
TARGET = BenchmarkTarget(provider="fake", model="model-a")


def definition(n=1, weights=None, categories=None):
    return BenchmarkSuite(
        suite_id="quality-test",
        version="1",
        name="Quality fixtures",
        cases=tuple(
            BenchmarkCase(
                case_id=f"case-{i}",
                category=categories[i] if categories else "math",
                weight=weights[i] if weights else 1.0,
                messages=[{"role": "user", "content": "fixture prompt"}],
                evaluator={"kind": "exact_match", "expected_text": "ok"},
            )
            for i in range(n)
        ),
    )


def make_run(suite, values=None, *, run_id="run-a", finished=NOW):
    values = values if values is not None else [1.0] * len(suite.cases)
    results = []
    for case, value in zip(suite.cases, values, strict=False):
        error = value if isinstance(value, str) else None
        evaluation = (
            None
            if error
            else CaseEvaluation(
                evaluator_kind=case.evaluator.kind,
                score=float(value),
                passed=value == 1,
                reason="match" if value == 1 else "mismatch",
            )
        )
        results.append(
            BenchmarkCaseResult(
                provider=TARGET.provider,
                model=TARGET.model,
                case_id=case.case_id,
                category=case.category,
                latency_ms=10.0,
                error_type=error,
                execution_status="provider_failed" if error else "completed",
                evaluation=evaluation,
            )
        )
    failed = sum(r.evaluation is None for r in results)
    return BenchmarkRun(
        provider=TARGET.provider,
        model=TARGET.model,
        run_id=run_id,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_fingerprint=sha256(suite.model_dump_json().encode()).hexdigest(),
        config=BenchmarkRunConfig(),
        started_at=finished - timedelta(seconds=1),
        finished_at=finished,
        status="completed_with_failures" if failed else "completed",
        total_cases=len(suite.cases),
        completed_cases=len(results) - failed,
        execution_failed_cases=failed,
        terminated_early=len(results) < len(suite.cases),
        case_results=tuple(results),
    )


def aggregate(suite, runs):
    return aggregate_quality(suite, runs, target=TARGET, generated_at=NOW)


@pytest.mark.parametrize("score", [0, 0.25, 0.5, 0.99, 1])
def test_single_evaluated_answer(score):
    suite = definition()
    result = aggregate(suite, [make_run(suite, [score])])
    assert result.quality_score == score
    assert result.coverage == result.execution_completeness == 1
    assert result.confidence == 0 and result.evaluated_cases == 1
    assert result.source_run_ids == ("run-a",)


@pytest.mark.parametrize("error", list(ProviderErrorType))
def test_execution_failures_not_quality_zeros(error):
    suite = definition(2)
    result = aggregate(suite, [make_run(suite, [1, error.value])])
    assert result.quality_score == 1
    assert result.coverage == 1 and result.execution_completeness == 0.5
    assert result.evaluated_cases == result.execution_failed_cases == 1
    failed = aggregate(definition(), [make_run(definition(), [error.value])])
    assert failed.quality_score is None and failed.confidence == 0


def test_weighted_quality_excludes_failed_weight_but_not_wrong_answers():
    suite = definition(3, weights=[3.0, 1.0, 6.0])
    result = aggregate(suite, [make_run(suite, [1, 0, "timeout"])])
    assert result.quality_score == 0.75
    assert result.weighted_evaluation_coverage == 0.4
    assert result.execution_completeness == 2 / 3


@pytest.mark.parametrize("weights", [[1e308, 1e308], [5e-324, 5e-324], [5e-324, 1e308]])
def test_extreme_finite_weights_do_not_overflow_or_divide_by_zero(weights):
    suite = definition(2, weights=weights)
    result = aggregate(suite, [make_run(suite, [1, 0])])
    assert 0 <= result.quality_score <= 1
    assert result.weighted_evaluation_coverage == 1
    result = aggregate(suite, [make_run(suite, [1, "timeout"])])
    assert result.quality_score == 1


@pytest.mark.parametrize(
    "n,expected",
    [(1, 0), (3, 0), (4, 0), (5, 0), (6, 1 / 45), (20, 15 / 45), (49, 44 / 45), (50, 1), (100, 1)],
)
@pytest.mark.parametrize("score", [0, 1])
def test_confidence_ramp_is_independent_of_quality(n, expected, score):
    suite = definition(n)
    result = aggregate(suite, [make_run(suite, [score] * n)])
    assert result.confidence == pytest.approx(expected)
    assert result.quality_score == score and result.execution_completeness == 1


def test_failure_heavy_run_and_weighted_confidence():
    suite = definition(100)
    result = aggregate(suite, [make_run(suite, [1] * 10 + ["timeout"] * 90)])
    assert result.quality_score == 1
    assert result.execution_completeness == result.weighted_evaluation_coverage == 0.1
    assert result.confidence == pytest.approx((5 / 45) * 0.1 * 0.1)
    heavy = definition(51, weights=[1.0] * 50 + [50.0])
    result = aggregate(heavy, [make_run(heavy, [1] * 50 + ["timeout"])])
    assert result.confidence == pytest.approx(0.5 * 50 / 51)


def test_auth_fail_fast_diagnostics():
    suite = definition(100)
    result = aggregate(suite, [make_run(suite, ["authentication_error"])])
    assert result.quality_score is None and result.confidence == 0
    assert result.coverage == result.latest_run_coverage == 0.01
    assert result.execution_completeness == result.latest_run_completeness == 0
    assert result.observed_cases == 1 and result.execution_failed_cases == 1


def test_partial_run_with_answers_has_observed_and_evaluated_denominators():
    suite = definition(100)
    result = aggregate(suite, [make_run(suite, [1] * 10 + ["authentication_error"])])
    assert result.coverage == 0.11 and result.execution_completeness == 0.1
    assert result.quality_score == 1
    assert result.confidence == pytest.approx((5 / 45) * 0.1 * 0.1)


@pytest.mark.parametrize("latest_value,quality", [(0, 0), ("timeout", None), (1, 1)])
def test_latest_attempt_wins_not_latest_success(latest_value, quality):
    suite = definition()
    older = make_run(suite, [1], run_id="z-old")
    newer = make_run(suite, [latest_value], run_id="a-new", finished=NOW + timedelta(seconds=1))
    result = aggregate(suite, [newer, older])
    assert result.quality_score == quality and result.source_run_ids == ("a-new",)
    assert result.execution_completeness == (0 if quality is None else 1)


def test_latest_correct_replaces_old_failure():
    suite = definition()
    result = aggregate(
        suite, [make_run(suite, ["timeout"], run_id="a"), make_run(suite, [1], run_id="b")]
    )
    assert result.quality_score == 1 and result.source_run_ids == ("b",)


def test_repeated_runs_do_not_inflate_evidence_and_ties_are_stable():
    suite = definition(3)
    runs = [make_run(suite, run_id=f"run-{i:03}") for i in range(100)]
    result = aggregate(suite, runs)
    assert result.evaluated_cases == 3 and result.confidence == 0
    assert result.source_run_count == 1 and result.source_run_ids == ("run-099",)
    assert aggregate(suite, list(reversed(runs))) == result
    assert aggregate(suite, runs + runs) == result


def test_new_auth_failure_preserves_only_unattempted_historical_evidence():
    suite = definition(50)
    older = make_run(suite, run_id="a")
    newer = make_run(suite, ["authentication_error"], run_id="b")
    result = aggregate(suite, [older, newer])
    assert result.quality_score == 1 and result.evaluated_cases == 49
    assert result.coverage == 1 and result.execution_completeness == 49 / 50
    assert result.latest_run_id == "b" and result.latest_run_completeness == 0
    assert result.latest_run_coverage == 1 / 50
    assert result.source_run_ids == ("a", "b") and result.source_run_count == 2
    assert result.confidence == pytest.approx((44 / 45) * (49 / 50) ** 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("suite_id", "other"),
        ("suite_version", "2"),
        ("suite_fingerprint", "a" * 64),
        ("provider", "other"),
        ("model", "other"),
    ],
)
def test_mixed_identities_rejected(field, value):
    suite = definition()
    invalid = make_run(suite).model_copy(update={field: value})
    with pytest.raises(ValueError):
        aggregate(suite, [make_run(suite, run_id="valid-newer"), invalid])


def test_changed_definition_fingerprint_rejected():
    suite = definition()
    original = make_run(suite)
    changed = suite.model_copy(
        update={"cases": (suite.cases[0].model_copy(update={"weight": 2.0}),)}
    )
    with pytest.raises(ValueError, match="fingerprint"):
        aggregate(changed, [original])


def test_incompatible_config_and_conflicting_duplicate_id_rejected():
    suite = definition()
    first = make_run(suite)
    second = make_run(suite, [0])
    with pytest.raises(ValueError, match="duplicate"):
        aggregate(suite, [first, second])
    changed = first.model_copy(update={"config": BenchmarkRunConfig(temperature=1)})
    with pytest.raises(ValueError, match="configuration"):
        aggregate(suite, [first, changed])


def test_categories_have_own_counts_weights_and_first_occurrence_order():
    suite = definition(
        52, weights=[3.0, 1.0] + [1.0] * 50, categories=["coding", "coding"] + ["math"] * 50
    )
    result = aggregate(suite, [make_run(suite, [1, 0] + [0.5] * 50)])
    coding, math = result.categories
    assert [c.category for c in result.categories] == ["coding", "math"]
    assert coding.quality_score == 0.75 and coding.confidence == 0
    assert math.quality_score == 0.5 and math.confidence == 1
    assert result.quality_score == pytest.approx(28 / 54)


def test_category_no_evaluations_and_unobserved_category():
    suite = definition(3, categories=["coding", "math", "reasoning"])
    result = aggregate(suite, [make_run(suite, [1, "authentication_error"])])
    coding, math, reasoning = result.categories
    assert coding.quality_score == 1
    assert math.quality_score is None and math.coverage == 1 and math.execution_completeness == 0
    assert reasoning.quality_score is None and reasoning.coverage == reasoning.confidence == 0


def test_empty_runs_has_definition_categories_but_no_invented_evidence():
    suite = definition(2, categories=["math", "coding"])
    result = aggregate(suite, [])
    assert result.quality_score is None and result.confidence == result.coverage == 0
    assert result.source_run_count == 0 and result.source_run_ids == ()
    assert result.latest_run_id is result.latest_run_completeness is result.run_config is None
    assert [c.category for c in result.categories] == ["math", "coding"]


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "duplicate",
        "category",
        "order",
        "evaluator",
        "count",
        "missing",
        "negative",
        "nan",
        "infinity",
    ],
)
def test_corrupt_runs_rejected_even_if_superseded(change):
    suite = definition(2)
    original = make_run(suite)
    cases = list(original.case_results)
    updates = {}
    if change == "unknown":
        cases[0] = cases[0].model_copy(update={"case_id": "unknown"})
    elif change == "duplicate":
        cases[1] = cases[0]
    elif change == "category":
        cases[0] = cases[0].model_copy(update={"category": BenchmarkCategory.CODING})
    elif change == "order":
        cases.reverse()
    elif change == "evaluator":
        evaluation = cases[0].evaluation.model_copy(update={"evaluator_kind": "contains"})
        cases[0] = cases[0].model_copy(update={"evaluation": evaluation})
    elif change == "count":
        updates["total_cases"] = 3
    elif change == "missing":
        cases.pop()
    else:
        score = {"negative": -1, "nan": float("nan"), "infinity": float("inf")}[change]
        cases[0] = cases[0].model_copy(
            update={
                "evaluation": cases[0].evaluation.model_copy(update={"score": score}),
            }
        )
    broken = original.model_copy(update={"case_results": tuple(cases), **updates})
    with pytest.raises(ValueError):
        aggregate(suite, [broken, make_run(suite, run_id="z-new")])


@pytest.mark.parametrize(
    "field",
    [
        "quality_score",
        "coverage",
        "execution_completeness",
        "confidence",
        "weighted_evaluation_coverage",
    ],
)
@pytest.mark.parametrize("value", [-1, 1.1, float("nan"), float("inf")])
def test_snapshot_ratio_validation(field, value):
    result = aggregate(definition(), [make_run(definition())])
    with pytest.raises(ValidationError):
        QualitySnapshot.model_validate({**result.model_dump(), field: value})


def test_serialization_utc_and_determinism():
    suite = definition(10)
    runs = [make_run(suite)]
    result = aggregate(suite, runs)
    assert QualitySnapshot.model_validate_json(result.model_dump_json()) == result
    offset = NOW.astimezone(timezone(timedelta(hours=8)))
    assert aggregate_quality(suite, runs, target=TARGET, generated_at=offset) == result
    later = aggregate_quality(suite, runs, target=TARGET, generated_at=NOW + timedelta(seconds=1))
    assert later.model_dump(exclude={"generated_at"}) == result.model_dump(exclude={"generated_at"})
    with pytest.raises(ValidationError):
        aggregate_quality(suite, runs, target=TARGET, generated_at=NOW.replace(tzinfo=None))


def test_smoke_confidence_guard():
    from pathlib import Path

    suite = load_benchmark_suite(
        Path(__file__).resolve().parents[2] / "benchmarks/suites/smoke-v1.json"
    )
    result = aggregate(suite, [make_run(suite)])
    assert result.quality_score == 1 and result.evaluated_cases == 3
    assert result.confidence == 0


def test_store_round_trip_aggregation_is_read_only_and_production_isolated(tmp_path, monkeypatch):
    path = tmp_path / "modelpilot.db"
    production = SQLiteMetricsStore(path)
    production.record_attempt(make_attempt())
    production.upsert_pricing(make_pricing())
    production.record_routing_decision(make_decision())
    production.upsert_health(opened())
    suite = definition(50)
    benchmark = SQLiteBenchmarkStore(path)
    benchmark.save_run(make_run(suite))
    loaded = benchmark.list_runs()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("modelpilot.metrics.api.datetime", FixedDatetime)
    monkeypatch.setattr("modelpilot.metrics.sqlite_store.datetime", FixedDatetime)
    with production._connect() as connection:
        before = tuple(connection.iterdump())
    metrics = production.list_provider_metrics()

    def forbidden(*args, **kwargs):
        pytest.fail("aggregation attempted I/O or production integration")

    with make_client(production) as client:
        response = client.get("/v1/metrics/summary")
        assert response.status_code == 200
        with monkeypatch.context() as guard:
            guard.setattr(sqlite3, "connect", forbidden)
            guard.setattr("httpx.AsyncClient.post", forbidden)
            guard.setattr("modelpilot.benchmarks.runner.BenchmarkRunner.run", forbidden)
            guard.setattr("modelpilot.router.ModelRouter.rank_with_explanation", forbidden)
            guard.setattr("modelpilot.health.manager.ProviderHealthManager.update", forbidden)
            result = aggregate(suite, loaded)
            assert result.quality_score == result.confidence == 1
        assert client.get("/v1/metrics/summary").json() == response.json()
    assert SQLiteMetricsStore(path).list_provider_metrics() == metrics
    with production._connect() as connection:
        assert tuple(connection.iterdump()) == before
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert SCHEMA_VERSION == 4
