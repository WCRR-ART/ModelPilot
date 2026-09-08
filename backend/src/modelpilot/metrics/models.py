from datetime import UTC, datetime
from decimal import Decimal
from math import isclose
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
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
    priced_sample_count: NonNegativeInt = 0
    estimated_average_cost: NonNegativeDecimal | None = None
    p50_estimated_cost: NonNegativeDecimal | None = None
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
            self.p50_estimated_cost,
        )
        if self.sample_count == 0:
            if (
                self.priced_sample_count != 0
                or self.success_rate is not None
                or any(value is not None for value in aggregates)
            ):
                raise ValueError("empty snapshots must use None for rates and aggregates")
            return self

        if self.priced_sample_count > self.success_count:
            raise ValueError("priced_sample_count must not exceed success_count")
        cost_aggregates = (self.estimated_average_cost, self.p50_estimated_cost)
        if self.priced_sample_count == 0 and any(
            value is not None for value in cost_aggregates
        ):
            raise ValueError("cost aggregates require priced samples")
        if self.priced_sample_count > 0 and any(
            value is None for value in cost_aggregates
        ):
            raise ValueError("priced samples require cost aggregates")

        expected_rate = self.success_count / self.sample_count
        if self.success_rate is not None and not isclose(
            self.success_rate,
            expected_rate,
            rel_tol=0,
            abs_tol=1e-9,
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
    rank: PositiveInt = 1
    selected: bool = False
    sources: dict[MetricName, NonEmptyStr]
    reason_metadata: dict[str, ReasonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_all_sources(self) -> "RoutingSignal":
        if set(self.sources) != METRIC_NAMES:
            raise ValueError("sources must describe quality, latency, reliability, and cost")
        return self


class RoutingExplanation(MetricsModel):
    request_id: NonEmptyStr
    routing_version: NonEmptyStr = "v0.2"
    strategy: NonEmptyStr
    selected_provider: NonEmptyStr
    selected_model: NonEmptyStr
    served_provider: NonEmptyStr | None = None
    served_model: NonEmptyStr | None = None
    selected: RoutingSignal
    candidates: tuple[RoutingSignal, ...] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def selected_must_be_a_candidate(self) -> "RoutingExplanation":
        selected_key = (self.selected.provider, self.selected.model)
        candidate_keys = [(item.provider, item.model) for item in self.candidates]
        if selected_key not in candidate_keys:
            raise ValueError("selected route must be present in candidates")
        if selected_key != (self.selected_provider, self.selected_model):
            raise ValueError("selected provider and model must match selected signal")
        if (self.served_provider is None) != (self.served_model is None):
            raise ValueError("served provider and model must both be set or both be None")
        if [item.rank for item in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("candidate ranks must be ordered, unique, and start at one")
        selected_items = [item for item in self.candidates if item.selected]
        if len(selected_items) != 1 or selected_items[0] != self.selected:
            raise ValueError("exactly one candidate must be selected")
        return self

    def with_served_by(self, provider: str, model: str) -> "RoutingExplanation":
        values = self.model_dump()
        values.update(served_provider=provider, served_model=model)
        return RoutingExplanation.model_validate(values)


class RoutingDecision(MetricsModel):
    request_id: NonEmptyStr
    routing_version: NonEmptyStr
    selected_provider: NonEmptyStr
    selected_model: NonEmptyStr
    served_provider: NonEmptyStr | None = None
    served_model: NonEmptyStr | None = None
    created_at: AwareDatetime
    explanation: RoutingExplanation

    @classmethod
    def from_explanation(cls, explanation: RoutingExplanation) -> "RoutingDecision":
        return cls(
            request_id=explanation.request_id,
            routing_version=explanation.routing_version,
            selected_provider=explanation.selected_provider,
            selected_model=explanation.selected_model,
            served_provider=explanation.served_provider,
            served_model=explanation.served_model,
            created_at=explanation.created_at,
            explanation=explanation,
        )

    @model_validator(mode="after")
    def decision_must_match_explanation(self) -> "RoutingDecision":
        mirrored = (
            self.request_id,
            self.routing_version,
            self.selected_provider,
            self.selected_model,
            self.served_provider,
            self.served_model,
            self.created_at,
        )
        explained = (
            self.explanation.request_id,
            self.explanation.routing_version,
            self.explanation.selected_provider,
            self.explanation.selected_model,
            self.explanation.served_provider,
            self.explanation.served_model,
            self.explanation.created_at,
        )
        if mirrored != explained:
            raise ValueError("routing decision metadata must match its explanation")
        return self
