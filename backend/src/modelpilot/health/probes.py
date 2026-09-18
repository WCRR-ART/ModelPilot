from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class ProbeLease:
    provider: str
    model: str


class HalfOpenProbeCoordinator:
    """Process-local single-flight claims; never wait for a provider's response."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._leases: dict[tuple[str, str], ProbeLease] = {}

    def try_acquire(self, provider: str, model: str) -> ProbeLease | None:
        key = (provider, model)
        with self._lock:
            if key in self._leases:
                return None
            lease = ProbeLease(provider, model)
            self._leases[key] = lease
            return lease

    def release(self, lease: ProbeLease) -> None:
        key = (lease.provider, lease.model)
        with self._lock:
            # A repeated or stale release must never remove another request's lease.
            if self._leases.get(key) is lease:
                del self._leases[key]
