"""Shared SQLite connection and versioned schema infrastructure for domain stores."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 4


class SchemaVersionError(RuntimeError):
    """Raised when a database uses an unsupported schema version."""


class SQLiteDatabase:
    def __init__(self, database: str | Path, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._database = str(database)
        self._timeout_seconds = timeout_seconds
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=self._timeout_seconds)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_version (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL CHECK (version >= 1)
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM schema_version WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    self._create_version_one_schema(connection)
                    self._migrate_version_one_to_two(connection)
                    self._migrate_version_two_to_three(connection)
                    self._migrate_version_three_to_four(connection)
                    connection.execute(
                        "INSERT INTO schema_version (singleton, version) VALUES (?, ?)",
                        (1, SCHEMA_VERSION),
                    )
                elif row["version"] > SCHEMA_VERSION or row["version"] < 1:
                    raise SchemaVersionError(
                        f"unsupported metrics schema version {row['version']}; "
                        f"expected {SCHEMA_VERSION}"
                    )
                elif row["version"] < SCHEMA_VERSION:
                    if row["version"] == 1:
                        self._migrate_version_one_to_two(connection)
                    if row["version"] <= 2:
                        self._migrate_version_two_to_three(connection)
                    self._migrate_version_three_to_four(connection)
                    connection.execute(
                        "UPDATE schema_version SET version = ? WHERE singleton = 1",
                        (SCHEMA_VERSION,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _migrate_version_three_to_four(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE benchmark_runs (
                run_id TEXT PRIMARY KEY NOT NULL CHECK (length(run_id) > 0),
                suite_id TEXT NOT NULL CHECK (length(suite_id) > 0),
                suite_version TEXT NOT NULL CHECK (length(suite_version) > 0),
                suite_fingerprint TEXT NOT NULL CHECK (length(suite_fingerprint) = 64),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('completed', 'completed_with_failures')),
                total_cases INTEGER NOT NULL CHECK (total_cases > 0),
                completed_cases INTEGER NOT NULL CHECK (completed_cases >= 0),
                execution_failed_cases INTEGER NOT NULL CHECK (execution_failed_cases >= 0),
                terminated_early INTEGER NOT NULL CHECK (terminated_early IN (0, 1)),
                max_cases INTEGER NOT NULL CHECK (max_cases > 0),
                case_timeout_seconds REAL NOT NULL CHECK (case_timeout_seconds > 0),
                temperature REAL NOT NULL CHECK (temperature BETWEEN 0 AND 2),
                max_tokens INTEGER NOT NULL CHECK (max_tokens > 0)
            )
            """
        )
        connection.execute(
            "CREATE INDEX benchmark_runs_started ON benchmark_runs (started_at DESC, run_id DESC)"
        )
        connection.execute(
            """
            CREATE TABLE benchmark_case_results (
                run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id),
                case_id TEXT NOT NULL CHECK (length(case_id) > 0),
                case_index INTEGER NOT NULL CHECK (case_index >= 0),
                category TEXT NOT NULL,
                execution_status TEXT NOT NULL
                    CHECK (execution_status IN ('completed', 'provider_failed')),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
                input_tokens INTEGER CHECK (input_tokens >= 0),
                output_tokens INTEGER CHECK (output_tokens >= 0),
                total_tokens INTEGER CHECK (total_tokens >= 0),
                error_type TEXT,
                evaluation_score REAL CHECK (evaluation_score BETWEEN 0 AND 1),
                evaluation_passed INTEGER CHECK (evaluation_passed IN (0, 1)),
                evaluator_kind TEXT,
                evaluator_version TEXT,
                evaluation_reason TEXT,
                PRIMARY KEY (run_id, case_index),
                UNIQUE (run_id, case_id),
                CHECK (
                    (execution_status = 'completed' AND error_type IS NULL
                        AND evaluation_score IS NOT NULL AND evaluation_passed IS NOT NULL
                        AND evaluator_kind IS NOT NULL AND evaluator_version IS NOT NULL
                        AND evaluation_reason IS NOT NULL)
                    OR (execution_status = 'provider_failed' AND error_type IS NOT NULL
                        AND evaluation_score IS NULL AND evaluation_passed IS NULL
                        AND evaluator_kind IS NULL AND evaluator_version IS NULL
                        AND evaluation_reason IS NULL)
                )
            )
            """
        )

    @staticmethod
    def _create_version_one_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL CHECK (attempt_index >= 0),
                provider TEXT NOT NULL CHECK (length(provider) > 0),
                model TEXT NOT NULL CHECK (length(model) > 0),
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
                success INTEGER NOT NULL CHECK (success IN (0, 1)),
                error_type TEXT,
                input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
                output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
                total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
                estimated_cost TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (request_id, attempt_index)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS attempts_provider_model_finished
            ON attempts (provider, model, finished_at DESC, id DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pricing (
                provider TEXT NOT NULL CHECK (length(provider) > 0),
                model TEXT NOT NULL CHECK (length(model) > 0),
                input_cost_per_million_tokens TEXT NOT NULL,
                output_cost_per_million_tokens TEXT NOT NULL,
                currency TEXT NOT NULL CHECK (length(currency) > 0),
                source TEXT NOT NULL CHECK (length(source) > 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, model)
            )
            """
        )

    @staticmethod
    def _migrate_version_one_to_two(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS routing_decisions (
                request_id TEXT PRIMARY KEY CHECK (length(request_id) > 0),
                routing_version TEXT NOT NULL CHECK (length(routing_version) > 0),
                selected_provider TEXT NOT NULL CHECK (length(selected_provider) > 0),
                selected_model TEXT NOT NULL CHECK (length(selected_model) > 0),
                served_provider TEXT,
                served_model TEXT,
                created_at TEXT NOT NULL,
                explanation_json TEXT NOT NULL,
                CHECK ((served_provider IS NULL) = (served_model IS NULL))
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS routing_decisions_created
            ON routing_decisions (created_at DESC, request_id DESC)
            """
        )

    @staticmethod
    def _migrate_version_two_to_three(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_health (
                provider TEXT NOT NULL CHECK (length(provider) > 0),
                model TEXT NOT NULL CHECK (length(model) > 0),
                state TEXT NOT NULL CHECK (state IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
                consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
                opened_at TEXT,
                cooldown_until TEXT,
                last_failure_at TEXT,
                last_success_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, model)
            )
            """
        )


def serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
