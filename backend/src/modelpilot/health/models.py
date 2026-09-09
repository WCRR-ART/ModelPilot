from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from modelpilot.providers.base import ProviderErrorType

NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


CIRCUIT_FAILURE_TYPES = frozenset(
    {
        ProviderErrorType.TIMEOUT,
        ProviderErrorType.CONNECTION_ERROR,
        ProviderErrorType.RATE_LIMIT,
        ProviderErrorType.PROVIDER_ERROR,
        ProviderErrorType.INVALID_RESPONSE,
        ProviderErrorType.UNKNOWN_ERROR,
    }
)


def counts_as_circuit_failure(error_type: ProviderErrorType) -> bool:
    return error_type in CIRCUIT_FAILURE_TYPES


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: NonEmptyStr
    model: NonEmptyStr
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: NonNegativeInt = 0
    opened_at: AwareDatetime | None = None
    cooldown_until: AwareDatetime | None = None
    last_failure_at: AwareDatetime | None = None
    last_success_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @field_validator(
        "opened_at",
        "cooldown_until",
        "last_failure_at",
        "last_success_at",
        "updated_at",
    )
    @classmethod
    def normalize_datetime_to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        has_open_window = self.opened_at is not None and self.cooldown_until is not None
        if self.state is CircuitState.CLOSED and (
            self.opened_at is not None or self.cooldown_until is not None
        ):
            raise ValueError("closed circuits cannot have an active cooldown")
        if self.state is not CircuitState.CLOSED and not has_open_window:
            raise ValueError("open and half-open circuits require an active cooldown")
        if has_open_window and self.opened_at > self.cooldown_until:
            raise ValueError("cooldown_until must not be earlier than opened_at")
        for timestamp in (self.opened_at, self.last_failure_at, self.last_success_at):
            if timestamp is not None and timestamp > self.updated_at:
                raise ValueError("event timestamps must not be later than updated_at")
        return self

    @classmethod
    def initial(cls, *, provider: str, model: str, now: datetime) -> Self:
        return cls(provider=provider, model=model, updated_at=now)

    def advance(self, *, now: datetime) -> Self:
        now = self._transition_time(now)
        if (
            self.state is CircuitState.OPEN
            and self.cooldown_until is not None
            and now >= self.cooldown_until
        ):
            return self._replace(state=CircuitState.HALF_OPEN, updated_at=now)
        return self

    def probe_eligible(self, *, now: datetime) -> bool:
        return self.advance(now=now).state is CircuitState.HALF_OPEN

    def record_success(self, *, now: datetime) -> Self:
        now = self._transition_time(now)
        current = self.advance(now=now)
        if current.state is CircuitState.OPEN:
            raise ValueError("cannot record an outcome while the circuit is open")
        return current._replace(
            state=CircuitState.CLOSED,
            consecutive_failures=0,
            opened_at=None,
            cooldown_until=None,
            last_success_at=now,
            updated_at=now,
        )

    def record_failure(
        self,
        error_type: ProviderErrorType,
        *,
        failure_threshold: int,
        cooldown: timedelta,
        now: datetime,
    ) -> Self:
        _validate_policy(failure_threshold, cooldown)
        now = self._transition_time(now)
        current = self.advance(now=now)
        if not counts_as_circuit_failure(error_type):
            return current
        if current.state is CircuitState.OPEN:
            raise ValueError("cannot record an outcome while the circuit is open")

        failures = current.consecutive_failures + 1
        should_open = (
            current.state is CircuitState.HALF_OPEN or failures >= failure_threshold
        )
        if should_open:
            return current._replace(
                state=CircuitState.OPEN,
                consecutive_failures=failures,
                opened_at=now,
                cooldown_until=now + cooldown,
                last_failure_at=now,
                updated_at=now,
            )
        return current._replace(
            consecutive_failures=failures,
            last_failure_at=now,
            updated_at=now,
        )

    def _transition_time(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        normalized = now.astimezone(UTC)
        if normalized < self.updated_at:
            raise ValueError("now must not be earlier than updated_at")
        return normalized

    def _replace(self, **updates: object) -> Self:
        values = self.model_dump()
        values.update(updates)
        return type(self).model_validate(values)


def _validate_policy(failure_threshold: int, cooldown: timedelta) -> None:
    if isinstance(failure_threshold, bool) or failure_threshold < 1:
        raise ValueError("failure_threshold must be at least one")
    if cooldown < timedelta(0):
        raise ValueError("cooldown must not be negative")
