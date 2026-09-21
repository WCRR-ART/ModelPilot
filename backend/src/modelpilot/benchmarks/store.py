"""Independent immutable benchmark history; no production metrics contract."""

from typing import Protocol, runtime_checkable

from modelpilot.benchmarks.results import BenchmarkCaseResult, BenchmarkRun


class DuplicateBenchmarkRunError(RuntimeError):
    """The run ID already exists; history was not overwritten."""


class BenchmarkStoreDataError(RuntimeError):
    """Persisted benchmark data cannot be restored faithfully."""


@runtime_checkable
class BenchmarkStore(Protocol):
    def save_run(self, run: BenchmarkRun) -> None: ...

    def get_run(self, run_id: str) -> BenchmarkRun | None: ...

    def list_runs(
        self, limit: int = 20, *, provider: str | None = None, model: str | None = None,
        suite_id: str | None = None, suite_version: str | None = None,
    ) -> list[BenchmarkRun]: ...

    def list_matching_runs(
        self, provider: str, model: str, suite_id: str, suite_version: str, suite_fingerprint: str,
    ) -> list[BenchmarkRun]: ...

    def list_case_results(self, run_id: str) -> tuple[BenchmarkCaseResult, ...]: ...
