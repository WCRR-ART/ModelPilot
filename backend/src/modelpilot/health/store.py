from typing import Protocol, runtime_checkable

from modelpilot.health.models import ProviderHealth


class ProviderHealthStoreDataError(RuntimeError):
    """Persisted health cannot be restored to a valid domain snapshot."""


@runtime_checkable
class ProviderHealthStore(Protocol):
    def get_health(self, provider: str, model: str) -> ProviderHealth | None: ...

    def upsert_health(self, health: ProviderHealth) -> None: ...

    def list_health(self) -> list[ProviderHealth]: ...
