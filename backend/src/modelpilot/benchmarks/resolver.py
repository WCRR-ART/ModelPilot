"""Explicit read-only benchmark quality boundary for production routing."""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from modelpilot.benchmarks.models import BenchmarkSuite
from modelpilot.benchmarks.quality import QualitySnapshot, aggregate_quality
from modelpilot.benchmarks.results import BenchmarkTarget
from modelpilot.benchmarks.store import BenchmarkStore


class QualitySignalSource(Protocol):
    @property
    def suite_identity(self) -> tuple[str, str, str]: ...

    def get_quality_snapshot(self, provider: str, model: str) -> QualitySnapshot | None: ...


class BenchmarkQualityResolver:
    def __init__(
        self,
        suite: BenchmarkSuite,
        store: BenchmarkStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.suite = BenchmarkSuite.model_validate(suite.model_dump())
        self.store = store
        self.clock = clock
        self._identity = (
            suite.suite_id,
            suite.version,
            sha256(self.suite.model_dump_json().encode("utf-8")).hexdigest(),
        )

    @property
    def suite_identity(self) -> tuple[str, str, str]:
        return self._identity

    def get_quality_snapshot(self, provider: str, model: str) -> QualitySnapshot | None:
        runs = self.store.list_matching_runs(provider, model, *self.suite_identity)
        if not runs:
            return None
        return aggregate_quality(
            self.suite,
            runs,
            target=BenchmarkTarget(provider=provider, model=model),
            generated_at=self.clock(),
        )
