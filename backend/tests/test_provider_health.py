import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from modelpilot.health import (
    CircuitState,
    ProviderHealth,
    counts_as_circuit_failure,
)
from modelpilot.providers import ProviderErrorType

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
THRESHOLD = 3
COOLDOWN = timedelta(seconds=30)


def initial(now: datetime = NOW) -> ProviderHealth:
    return ProviderHealth.initial(provider="openai", model="test-model", now=now)


def fail(
    health: ProviderHealth,
    now: datetime,
    error_type: ProviderErrorType = ProviderErrorType.TIMEOUT,
    *,
    threshold: int = THRESHOLD,
    cooldown: timedelta = COOLDOWN,
) -> ProviderHealth:
    return health.record_failure(
        error_type,
        failure_threshold=threshold,
        cooldown=cooldown,
        now=now,
    )


def open_circuit(*, cooldown: timedelta = COOLDOWN) -> ProviderHealth:
    health = initial()
    health = fail(health, NOW + timedelta(seconds=1), cooldown=cooldown)
    health = fail(health, NOW + timedelta(seconds=2), cooldown=cooldown)
    return fail(health, NOW + timedelta(seconds=3), cooldown=cooldown)


def test_initial_state_is_closed() -> None:
    health = initial()

    assert health.state is CircuitState.CLOSED
    assert health.consecutive_failures == 0
    assert health.opened_at is None
    assert health.cooldown_until is None
    assert health.updated_at == NOW


def test_first_failure_stays_closed() -> None:
    health = fail(initial(), NOW + timedelta(seconds=1))

    assert health.state is CircuitState.CLOSED
    assert health.consecutive_failures == 1
    assert health.last_failure_at == NOW + timedelta(seconds=1)


def test_second_failure_below_threshold_stays_closed() -> None:
    health = fail(initial(), NOW + timedelta(seconds=1))
    health = fail(health, NOW + timedelta(seconds=2))

    assert health.state is CircuitState.CLOSED
    assert health.consecutive_failures == 2


def test_threshold_failure_opens_circuit() -> None:
    health = open_circuit()

    assert health.state is CircuitState.OPEN
    assert health.consecutive_failures == THRESHOLD
    assert health.opened_at == NOW + timedelta(seconds=3)
    assert health.cooldown_until == NOW + timedelta(seconds=33)


def test_closed_success_resets_failures() -> None:
    health = fail(initial(), NOW + timedelta(seconds=1))
    health = health.record_success(now=NOW + timedelta(seconds=2))

    assert health.state is CircuitState.CLOSED
    assert health.consecutive_failures == 0
    assert health.last_success_at == NOW + timedelta(seconds=2)
    assert health.last_failure_at == NOW + timedelta(seconds=1)


def test_open_before_cooldown_remains_open() -> None:
    health = open_circuit()

    evaluated = health.advance(now=NOW + timedelta(seconds=32))

    assert evaluated is health
    assert evaluated.state is CircuitState.OPEN
    assert evaluated.probe_eligible(now=NOW + timedelta(seconds=32)) is False


def test_open_exactly_at_cooldown_becomes_half_open() -> None:
    health = open_circuit()

    evaluated = health.advance(now=NOW + timedelta(seconds=33))

    assert evaluated.state is CircuitState.HALF_OPEN
    assert evaluated.probe_eligible(now=NOW + timedelta(seconds=33)) is True


def test_open_after_cooldown_becomes_half_open() -> None:
    health = open_circuit()

    evaluated = health.advance(now=NOW + timedelta(seconds=40))

    assert evaluated.state is CircuitState.HALF_OPEN


def test_half_open_success_closes_circuit() -> None:
    health = open_circuit().advance(now=NOW + timedelta(seconds=33))

    recovered = health.record_success(now=NOW + timedelta(seconds=34))

    assert recovered.state is CircuitState.CLOSED
    assert recovered.consecutive_failures == 0
    assert recovered.opened_at is None
    assert recovered.cooldown_until is None


def test_half_open_failure_reopens_and_restarts_cooldown() -> None:
    health = open_circuit().advance(now=NOW + timedelta(seconds=33))

    reopened = fail(health, NOW + timedelta(seconds=34))

    assert reopened.state is CircuitState.OPEN
    assert reopened.consecutive_failures == THRESHOLD + 1
    assert reopened.opened_at == NOW + timedelta(seconds=34)
    assert reopened.cooldown_until == NOW + timedelta(seconds=64)


def test_authentication_error_is_ignored() -> None:
    health = initial()

    result = fail(
        health,
        NOW + timedelta(seconds=1),
        ProviderErrorType.AUTHENTICATION_ERROR,
    )

    assert result is health
    assert result.consecutive_failures == 0
    assert result.last_failure_at is None


@pytest.mark.parametrize(
    "error_type",
    [
        ProviderErrorType.TIMEOUT,
        ProviderErrorType.CONNECTION_ERROR,
        ProviderErrorType.RATE_LIMIT,
        ProviderErrorType.PROVIDER_ERROR,
        ProviderErrorType.INVALID_RESPONSE,
        ProviderErrorType.UNKNOWN_ERROR,
    ],
)
def test_health_relevant_error_types_count(error_type: ProviderErrorType) -> None:
    assert counts_as_circuit_failure(error_type) is True

    health = fail(initial(), NOW + timedelta(seconds=1), error_type)

    assert health.consecutive_failures == 1
    assert health.last_failure_at == NOW + timedelta(seconds=1)


def test_only_authentication_error_is_not_a_circuit_failure() -> None:
    assert counts_as_circuit_failure(ProviderErrorType.AUTHENTICATION_ERROR) is False


def test_datetimes_are_timezone_aware_and_normalized_to_utc() -> None:
    eastern = timezone(timedelta(hours=8))
    health = initial(datetime(2026, 9, 9, 20, tzinfo=eastern))

    assert health.updated_at == NOW
    assert health.updated_at.tzinfo is UTC

    with pytest.raises(ValidationError):
        initial(datetime(2026, 9, 9, 12))

    with pytest.raises(ValueError, match="timezone-aware"):
        health.advance(now=datetime(2026, 9, 9, 13))


@pytest.mark.parametrize("threshold", [0, -1])
def test_invalid_failure_threshold_is_rejected(threshold: int) -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        fail(initial(), NOW + timedelta(seconds=1), threshold=threshold)


def test_negative_cooldown_is_rejected() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        fail(initial(), NOW + timedelta(seconds=1), cooldown=timedelta(microseconds=-1))


def test_zero_cooldown_is_immediately_half_open_eligible() -> None:
    health = initial()
    health = fail(health, NOW + timedelta(seconds=1), threshold=1, cooldown=timedelta(0))

    assert health.state is CircuitState.OPEN
    assert health.cooldown_until == NOW + timedelta(seconds=1)
    assert health.advance(now=NOW + timedelta(seconds=1)).state is CircuitState.HALF_OPEN


def test_repeated_transitions_are_deterministic() -> None:
    health = initial()
    transition_time = NOW + timedelta(seconds=1)

    first = fail(health, transition_time)
    second = fail(health, transition_time)

    assert first == second
    assert first.advance(now=transition_time) is first


def test_time_cannot_move_backwards() -> None:
    health = fail(initial(), NOW + timedelta(seconds=2))

    with pytest.raises(ValueError, match="earlier than updated_at"):
        health.advance(now=NOW + timedelta(seconds=1))


def test_health_snapshot_serializes_for_future_api_use() -> None:
    health = open_circuit()

    payload = json.loads(health.model_dump_json())

    assert payload == {
        "provider": "openai",
        "model": "test-model",
        "state": "OPEN",
        "consecutive_failures": 3,
        "opened_at": "2026-09-09T12:00:03Z",
        "cooldown_until": "2026-09-09T12:00:33Z",
        "last_failure_at": "2026-09-09T12:00:03Z",
        "last_success_at": None,
        "updated_at": "2026-09-09T12:00:03Z",
    }
