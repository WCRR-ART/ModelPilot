"""In-memory benchmark execution contracts; independent of production stores."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from modelpilot.benchmarks.evaluators import CaseEvaluation
from modelpilot.benchmarks.models import (
    BenchmarkCategory,
    DefinitionModel,
    FiniteNumber,
    Identifier,
    NonBlankText,
)
from modelpilot.providers.base import ProviderErrorType

Count = Annotated[int, Field(strict=True, ge=0)]


class BenchmarkTarget(DefinitionModel):
    provider: NonBlankText
    model: NonBlankText

    @field_validator("provider", "model")
    @classmethod
    def explicit_identity(cls, value: str) -> str:
        if value.strip().lower() == "auto" or value != value.strip():
            raise ValueError("benchmark target must be an explicit identity without padding")
        return value


class BenchmarkRunConfig(DefinitionModel):
    max_cases: Annotated[int, Field(strict=True, gt=0)] = 100
    case_timeout_seconds: Annotated[FiniteNumber, Field(gt=0)] = 30.0
    temperature: Annotated[FiniteNumber, Field(ge=0, le=2)] = 0.0
    max_tokens: Annotated[int, Field(strict=True, gt=0)] = 1024


class BenchmarkCaseResult(BenchmarkTarget):
    case_id: Identifier
    category: BenchmarkCategory
    execution_status: Literal["completed", "provider_failed"]
    latency_ms: Annotated[FiniteNumber, Field(ge=0)]
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    total_tokens: Count | None = None
    error_type: ProviderErrorType | None = None
    evaluation: CaseEvaluation | None = None

    @model_validator(mode="after")
    def consistent_execution(self) -> Self:
        if self.execution_status == "completed":
            if self.evaluation is None or self.error_type is not None:
                raise ValueError("completed cases require evaluation and no execution error")
        elif self.evaluation is not None or self.error_type is None:
            raise ValueError("failed cases require an error and no evaluation")
        return self


class BenchmarkRun(BenchmarkTarget):
    run_id: Identifier
    suite_id: Identifier
    suite_version: Identifier
    suite_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    config: BenchmarkRunConfig
    started_at: AwareDatetime
    finished_at: AwareDatetime
    status: Literal["completed", "completed_with_failures"]
    total_cases: Annotated[int, Field(strict=True, gt=0)]
    completed_cases: Count
    execution_failed_cases: Count
    terminated_early: StrictBool = False
    case_results: tuple[BenchmarkCaseResult, ...]

    @field_validator("started_at", "finished_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_run(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        completed = sum(r.execution_status == "completed" for r in self.case_results)
        failures = len(self.case_results) - completed
        if (completed, failures) != (self.completed_cases, self.execution_failed_cases):
            raise ValueError("case counts disagree")
        if not self.case_results or len(self.case_results) > self.total_cases:
            raise ValueError("invalid result count")
        if len({r.case_id for r in self.case_results}) != len(self.case_results):
            raise ValueError("duplicate result case_id")
        if any((r.provider, r.model) != (self.provider, self.model) for r in self.case_results):
            raise ValueError("result target differs from run target")
        auth_indices = [
            i
            for i, r in enumerate(self.case_results)
            if r.error_type == ProviderErrorType.AUTHENTICATION_ERROR
        ]
        if auth_indices and auth_indices != [len(self.case_results) - 1]:
            raise ValueError("authentication failure must be terminal")
        partial = len(self.case_results) < self.total_cases
        if self.terminated_early != partial or (partial and not auth_indices):
            raise ValueError("only authentication failure may terminate a run early")
        if self.status != ("completed_with_failures" if failures else "completed"):
            raise ValueError("run status disagrees with results")
        return self
