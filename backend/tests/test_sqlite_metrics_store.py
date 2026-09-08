import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modelpilot.metrics import (
    SCHEMA_VERSION,
    AttemptRecord,
    MetricsStore,
    MetricsStoreDataError,
    ModelPricing,
    SchemaVersionError,
    SQLiteMetricsStore,
)

NOW = datetime.now(UTC).replace(microsecond=123456)


def make_attempt(index: int = 0, **overrides: object) -> AttemptRecord:
    finished_at = NOW - timedelta(seconds=index)
    values: dict[str, object] = {
        "request_id": f"req_{index}",
        "attempt_index": 0,
        "provider": "test-provider",
        "model": "test-model",
        "started_at": finished_at - timedelta(milliseconds=100 + index),
        "finished_at": finished_at,
        "latency_ms": float(100 + index),
        "success": True,
        "error_type": None,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "estimated_cost": Decimal("0.001"),
        "created_at": finished_at,
    }
    values.update(overrides)
    return AttemptRecord.model_validate(values)


def make_pricing(**overrides: object) -> ModelPricing:
    values: dict[str, object] = {
        "provider": "test-provider",
        "model": "test-model",
        "input_cost_per_million_tokens": Decimal("1.25"),
        "output_cost_per_million_tokens": Decimal("2.50"),
        "currency": "USD",
        "source": "test fixture",
        "updated_at": NOW,
    }
    values.update(overrides)
    return ModelPricing.model_validate(values)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "metrics.sqlite3"


@pytest.fixture
def store(database_path: Path) -> SQLiteMetricsStore:
    return SQLiteMetricsStore(database_path)


def test_new_database_initializes_schema_and_implements_protocol(
    database_path: Path,
) -> None:
    store = SQLiteMetricsStore(database_path)

    assert database_path.exists()
    assert isinstance(store, MetricsStore)
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"schema_version", "attempts", "pricing", "routing_decisions"} <= tables


def test_database_uses_wal_mode(database_path: Path) -> None:
    SQLiteMetricsStore(database_path)

    with sqlite3.connect(database_path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_schema_version_is_current(database_path: Path) -> None:
    SQLiteMetricsStore(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()[0]

    assert version == SCHEMA_VERSION == 2


def test_reopens_initialized_database_without_destroying_data(database_path: Path) -> None:
    first = SQLiteMetricsStore(database_path)
    attempt = make_attempt()
    first.record_attempt(attempt)

    reopened = SQLiteMetricsStore(database_path)

    assert reopened.get_recent_attempts(
        attempt.provider, attempt.model, 10, NOW - timedelta(days=1)
    ) == [attempt]


def test_records_successful_attempt(store: SQLiteMetricsStore) -> None:
    attempt = make_attempt()

    store.record_attempt(attempt)

    assert store.get_recent_attempts(
        attempt.provider, attempt.model, 10, NOW - timedelta(days=1)
    )[0].success is True


def test_records_failed_attempt_and_error(store: SQLiteMetricsStore) -> None:
    attempt = make_attempt(success=False, error_type="timeout")

    store.record_attempt(attempt)
    restored = store.get_recent_attempts(
        attempt.provider, attempt.model, 10, NOW - timedelta(days=1)
    )[0]

    assert restored.success is False
    assert restored.error_type == "timeout"


def test_preserves_nullable_usage_and_cost(store: SQLiteMetricsStore) -> None:
    attempt = make_attempt(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost=None,
    )

    store.record_attempt(attempt)
    restored = store.get_recent_attempts(
        attempt.provider, attempt.model, 10, NOW - timedelta(days=1)
    )[0]

    assert restored.input_tokens is None
    assert restored.output_tokens is None
    assert restored.total_tokens is None
    assert restored.estimated_cost is None


def test_datetime_round_trip_normalizes_to_utc(store: SQLiteMetricsStore) -> None:
    offset = datetime.fromisoformat("2026-09-07T20:00:00.654321+08:00")
    attempt = make_attempt(
        started_at=offset - timedelta(milliseconds=100),
        finished_at=offset,
        created_at=offset,
    )

    store.record_attempt(attempt)
    restored = store.get_recent_attempts(
        attempt.provider, attempt.model, 10, datetime(2026, 9, 7, tzinfo=UTC)
    )[0]

    assert restored.finished_at == offset
    assert restored.finished_at.utcoffset() == timedelta(0)
    assert restored.finished_at.microsecond == 654321


def test_recent_attempts_are_newest_first(store: SQLiteMetricsStore) -> None:
    attempts = [make_attempt(index) for index in range(3)]
    for attempt in attempts:
        store.record_attempt(attempt)

    restored = store.get_recent_attempts(
        "test-provider", "test-model", 10, NOW - timedelta(days=1)
    )

    assert [attempt.request_id for attempt in restored] == ["req_0", "req_1", "req_2"]


def test_recent_attempts_applies_limit(store: SQLiteMetricsStore) -> None:
    for index in range(3):
        store.record_attempt(make_attempt(index))

    restored = store.get_recent_attempts(
        "test-provider", "test-model", 2, NOW - timedelta(days=1)
    )

    assert len(restored) == 2


def test_recent_attempts_applies_since(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0))
    store.record_attempt(
        make_attempt(
            1,
            started_at=NOW - timedelta(days=2, milliseconds=100),
            finished_at=NOW - timedelta(days=2),
        )
    )

    restored = store.get_recent_attempts(
        "test-provider", "test-model", 10, NOW - timedelta(days=1)
    )

    assert [attempt.request_id for attempt in restored] == ["req_0"]


def test_recent_attempts_rejects_invalid_inputs(store: SQLiteMetricsStore) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.get_recent_attempts("test-provider", "test-model", 0, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.get_recent_attempts(
            "test-provider", "test-model", 1, datetime(2026, 9, 7)
        )


def test_pricing_insert_read_and_list(store: SQLiteMetricsStore) -> None:
    first = make_pricing(model="alpha")
    second = make_pricing(model="beta")

    store.upsert_pricing(second)
    store.upsert_pricing(first)

    assert store.get_pricing("test-provider", "alpha") == first
    assert store.list_pricing() == [first, second]


def test_pricing_update_replaces_current_value(store: SQLiteMetricsStore) -> None:
    original = make_pricing()
    updated = make_pricing(
        input_cost_per_million_tokens=Decimal("3.75"),
        source="updated fixture",
        updated_at=NOW + timedelta(minutes=1),
    )

    store.upsert_pricing(original)
    store.upsert_pricing(updated)

    assert store.get_pricing("test-provider", "test-model") == updated
    assert len(store.list_pricing()) == 1


def test_pricing_persists_across_reopen(database_path: Path) -> None:
    pricing = make_pricing()
    SQLiteMetricsStore(database_path).upsert_pricing(pricing)

    reopened = SQLiteMetricsStore(database_path)

    assert reopened.get_pricing(pricing.provider, pricing.model) == pricing


def test_provider_metrics_counts_success_and_failure(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, success=True))
    store.record_attempt(make_attempt(1, success=False))
    store.record_attempt(make_attempt(2, success=True))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.sample_count == 3
    assert metrics.success_count == 2
    assert metrics.failure_count == 1
    assert metrics.success_rate == pytest.approx(2 / 3)


def test_provider_metrics_include_failed_latency(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0, latency_ms=100, success=True))
    store.record_attempt(make_attempt(1, latency_ms=300, success=False))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.average_latency_ms == 200


def test_provider_metrics_use_nearest_rank_percentiles(store: SQLiteMetricsStore) -> None:
    for index, latency in enumerate(range(10, 110, 10)):
        store.record_attempt(make_attempt(index, latency_ms=latency))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.p50_latency_ms == 50
    assert metrics.p95_latency_ms == 100


def test_provider_metrics_exclude_missing_cost_from_average(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(make_attempt(0, estimated_cost=Decimal("0.002")))
    store.record_attempt(make_attempt(1, estimated_cost=None))
    store.record_attempt(make_attempt(2, estimated_cost=Decimal("0.004")))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.estimated_average_cost == Decimal("0.003")


def test_provider_metrics_return_none_when_all_costs_are_missing(
    store: SQLiteMetricsStore,
) -> None:
    store.record_attempt(make_attempt(estimated_cost=None))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.estimated_average_cost is None


def test_provider_metrics_apply_100_attempt_cap(store: SQLiteMetricsStore) -> None:
    for index in range(101):
        store.record_attempt(make_attempt(index, success=index != 100))

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.sample_count == 100
    assert metrics.failure_count == 0


def test_provider_metrics_apply_seven_day_cap(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(0))
    store.record_attempt(
        make_attempt(
            1,
            started_at=NOW - timedelta(days=8, milliseconds=100),
            finished_at=NOW - timedelta(days=8),
            success=False,
        )
    )

    metrics = store.get_provider_metrics("test-provider", "test-model")

    assert metrics is not None
    assert metrics.sample_count == 1
    assert metrics.success_rate == 1


def test_empty_provider_metrics_are_none(store: SQLiteMetricsStore) -> None:
    assert store.get_provider_metrics("missing", "model") is None
    assert store.list_provider_metrics() == []


def test_list_provider_metrics_is_stably_sorted(store: SQLiteMetricsStore) -> None:
    store.record_attempt(make_attempt(provider="zeta", model="b"))
    store.record_attempt(make_attempt(1, provider="alpha", model="a"))

    metrics = store.list_provider_metrics()

    assert [(item.provider, item.model) for item in metrics] == [
        ("alpha", "a"),
        ("zeta", "b"),
    ]


def test_incompatible_schema_version_fails_clearly(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version (singleton, version) VALUES (?, ?)", (1, 3)
        )

    with pytest.raises(SchemaVersionError, match="unsupported metrics schema version 3"):
        SQLiteMetricsStore(database_path)


def test_uncreatable_database_path_surfaces_operational_error(tmp_path: Path) -> None:
    database_path = tmp_path / "missing" / "metrics.sqlite3"

    with pytest.raises(sqlite3.OperationalError):
        SQLiteMetricsStore(database_path)


def test_malformed_attempt_row_raises_data_error(
    store: SQLiteMetricsStore, database_path: Path
) -> None:
    store.record_attempt(make_attempt())
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE attempts SET finished_at = ?", ("not-a-datetime",))

    with pytest.raises(MetricsStoreDataError, match="malformed attempt row"):
        store.get_recent_attempts(
            "test-provider", "test-model", 10, NOW - timedelta(days=1)
        )
