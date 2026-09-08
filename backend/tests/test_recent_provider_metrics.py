from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modelpilot.metrics import AttemptRecord, SQLiteMetricsStore

NOW = datetime.now(UTC).replace(microsecond=123456)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "metrics.sqlite3"


@pytest.fixture
def store(database_path: Path) -> SQLiteMetricsStore:
    return SQLiteMetricsStore(database_path)


def make_attempt(
    index: int,
    *,
    provider: str = "openai",
    model: str = "model-a",
    success: bool = True,
    latency_ms: float = 100,
    estimated_cost: Decimal | None = Decimal("0.001"),
    finished_at: datetime | None = None,
) -> AttemptRecord:
    ended = finished_at or NOW - timedelta(seconds=index)
    return AttemptRecord(
        request_id=f"req_{provider}_{model}_{index}",
        provider=provider,
        model=model,
        started_at=ended - timedelta(milliseconds=latency_ms),
        finished_at=ended,
        latency_ms=latency_ms,
        success=success,
        estimated_cost=estimated_cost,
        created_at=ended,
    )


def record_latencies(store: SQLiteMetricsStore, values: list[float]) -> None:
    for index, latency in enumerate(values):
        store.record_attempt(make_attempt(index, latency_ms=latency))


def require_metrics(store: SQLiteMetricsStore):
    metrics = store.get_provider_metrics("openai", "model-a")
    assert metrics is not None
    return metrics


def test_empty_database_returns_no_metrics(store: SQLiteMetricsStore) -> None:
    assert store.get_provider_metrics("openai", "model-a") is None
    assert store.list_provider_metrics() == []


def test_single_successful_attempt(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, success=True))

    metrics = require_metrics(store)

    assert metrics.sample_count == 1
    assert metrics.success_count == 1
    assert metrics.failure_count == 0


def test_single_failed_attempt(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, success=False))

    metrics = require_metrics(store)

    assert metrics.sample_count == 1
    assert metrics.success_count == 0
    assert metrics.failure_count == 1


def test_success_rate_is_successes_over_samples(store: SQLiteMetricsStore) -> None:
    for index, success in enumerate([True, True, False, False]):
        store.record_attempt(make_attempt(index, success=success))

    assert require_metrics(store).success_rate == 0.5


def test_success_count_includes_only_successes(store: SQLiteMetricsStore) -> None:
    for index, success in enumerate([True, False, True]):
        store.record_attempt(make_attempt(index, success=success))

    assert require_metrics(store).success_count == 2


def test_failure_count_includes_only_failures(store: SQLiteMetricsStore) -> None:
    for index, success in enumerate([False, True, False]):
        store.record_attempt(make_attempt(index, success=success))

    assert require_metrics(store).failure_count == 2


def test_average_latency_uses_stored_values(store: SQLiteMetricsStore) -> None:
    record_latencies(store, [100, 200, 600])

    assert require_metrics(store).average_latency_ms == 300


def test_p50_uses_nearest_rank(store: SQLiteMetricsStore) -> None:
    record_latencies(store, [40, 10, 30, 20])

    assert require_metrics(store).p50_latency_ms == 20


def test_p95_uses_nearest_rank(store: SQLiteMetricsStore) -> None:
    record_latencies(store, list(range(10, 110, 10)))

    assert require_metrics(store).p95_latency_ms == 100


def test_odd_sample_percentiles_use_nearest_rank(store: SQLiteMetricsStore) -> None:
    record_latencies(store, [50, 10, 40, 20, 30])

    metrics = require_metrics(store)

    assert metrics.p50_latency_ms == 30
    assert metrics.p95_latency_ms == 50


def test_even_sample_percentiles_use_nearest_rank(store: SQLiteMetricsStore) -> None:
    record_latencies(store, [40, 10, 30, 20])

    metrics = require_metrics(store)

    assert metrics.p50_latency_ms == 20
    assert metrics.p95_latency_ms == 40


def test_window_accepts_exactly_100_recent_attempts(store: SQLiteMetricsStore) -> None:
    for index in range(100):
        store.record_attempt(make_attempt(index))

    assert require_metrics(store).sample_count == 100


def test_window_keeps_newest_100_of_101_attempts(store: SQLiteMetricsStore) -> None:
    for index in range(101):
        store.record_attempt(make_attempt(index, success=index != 100))

    metrics = require_metrics(store)

    assert metrics.sample_count == 100
    assert metrics.failure_count == 0


def test_attempt_inside_seven_day_boundary_is_included(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(
        make_attempt(0, finished_at=NOW - timedelta(days=7) + timedelta(minutes=1))
    )

    assert require_metrics(store).sample_count == 1


def test_attempt_older_than_seven_days_is_excluded(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0))
    store.record_attempt(
        make_attempt(
            1,
            success=False,
            finished_at=NOW - timedelta(days=7, minutes=1),
        )
    )

    metrics = require_metrics(store)

    assert metrics.sample_count == 1
    assert metrics.failure_count == 0


def test_provider_metrics_are_isolated(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, provider="openai", success=True))
    store.record_attempt(make_attempt(1, provider="gemini", success=False))

    metrics = require_metrics(store)

    assert metrics.provider == "openai"
    assert metrics.sample_count == 1
    assert metrics.success_rate == 1


def test_model_metrics_are_isolated(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, model="model-a", success=True))
    store.record_attempt(make_attempt(1, model="model-b", success=False))

    metrics = require_metrics(store)

    assert metrics.model == "model-a"
    assert metrics.sample_count == 1
    assert metrics.success_rate == 1


def test_nullable_cost_is_excluded_from_average(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, estimated_cost=Decimal("0.002")))
    store.record_attempt(make_attempt(1, estimated_cost=None))
    store.record_attempt(make_attempt(2, estimated_cost=Decimal("0.004")))

    assert require_metrics(store).estimated_average_cost == Decimal("0.003")


def test_all_nullable_costs_produce_none(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, estimated_cost=None))
    store.record_attempt(make_attempt(1, estimated_cost=None))

    assert require_metrics(store).estimated_average_cost is None


def test_decimal_cost_average_preserves_precision(store: SQLiteMetricsStore) -> None:
    store.record_attempt(
        make_attempt(0, estimated_cost=Decimal("0.000000000000000001"))
    )
    store.record_attempt(
        make_attempt(1, estimated_cost=Decimal("0.000000000000000003"))
    )

    assert require_metrics(store).estimated_average_cost == Decimal("0.000000000000000002")


def test_window_start_is_oldest_included_attempt(store: SQLiteMetricsStore) -> None:
    oldest = NOW - timedelta(hours=3)
    store.record_attempt(make_attempt(0, finished_at=NOW - timedelta(hours=1)))
    store.record_attempt(make_attempt(1, finished_at=oldest))

    assert require_metrics(store).window_start == oldest


def test_window_end_is_newest_included_attempt(store: SQLiteMetricsStore) -> None:
    newest = NOW - timedelta(hours=1)
    store.record_attempt(make_attempt(0, finished_at=newest))
    store.record_attempt(make_attempt(1, finished_at=NOW - timedelta(hours=3)))

    assert require_metrics(store).window_end == newest


def test_list_metrics_includes_multiple_providers(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, provider="openai"))
    store.record_attempt(make_attempt(1, provider="gemini"))

    identities = {(item.provider, item.model) for item in store.list_provider_metrics()}

    assert identities == {("openai", "model-a"), ("gemini", "model-a")}


def test_list_metrics_includes_multiple_models(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, model="model-a"))
    store.record_attempt(make_attempt(1, model="model-b"))

    identities = {(item.provider, item.model) for item in store.list_provider_metrics()}

    assert identities == {("openai", "model-a"), ("openai", "model-b")}


def test_list_metrics_has_stable_provider_model_order(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, provider="zeta", model="b"))
    store.record_attempt(make_attempt(1, provider="alpha", model="z"))
    store.record_attempt(make_attempt(2, provider="alpha", model="a"))

    identities = [(item.provider, item.model) for item in store.list_provider_metrics()]

    assert identities == [("alpha", "a"), ("alpha", "z"), ("zeta", "b")]


def test_metrics_are_consistent_after_sqlite_reopen(
    database_path: Path,
) -> None:
    first = SQLiteMetricsStore(database_path)
    first.record_attempt(make_attempt(0, latency_ms=100, success=True))
    first.record_attempt(make_attempt(1, latency_ms=300, success=False))
    before = first.get_provider_metrics("openai", "model-a")

    reopened = SQLiteMetricsStore(database_path)
    after = reopened.get_provider_metrics("openai", "model-a")

    assert before == after


def test_success_and_failure_counts_equal_sample_count(
    store: SQLiteMetricsStore,
) -> None:
    for index, success in enumerate([True, False, False, True, True]):
        store.record_attempt(make_attempt(index, success=success))

    metrics = require_metrics(store)

    assert metrics.success_count + metrics.failure_count == metrics.sample_count


def test_failed_attempt_latency_is_included(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, success=True, latency_ms=100))
    store.record_attempt(make_attempt(1, success=False, latency_ms=500))

    metrics = require_metrics(store)

    assert metrics.average_latency_ms == 300
    assert metrics.p50_latency_ms == 100
    assert metrics.p95_latency_ms == 500
