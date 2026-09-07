from datetime import UTC, datetime
from decimal import Decimal
from math import isclose
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
UnitScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
MetricName = Literal["quality", "latency", "reliability", "cost"]
ReasonValue = str | int | float | bool | None

METRIC_NAMES = frozenset({"quality", "latency", "reliability", "cost"})


class MetricsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AttemptRecord(MetricsModel):
    request_id: NonEmptyStr
    attempt_index: NonNegativeInt = 0
    provider: NonEmptyStr
    model: NonEmptyStr
    started_at: AwareDatetime
    finished_at: AwareDatetime
    latency_ms: NonNegativeFloat
    success: bool
    error_type: str | None = None
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    estimated_cost: NonNegativeDecimal | None = None
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_timestamps(self) -> "AttemptRecord":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self


class ProviderMetricsSnapshot(MetricsModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    sample_count: NonNegativeInt
    success_count: NonNegativeInt
    failure_count: NonNegativeInt
    success_rate: UnitScore | None = None
    average_latency_ms: NonNegativeFloat | None = None
    p50_latency_ms: NonNegativeFloat | None = None
    p95_latency_ms: NonNegativeFloat | None = None
    estimated_average_cost: NonNegativeDecimal | None = None
    window_start: AwareDatetime
    window_end: AwareDatetime

    @model_validator(mode="after")
    def validate_counts_and_aggregates(self) -> "ProviderMetricsSnapshot":
        if self.window_end < self.window_start:
            raise ValueError("window_end must not be earlier than window_start")
        if self.success_count + self.failure_count != self.sample_count:
            raise ValueError("sample_count must equal success_count plus failure_count")

        aggregates = (
            self.average_latency_ms,
            self.p50_latency_ms,
            self.p95_latency_ms,
            self.estimated_average_cost,
        )
        if self.sample_count == 0:
            if self.success_rate is not None or any(value is not None for value in aggregates):
                raise ValueError("empty snapshots must use None for rates and aggregates")
            return self

        expected_rate = self.success_count / self.sample_count
        if self.success_rate is None or not isclose(
            self.success_rate, expected_rate, rel_tol=0, abs_tol=1e-9
        ):
            raise ValueError("success_rate must match the snapshot counts")
        return self


class ModelPricing(MetricsModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    input_cost_per_million_tokens: NonNegativeDecimal
    output_cost_per_million_tokens: NonNegativeDecimal
    currency: NonEmptyStr = "USD"
    source: NonEmptyStr
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class RoutingSignal(MetricsModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    quality_score: UnitScore
    latency_score: UnitScore
    reliability_score: UnitScore
    cost_score: UnitScore
    measured_sample_count: NonNegativeInt
    measured_confidence: UnitScore
    final_score: UnitScore
    sources: dict[MetricName, NonEmptyStr]
    reason_metadata: dict[str, ReasonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_all_sources(self) -> "RoutingSignal":
        if set(self.sources) != METRIC_NAMES:
            raise ValueError("sources must describe quality, latency, reliability, and cost")
        return self


class RoutingExplanation(MetricsModel):
    strategy: NonEmptyStr
    selected: RoutingSignal
    candidates: tuple[RoutingSignal, ...] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def selected_must_be_a_candidate(self) -> "RoutingExplanation":
        selected_key = (self.selected.provider, self.selected.model)
        candidate_keys = {(item.provider, item.model) for item in self.candidates}
        if selected_key not in candidate_keys:
            raise ValueError("selected route must be present in candidates")
        return self
