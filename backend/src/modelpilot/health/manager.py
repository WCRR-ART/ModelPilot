from datetime import timedelta

from modelpilot.health.models import ProviderHealth, _validate_policy
from modelpilot.health.store import ProviderHealthStore
from modelpilot.providers import ProviderOutcome


class HealthPersistenceError(RuntimeError):
    """A health store read or write failed."""


class ProviderHealthManager:
    """Apply domain transitions using the outcome's UTC completion timestamp."""

    def __init__(
        self, store: ProviderHealthStore, *, failure_threshold: int, cooldown: timedelta
    ) -> None:
        _validate_policy(failure_threshold, cooldown)
        self.store = store
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown

    def update(self, outcome: ProviderOutcome) -> ProviderHealth:
        try:
            current = self.store.get_health(outcome.provider, outcome.model)
        except Exception as exc:
            raise HealthPersistenceError("health read failed") from exc
        now = outcome.finished_at
        if current is None:
            current = ProviderHealth.initial(
                provider=outcome.provider, model=outcome.model, now=now
            )
        if outcome.success:
            updated = current.record_success(now=now)
        else:
            if outcome.error_type is None:
                raise ValueError("failed outcome requires an error type")
            updated = current.record_failure(
                outcome.error_type,
                failure_threshold=self.failure_threshold,
                cooldown=self.cooldown,
                now=now,
            )
        try:
            self.store.upsert_health(updated)
        except Exception as exc:
            raise HealthPersistenceError("health write failed") from exc
        return updated
