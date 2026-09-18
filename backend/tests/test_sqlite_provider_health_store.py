import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_routing_explanations import make_decision
from test_sqlite_metrics_store import make_attempt, make_pricing

from modelpilot.health import (
    CircuitState,
    ProviderHealth,
    ProviderHealthStore,
    ProviderHealthStoreDataError,
)
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers import ProviderErrorType

NOW = datetime(2026, 9, 18, 12, 0, 0, 123456, tzinfo=UTC)


def snapshot(state: CircuitState) -> ProviderHealth:
    health = ProviderHealth.initial(provider="test-provider", model="test-model", now=NOW)
    health = health.record_success(now=NOW)
    if state is CircuitState.CLOSED:
        return health
    health = health.record_failure(
        ProviderErrorType.TIMEOUT,
        failure_threshold=1,
        cooldown=timedelta(seconds=30),
        now=NOW + timedelta(seconds=1),
    )
    if state is CircuitState.HALF_OPEN:
        return health.advance(now=NOW + timedelta(seconds=31))
    return health


def test_fresh_database_protocol_schema_and_missing_health(tmp_path: Path) -> None:
    path = tmp_path / "modelpilot.db"
    store = SQLiteMetricsStore(path)
    assert isinstance(store, ProviderHealthStore)
    assert store.get_health("missing", "missing") is None
    assert store.list_health() == []
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (3,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_health)")}
        assert columns == set(ProviderHealth.model_fields)


@pytest.mark.parametrize("state", list(CircuitState))
def test_all_states_round_trip_and_reopen(tmp_path: Path, state: CircuitState) -> None:
    path = tmp_path / "modelpilot.db"
    expected = snapshot(state)
    store = SQLiteMetricsStore(path)
    store.upsert_health(expected)
    assert store.get_health(expected.provider, expected.model) == expected
    reopened = SQLiteMetricsStore(path)
    restored = reopened.get_health(expected.provider, expected.model)
    assert restored == expected
    assert reopened.list_health() == [expected]
    for field in (
        "opened_at",
        "cooldown_until",
        "last_failure_at",
        "last_success_at",
        "updated_at",
    ):
        value = getattr(restored, field)
        assert value is None or value.tzinfo is UTC
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT state FROM provider_health").fetchone() == (state.value,)


def test_nullable_timestamps_remain_null(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    health = ProviderHealth.initial(provider="p", model="m", now=NOW)
    store.upsert_health(health)
    assert store.get_health("p", "m") == health
    with store._connect() as connection:
        row = connection.execute(
            "SELECT opened_at, cooldown_until, last_failure_at, last_success_at "
            "FROM provider_health"
        ).fetchone()
        assert tuple(row) == (None, None, None, None)


def test_offset_timestamp_is_stored_as_utc(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    health = ProviderHealth.initial(
        provider="p", model="m", now=datetime.fromisoformat("2026-09-18T20:00:00.123456+08:00")
    )
    store.upsert_health(health)
    assert store.get_health("p", "m").updated_at == NOW
    with store._connect() as connection:
        assert connection.execute("SELECT updated_at FROM provider_health").fetchone()[0] == (
            NOW.isoformat(timespec="microseconds")
        )


def test_upsert_replaces_snapshot_and_clears_nullable_fields(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    health = snapshot(CircuitState.OPEN)
    store.upsert_health(health)
    closed = health.record_success(now=NOW + timedelta(seconds=32))
    store.upsert_health(closed)
    assert store.list_health() == [closed]
    assert store.get_health(closed.provider, closed.model).opened_at is None


def test_provider_model_isolation_stable_order_and_parameterization(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    keys = [("z", "b"), ("a", "b"), ("a", "a"), ("x' OR 1=1 --", "b")]
    for provider, model in keys:
        store.upsert_health(ProviderHealth.initial(provider=provider, model=model, now=NOW))
    assert [(h.provider, h.model) for h in store.list_health()] == sorted(keys)
    for provider, model in keys:
        assert store.get_health(provider, model).provider == provider
        assert store.get_health(provider, model).model == model
    assert store.get_health("a' OR 1=1 --", "a") is None


@pytest.mark.parametrize("version", [1, 2])
def test_migration_preserves_existing_data_and_is_repeatable(tmp_path: Path, version: int) -> None:
    path = tmp_path / "legacy.db"
    # Construct the actual legacy tables, without initializing the v3 schema.
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, version INT)"
        )
        connection.execute("INSERT INTO schema_version VALUES (1, ?)", (version,))
        SQLiteMetricsStore._create_version_one_schema(connection)
        if version == 2:
            SQLiteMetricsStore._migrate_version_one_to_two(connection)
        connection.commit()
    # Use existing write methods without running initialization on the legacy fixture.
    legacy = object.__new__(SQLiteMetricsStore)
    legacy._database = str(path)
    legacy._timeout_seconds = 5.0
    attempt, pricing, decision = make_attempt(), make_pricing(), make_decision()
    legacy.record_attempt(attempt)
    legacy.upsert_pricing(pricing)
    if version == 2:
        legacy.record_routing_decision(decision)
    for _ in range(2):
        migrated = SQLiteMetricsStore(path)
        assert migrated.get_recent_attempts(
            attempt.provider, attempt.model, 10, attempt.started_at
        ) == [attempt]
        assert migrated.get_pricing(pricing.provider, pricing.model) == pricing
        assert migrated.get_routing_decision(decision.request_id) == (
            decision if version == 2 else None
        )
        with migrated._connect() as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        migrated.upsert_health(snapshot(CircuitState.OPEN))
    assert migrated.list_health() == [snapshot(CircuitState.OPEN)]


@pytest.mark.parametrize(
    ("state", "failures", "timestamp"),
    [
        ("UNKNOWN", 0, NOW.isoformat()),
        ("CLOSED", -1, NOW.isoformat()),
        ("CLOSED", 0, "invalid"),
        ("CLOSED", 0, "2026-09-18T12:00:00"),
    ],
)
@pytest.mark.parametrize("read", ["get", "list"])
def test_malformed_rows_fail_explicitly(
    tmp_path: Path, state: str, failures: int, timestamp: str, read: str
) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    with store._connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO provider_health "
            "(provider, model, state, consecutive_failures, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p", "m", state, failures, timestamp),
        )
        connection.commit()
    with pytest.raises(ProviderHealthStoreDataError, match="malformed provider health row"):
        store.get_health("p", "m") if read == "get" else store.list_health()


def test_database_errors_propagate(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "modelpilot.db")
    with store._connect() as connection:
        connection.execute("DROP TABLE provider_health")
        connection.commit()
    for operation in (
        lambda: store.get_health("p", "m"),
        store.list_health,
        lambda: store.upsert_health(snapshot(CircuitState.CLOSED)),
    ):
        with pytest.raises(sqlite3.OperationalError):
            operation()


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, version INT)"
        )
        connection.execute("INSERT INTO schema_version VALUES (1, 2)")
        SQLiteMetricsStore._create_version_one_schema(connection)
        SQLiteMetricsStore._migrate_version_one_to_two(connection)
        connection.commit()
    migrate = SQLiteMetricsStore._migrate_version_two_to_three

    def interrupted(connection: sqlite3.Connection) -> None:
        migrate(connection)
        raise sqlite3.OperationalError("test migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteMetricsStore, "_migrate_version_two_to_three", staticmethod(interrupted)
        )
        with pytest.raises(sqlite3.OperationalError, match="test migration interruption"):
            SQLiteMetricsStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (2,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'provider_health'"
        ).fetchone() is None
    assert SQLiteMetricsStore(path).list_health() == []
