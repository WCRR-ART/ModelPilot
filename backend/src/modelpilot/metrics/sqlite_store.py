from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from modelpilot.health.models import ProviderHealth
from modelpilot.health.store import ProviderHealthStoreDataError
from modelpilot.metrics.models import (
    AttemptRecord,
    MetricsSummary,
    MetricsWindow,
    ModelPricing,
    ProviderMetricsSnapshot,
    RecentFailure,
    RoutingDecision,
    RoutingExplanation,
)
from modelpilot.sqlite_database import SCHEMA_VERSION as SCHEMA_VERSION
from modelpilot.sqlite_database import SchemaVersionError as SchemaVersionError
from modelpilot.sqlite_database import SQLiteDatabase
from modelpilot.sqlite_database import serialize_datetime as _serialize_datetime

METRICS_ATTEMPT_LIMIT = 100
METRICS_MAX_AGE = timedelta(days=7)
SAFE_ERROR_TYPES = frozenset(
    {
        "timeout",
        "connection_error",
        "authentication_error",
        "rate_limit",
        "provider_error",
        "invalid_response",
        "unknown_error",
    }
)


class MetricsStoreDataError(RuntimeError):
    """Raised when persisted data cannot be restored to a domain model."""


class DuplicateRoutingDecisionError(RuntimeError):
    """Raised when a routing decision already exists for a request ID."""


class SQLiteMetricsStore(SQLiteDatabase):
    """A small, synchronous SQLite implementation of the metrics store contract."""

    def get_health(self, provider: str, model: str) -> ProviderHealth | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        return _health_from_row(row) if row is not None else None

    def list_health(self) -> list[ProviderHealth]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_health ORDER BY provider, model"
            ).fetchall()
        return [_health_from_row(row) for row in rows]

    def upsert_health(self, health: ProviderHealth) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_health (
                    provider, model, state, consecutive_failures, opened_at,
                    cooldown_until, last_failure_at, last_success_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    state = excluded.state,
                    consecutive_failures = excluded.consecutive_failures,
                    opened_at = excluded.opened_at,
                    cooldown_until = excluded.cooldown_until,
                    last_failure_at = excluded.last_failure_at,
                    last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at
                """,
                (
                    health.provider, health.model, health.state.value,
                    health.consecutive_failures,
                    *(
                        _serialize_datetime(value) if value is not None else None
                        for value in (
                            health.opened_at, health.cooldown_until,
                            health.last_failure_at, health.last_success_at, health.updated_at,
                        )
                    ),
                ),
            )
            connection.commit()

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

    def get_metrics_summary(
        self,
        since: datetime,
        until: datetime,
    ) -> MetricsSummary:
        serialized_since = _serialize_datetime(since)
        serialized_until = _serialize_datetime(until)
        if until < since:
            raise ValueError("until must not be earlier than since")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT request_id) AS request_count,
                       COUNT(*) AS attempt_count,
                       COALESCE(SUM(success), 0) AS success_count,
                       AVG(latency_ms) AS average_latency_ms,
                       COUNT(DISTINCT provider) AS providers_count,
                       COUNT(DISTINCT model) AS models_count
                FROM attempts
                WHERE finished_at >= ? AND finished_at <= ?
                """,
                (serialized_since, serialized_until),
            ).fetchone()
            routing_decision_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM routing_decisions
                WHERE created_at >= ? AND created_at <= ?
                """,
                (serialized_since, serialized_until),
            ).fetchone()[0]
            cost_rows = connection.execute(
                """
                SELECT estimated_cost
                FROM attempts
                WHERE finished_at >= ? AND finished_at <= ?
                  AND estimated_cost IS NOT NULL
                """,
                (serialized_since, serialized_until),
            )
            try:
                costs = [Decimal(cost_row[0]) for cost_row in cost_rows]
            except (InvalidOperation, TypeError, ValueError) as error:
                raise MetricsStoreDataError("malformed estimated cost") from error

        attempt_count = row["attempt_count"]
        success_count = row["success_count"]
        return MetricsSummary(
            window=MetricsWindow(since=since, until=until),
            request_count=row["request_count"],
            attempt_count=attempt_count,
            success_count=success_count,
            failure_count=attempt_count - success_count,
            success_rate=(success_count / attempt_count if attempt_count else None),
            average_latency_ms=row["average_latency_ms"],
            estimated_total_cost=(sum(costs, Decimal(0)) if costs else None),
            providers_count=row["providers_count"],
            models_count=row["models_count"],
            routing_decision_count=routing_decision_count,
        )

    def list_recent_failures(self, limit: int) -> list[RecentFailure]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, provider, model, finished_at, created_at,
                       latency_ms, error_type
                FROM attempts
                WHERE success = 0
                ORDER BY finished_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_recent_failure_from_row(row) for row in rows]


def _health_from_row(row: sqlite3.Row) -> ProviderHealth:
    try:
        return ProviderHealth.model_validate(dict(row))
    except ValidationError as error:
        raise ProviderHealthStoreDataError("malformed provider health row") from error


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


def _recent_failure_from_row(row: sqlite3.Row) -> RecentFailure:
    try:
        stored_error_type = row["error_type"]
        safe_error_type = (
            stored_error_type if stored_error_type in SAFE_ERROR_TYPES else "unknown_error"
        )
        return RecentFailure(
            request_id=row["request_id"],
            provider=row["provider"],
            model=row["model"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
            latency_ms=row["latency_ms"],
            error_type=safe_error_type,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise MetricsStoreDataError("malformed failure row") from error


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return ordered[index]
