import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_benchmark_runner import NOW, SMOKE, TARGET, FakeProvider, outcome, run, suite
from test_health_aware_routing import opened
from test_metrics_api import make_client
from test_routing_explanations import make_decision
from test_sqlite_metrics_store import make_attempt, make_pricing

from modelpilot.benchmarks import (
    BenchmarkRunner,
    BenchmarkStore,
    BenchmarkStoreDataError,
    DuplicateBenchmarkRunError,
    SQLiteBenchmarkStore,
    load_benchmark_suite,
)
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers.base import ProviderErrorType
from modelpilot.sqlite_database import SCHEMA_VERSION, SQLiteDatabase


@pytest.fixture
def store(tmp_path):
    return SQLiteBenchmarkStore(tmp_path / "modelpilot.db")


def example(*outcomes, run_id="run-a", **updates):
    values = outcomes or (outcome(),)
    result = run(suite(len(values)), FakeProvider(values))
    return result.model_copy(update={"run_id": run_id, **updates})


def table_rows(connection, table):
    return tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))


def test_fresh_schema_protocol_and_connection_settings(store):
    assert isinstance(store, BenchmarkStore)
    assert not isinstance(SQLiteMetricsStore(store._database), BenchmarkStore)
    with store._connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert SCHEMA_VERSION == 4
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'benchmark_%'"
            )
        } == {"benchmark_runs", "benchmark_case_results"}
    assert store.get_run("missing") is None
    assert store.list_runs() == [] and store.list_case_results("missing") == ()


def legacy_database(path, version):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, version INT)"
        )
        connection.execute("INSERT INTO schema_version VALUES (1, ?)", (version,))
        SQLiteDatabase._create_version_one_schema(connection)
        if version >= 2:
            SQLiteDatabase._migrate_version_one_to_two(connection)
        if version >= 3:
            SQLiteDatabase._migrate_version_two_to_three(connection)
    legacy = object.__new__(SQLiteMetricsStore)
    legacy._database, legacy._timeout_seconds = str(path), 5.0
    attempt, pricing, decision, health = make_attempt(), make_pricing(), make_decision(), opened()
    legacy.record_attempt(attempt)
    legacy.upsert_pricing(pricing)
    if version >= 2:
        legacy.record_routing_decision(decision)
    if version >= 3:
        legacy.upsert_health(health)
    return legacy


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("initializer", [SQLiteMetricsStore, SQLiteBenchmarkStore])
def test_migration_chain_preserves_all_existing_rows(tmp_path, version, initializer):
    path = tmp_path / "legacy.db"
    legacy = legacy_database(path, version)
    tables = ["attempts", "pricing"]
    if version >= 2:
        tables.append("routing_decisions")
    if version >= 3:
        tables.append("provider_health")
    with legacy._connect() as connection:
        before = {name: table_rows(connection, name) for name in tables}
    for _ in range(2):
        migrated = initializer(path)
        with migrated._connect() as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
            assert {name: table_rows(connection, name) for name in tables} == before
    benchmark = SQLiteBenchmarkStore(path)
    result = example()
    benchmark.save_run(result)
    assert benchmark.get_run(result.run_id) == result
    production = SQLiteMetricsStore(path)
    assert len(production.list_pricing()) == 1
    assert len(production.list_provider_metrics()) == 1


@pytest.mark.parametrize("version", [1, 2, 3])
def test_failed_migration_rolls_back_tables_and_version(tmp_path, monkeypatch, version):
    path = tmp_path / "legacy.db"
    legacy = legacy_database(path, version)
    with legacy._connect() as connection:
        before = tuple(connection.iterdump())
    migrate = SQLiteDatabase._migrate_version_three_to_four

    def fail(connection):
        migrate(connection)
        raise sqlite3.OperationalError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteDatabase, "_migrate_version_three_to_four", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError):
            SQLiteBenchmarkStore(path)
    with legacy._connect() as connection:
        assert tuple(connection.iterdump()) == before
    assert SQLiteBenchmarkStore(path).list_runs() == []


@pytest.mark.parametrize("answer", ["PINE", "wrong"])
def test_completed_result_round_trip_all_fields(store, answer):
    result = example(outcome(answer))
    store.save_run(result)
    restored = SQLiteBenchmarkStore(store._database).get_run(result.run_id)
    assert restored == result
    assert restored.case_results[0].evaluation.score == (1 if answer == "PINE" else 0)
    assert restored.started_at.tzinfo is UTC
    assert store.list_case_results(result.run_id) == result.case_results
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM benchmark_case_results").fetchone()
        assert row["evaluation_passed"] == (1 if answer == "PINE" else 0)
        assert row["execution_status"] == "completed"
        assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (10, 2, 12)
        assert row["latency_ms"] == 12.5


@pytest.mark.parametrize("error", list(ProviderErrorType))
def test_all_failure_types_have_null_evaluation_and_usage(store, error):
    result = example(outcome(error=error, input_tokens=None, output_tokens=None, total_tokens=None))
    store.save_run(result)
    assert SQLiteBenchmarkStore(store._database).get_run(result.run_id) == result
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM benchmark_case_results").fetchone()
        for field in (
            "evaluation_score",
            "evaluation_passed",
            "evaluator_kind",
            "evaluator_version",
            "evaluation_reason",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            assert row[field] is None
        assert row["error_type"] == error.value


def test_auth_fail_fast_partial_run_round_trip(store):
    result = run(suite(3), FakeProvider([outcome(), outcome(error="authentication_error")]))
    store.save_run(result)
    restored = SQLiteBenchmarkStore(store._database).get_run(result.run_id)
    assert restored == result
    assert restored.terminated_early and len(restored.case_results) == 2
    assert restored.case_results[-1].error_type == "authentication_error"


def test_smoke_suite_run_save_reopen_semantic_equality(store):
    definition = load_benchmark_suite(SMOKE)
    result = asyncio.run(
        BenchmarkRunner().run(
            definition,
            TARGET,
            FakeProvider(
                [
                    outcome("PINE"),
                    outcome("42"),
                    outcome('{"ready":true}'),
                ]
            ),
        )
    )
    store.save_run(result)
    assert SQLiteBenchmarkStore(store._database).get_run(result.run_id) == result


def test_case_order_is_index_not_alphabetical(store):
    result = example(outcome(), outcome(), outcome())
    cases = tuple(
        c.model_copy(update={"case_id": name})
        for c, name in zip(
            result.case_results,
            ["z", "a", "m"],
            strict=True,
        )
    )
    result = result.model_copy(update={"case_results": cases})
    store.save_run(result)
    for _ in range(2):
        assert [c.case_id for c in store.list_case_results(result.run_id)] == ["z", "a", "m"]


def test_latest_runs_order_with_ties_and_sql_limit(store, monkeypatch):
    prototype = example()
    for index in range(120):
        store.save_run(prototype.model_copy(update={"run_id": f"run-{index:03}"}))
    newer = prototype.model_copy(
        update={
            "run_id": "newest",
            "started_at": NOW + timedelta(seconds=1),
            "finished_at": NOW + timedelta(seconds=2),
        }
    )
    store.save_run(newer)
    import modelpilot.benchmarks.sqlite_store as implementation

    original = implementation._restore_run
    restored = []

    def observe(connection, row):
        restored.append(row["run_id"])
        return original(connection, row)

    monkeypatch.setattr(implementation, "_restore_run", observe)
    assert [r.run_id for r in store.list_runs(3)] == ["newest", "run-119", "run-118"]
    assert restored == ["newest", "run-119", "run-118"]
    assert len(store.list_runs()) == 20
    assert len(store.list_runs(100)) == 100
    with store._connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM benchmark_runs "
            "ORDER BY started_at DESC, run_id DESC LIMIT 3"
        ).fetchall()
        assert any("benchmark_runs_started" in row[3] for row in plan)


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "20", None])
def test_invalid_limit(store, limit):
    with pytest.raises(ValueError, match="limit"):
        store.list_runs(limit)


def test_duplicate_never_overwrites(store):
    original = example()
    store.save_run(original)
    for duplicate in [original, example(outcome("wrong"))]:
        with pytest.raises(DuplicateBenchmarkRunError):
            store.save_run(duplicate)
        assert store.get_run(original.run_id) == original
        assert store.list_runs() == [original]


def test_transaction_failure_rolls_back_parent_and_previous_cases(store):
    original = example()
    store.save_run(original)
    with store._connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_second_case BEFORE INSERT ON benchmark_case_results "
            "WHEN NEW.case_index = 1 BEGIN SELECT RAISE(ABORT, 'injected case failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected case failure"):
        store.save_run(example(outcome(), outcome(), run_id="rollback"))
    assert store.get_run("rollback") is None
    assert store.list_case_results("rollback") == ()
    assert store.list_runs() == [original]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM benchmark_case_results").fetchone()[0] == 1


def test_unchecked_invalid_model_rejected_before_save(store):
    result = example()
    invalid = result.model_copy(
        update={"case_results": (result.case_results[0].model_copy(update={"latency_ms": -1}),)}
    )
    with pytest.raises(ValidationError):
        store.save_run(invalid)
    assert store.list_runs() == []


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("benchmark_runs", "status", "unknown"),
        ("benchmark_runs", "started_at", "not-a-date"),
        ("benchmark_runs", "finished_at", "2026-01-01T12:00:00"),
        ("benchmark_runs", "suite_id", ""),
        ("benchmark_runs", "suite_fingerprint", "bad"),
        ("benchmark_runs", "terminated_early", 2),
        ("benchmark_runs", "completed_cases", 9),
        ("benchmark_runs", "max_cases", 0),
        ("benchmark_case_results", "execution_status", "unknown"),
        ("benchmark_case_results", "evaluation_score", -0.1),
        ("benchmark_case_results", "evaluation_score", 1.1),
        ("benchmark_case_results", "evaluation_score", None),
        ("benchmark_case_results", "evaluation_passed", 2),
        ("benchmark_case_results", "latency_ms", -1),
        ("benchmark_case_results", "input_tokens", -1),
        ("benchmark_case_results", "output_tokens", -1),
        ("benchmark_case_results", "total_tokens", -1),
        ("benchmark_case_results", "evaluator_kind", "unknown"),
        ("benchmark_case_results", "evaluator_version", "999"),
        ("benchmark_case_results", "evaluation_reason", "unknown"),
        ("benchmark_case_results", "case_index", 2),
        ("benchmark_case_results", "provider", "wrong-target"),
    ],
)
@pytest.mark.parametrize("read", ["get", "list", "cases"])
def test_corrupt_rows_fail_explicitly(store, table, column, value, read):
    store.save_run(example())
    with store._connect() as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
    with pytest.raises(BenchmarkStoreDataError, match="malformed benchmark run"):
        if read == "get":
            store.get_run("run-a")
        elif read == "list":
            store.list_runs()
        else:
            store.list_case_results("run-a")


def test_foreign_keys_and_orphan_detection(store):
    store.save_run(example())
    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM benchmark_runs")
    with store._connect() as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM benchmark_runs")
    with pytest.raises(BenchmarkStoreDataError, match="orphan"):
        store.get_run("run-a")
    with pytest.raises(BenchmarkStoreDataError, match="orphan"):
        store.list_case_results("run-a")
    with pytest.raises(BenchmarkStoreDataError, match="orphan"):
        store.list_runs()


def test_utc_microsecond_round_trip(store):
    offset = datetime.fromisoformat("2026-09-18T08:00:00.123456+08:00")
    result = example(started_at=offset, finished_at=offset + timedelta(seconds=1))
    store.save_run(result)
    restored = store.get_run(result.run_id)
    assert restored.started_at == offset and restored.started_at.tzinfo is UTC
    assert restored.started_at.microsecond == 123456
    with store._connect() as connection:
        assert connection.execute("SELECT started_at FROM benchmark_runs").fetchone()[0] == (
            "2026-09-18T00:00:00.123456+00:00"
        )


def test_missing_case_detected_as_corruption(store):
    store.save_run(example())
    with store._connect() as connection, connection:
        connection.execute("DELETE FROM benchmark_case_results")
    with pytest.raises(BenchmarkStoreDataError):
        store.get_run("run-a")


def test_save_database_failure_is_visible(store):
    with store._connect() as connection, connection:
        connection.execute("DROP TABLE benchmark_case_results")
    with pytest.raises(sqlite3.OperationalError):
        store.save_run(example())
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0] == 0


def test_production_metrics_api_health_and_rows_unchanged(store, monkeypatch):
    production = SQLiteMetricsStore(store._database)
    production.record_attempt(make_attempt(finished_at=NOW, started_at=NOW, created_at=NOW))
    production.upsert_pricing(make_pricing())
    production.record_routing_decision(make_decision())
    production.upsert_health(opened())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("modelpilot.metrics.api.datetime", FixedDatetime)
    monkeypatch.setattr("modelpilot.metrics.sqlite_store.datetime", FixedDatetime)
    tables = ("attempts", "pricing", "provider_health", "routing_decisions")
    with production._connect() as connection:
        before = {t: table_rows(connection, t) for t in tables}
    metrics = production.list_provider_metrics()
    with make_client(production) as client:
        summary = client.get("/v1/metrics/summary")
        assert summary.status_code == 200
        store.save_run(example(outcome(), outcome("wrong"), outcome(error="timeout")))
        assert client.get("/v1/metrics/summary").json() == summary.json()
    reopened = SQLiteMetricsStore(store._database)
    assert reopened.list_provider_metrics() == metrics
    with reopened._connect() as connection:
        assert {t: table_rows(connection, t) for t in tables} == before


def test_database_contains_no_prompt_output_or_credentials(store):
    result = example(
        outcome("UNIQUE_RAW_COMPLETION"),
        outcome(
            error="provider_error",
            error_message="Authorization: Bearer UNIQUE_PRIVATE_SECRET",
        ),
    )
    store.save_run(result)
    with store._connect() as connection:
        dump = "\n".join(connection.iterdump())
    for forbidden in (
        "UNIQUE_RAW_COMPLETION",
        "UNIQUE_PRIVATE_SECRET",
        "Authorization",
        "Prompt 0",
        "Do not change me",
        "expected_text",
        "actual_output",
        "fake-credential",
    ):
        assert forbidden not in dump
    assert result.suite_fingerprint in dump


def test_parameterized_identity_queries_and_unchanged_fingerprint(store):
    result = example().model_copy(update={"suite_fingerprint": "a" * 64})
    store.save_run(result)
    assert store.get_run("run-a").suite_fingerprint == "a" * 64
    assert store.get_run("' OR 1=1 --") is None
    assert store.list_case_results("' OR 1=1 --") == ()
