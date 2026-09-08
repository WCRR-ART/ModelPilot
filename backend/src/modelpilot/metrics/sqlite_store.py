from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from modelpilot.metrics.models import (
    AttemptRecord,
    ModelPricing,
    ProviderMetricsSnapshot,
    RoutingDecision,
    RoutingExplanation,
)

SCHEMA_VERSION = 2
METRICS_ATTEMPT_LIMIT = 100
METRICS_MAX_AGE = timedelta(days=7)


class SchemaVersionError(RuntimeError):
    """Raised when a database uses a schema this store cannot read."""


class MetricsStoreDataError(RuntimeError):
    """Raised when persisted data cannot be restored to a domain model."""


class DuplicateRoutingDecisionError(RuntimeError):
    """Raised when a routing decision already exists for a request ID."""


class SQLiteMetricsStore:
    """A small, synchronous SQLite implementation of the metrics store contract."""

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
                    connection.execute(
                        "INSERT INTO schema_version (singleton, version) VALUES (?, ?)",
                        (1, SCHEMA_VERSION),
                    )
                elif row["version"] > SCHEMA_VERSION or row["version"] < 1:
                    raise SchemaVersionError(
                        f"unsupported metrics schema version {row['version']}; "
                        f"expected {SCHEMA_VERSION}"
                    )
                elif row["version"] == 1:
                    self._migrate_version_one_to_two(connection)
                    connection.execute(
                        "UPDATE schema_version SET version = ? WHERE singleton = 1",
                        (SCHEMA_VERSION,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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

    def record_attempt(self, attempt: AttemptRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO attempts (
                    request_id, attempt_index, provider, model, started_at, finished_at,
                    latency_ms, success, error_type, input_tokens, output_tokens,
                    total_tokens, estimated_cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.request_id,
                    attempt.attempt_index,
                    attempt.provider,
                    attempt.model,
                    _serialize_datetime(attempt.started_at),
                    _serialize_datetime(attempt.finished_at),
                    attempt.latency_ms,
                    int(attempt.success),
                    attempt.error_type,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.total_tokens,
                    _serialize_decimal(attempt.estimated_cost),
                    _serialize_datetime(attempt.created_at),
                ),
            )
            connection.commit()

    def get_recent_attempts(
        self,
        provider: str,
        model: str,
        limit: int,
        since: datetime,
    ) -> list[AttemptRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        serialized_since = _serialize_datetime(since)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, attempt_index, provider, model, started_at, finished_at,
                       latency_ms, success, error_type, input_tokens, output_tokens,
                       total_tokens, estimated_cost, created_at
                FROM attempts
                WHERE provider = ? AND model = ? AND finished_at >= ?
                ORDER BY finished_at DESC, id DESC
                LIMIT ?
                """,
                (provider, model, serialized_since, limit),
            ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def get_provider_metrics(
        self,
        provider: str,
        model: str,
    ) -> ProviderMetricsSnapshot | None:
        return self._build_metrics(provider, model, datetime.now(UTC))

    def list_provider_metrics(self) -> list[ProviderMetricsSnapshot]:
        as_of = datetime.now(UTC)
        since = _serialize_datetime(as_of - METRICS_MAX_AGE)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT provider, model
                FROM attempts
                WHERE finished_at >= ?
                ORDER BY provider, model
                """,
                (since,),
            ).fetchall()

        snapshots = [
            self._build_metrics(row["provider"], row["model"], as_of) for row in rows
        ]
        return [snapshot for snapshot in snapshots if snapshot is not None]

    def _build_metrics(
        self,
        provider: str,
        model: str,
        as_of: datetime,
    ) -> ProviderMetricsSnapshot | None:
        attempts = self.get_recent_attempts(
            provider=provider,
            model=model,
            limit=METRICS_ATTEMPT_LIMIT,
            since=as_of - METRICS_MAX_AGE,
        )
        if not attempts:
            return None

        success_count = sum(attempt.success for attempt in attempts)
        latencies = [attempt.latency_ms for attempt in attempts]
        costs = [
            attempt.estimated_cost
            for attempt in attempts
            if attempt.success and attempt.estimated_cost is not None
        ]
        sample_count = len(attempts)

        return ProviderMetricsSnapshot(
            provider=provider,
            model=model,
            sample_count=sample_count,
            success_count=success_count,
            failure_count=sample_count - success_count,
            success_rate=success_count / sample_count,
            average_latency_ms=math.fsum(latencies) / sample_count,
            p50_latency_ms=_nearest_rank(latencies, 0.50),
            p95_latency_ms=_nearest_rank(latencies, 0.95),
            priced_sample_count=len(costs),
            estimated_average_cost=(sum(costs, Decimal(0)) / len(costs) if costs else None),
            p50_estimated_cost=_nearest_rank(costs, 0.50),
            window_start=min(attempt.finished_at for attempt in attempts),
            window_end=max(attempt.finished_at for attempt in attempts),
        )

    def get_pricing(self, provider: str, model: str) -> ModelPricing | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider, model, input_cost_per_million_tokens,
                       output_cost_per_million_tokens, currency, source, updated_at
                FROM pricing
                WHERE provider = ? AND model = ?
                """,
                (provider, model),
            ).fetchone()
        return _pricing_from_row(row) if row is not None else None

    def list_pricing(self) -> list[ModelPricing]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, input_cost_per_million_tokens,
                       output_cost_per_million_tokens, currency, source, updated_at
                FROM pricing
                ORDER BY provider, model
                """
            ).fetchall()
        return [_pricing_from_row(row) for row in rows]

    def upsert_pricing(self, pricing: ModelPricing) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pricing (
                    provider, model, input_cost_per_million_tokens,
                    output_cost_per_million_tokens, currency, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    input_cost_per_million_tokens = excluded.input_cost_per_million_tokens,
                    output_cost_per_million_tokens = excluded.output_cost_per_million_tokens,
                    currency = excluded.currency,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    pricing.provider,
                    pricing.model,
                    str(pricing.input_cost_per_million_tokens),
                    str(pricing.output_cost_per_million_tokens),
                    pricing.currency,
                    pricing.source,
                    _serialize_datetime(pricing.updated_at),
                ),
            )
            connection.commit()

    def record_routing_decision(self, decision: RoutingDecision) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO routing_decisions (
                        request_id, routing_version, selected_provider, selected_model,
                        served_provider, served_model, created_at, explanation_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.request_id,
                        decision.routing_version,
                        decision.selected_provider,
                        decision.selected_model,
                        decision.served_provider,
                        decision.served_model,
                        _serialize_datetime(decision.created_at),
                        decision.explanation.model_dump_json(),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            if "routing_decisions.request_id" in str(error):
                raise DuplicateRoutingDecisionError(
                    f"routing decision already exists for request {decision.request_id}"
                ) from error
            raise

    def get_routing_decision(self, request_id: str) -> RoutingDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, routing_version, selected_provider, selected_model,
                       served_provider, served_model, created_at, explanation_json
                FROM routing_decisions
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return _routing_decision_from_row(row) if row is not None else None

    def list_recent_routing_decisions(self, limit: int) -> list[RoutingDecision]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, routing_version, selected_provider, selected_model,
                       served_provider, served_model, created_at, explanation_json
                FROM routing_decisions
                ORDER BY created_at DESC, request_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_routing_decision_from_row(row) for row in rows]


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _serialize_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
    try:
        return AttemptRecord(
            request_id=row["request_id"],
            attempt_index=row["attempt_index"],
            provider=row["provider"],
            model=row["model"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            latency_ms=row["latency_ms"],
            success=row["success"],
            error_type=row["error_type"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            estimated_cost=row["estimated_cost"],
            created_at=row["created_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise MetricsStoreDataError("malformed attempt row") from error


def _pricing_from_row(row: sqlite3.Row) -> ModelPricing:
    try:
        return ModelPricing(
            provider=row["provider"],
            model=row["model"],
            input_cost_per_million_tokens=row["input_cost_per_million_tokens"],
            output_cost_per_million_tokens=row["output_cost_per_million_tokens"],
            currency=row["currency"],
            source=row["source"],
            updated_at=row["updated_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise MetricsStoreDataError("malformed pricing row") from error


def _routing_decision_from_row(row: sqlite3.Row) -> RoutingDecision:
    try:
        explanation = RoutingExplanation.model_validate_json(row["explanation_json"])
        return RoutingDecision(
            request_id=row["request_id"],
            routing_version=row["routing_version"],
            selected_provider=row["selected_provider"],
            selected_model=row["selected_model"],
            served_provider=row["served_provider"],
            served_model=row["served_model"],
            created_at=row["created_at"],
            explanation=explanation,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise MetricsStoreDataError("malformed routing decision row") from error


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return ordered[index]
