"""Pure latest-attempt quality evidence, separate from operational reliability."""

from collections.abc import Sequence
from datetime import UTC, datetime
from fractions import Fraction
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from modelpilot.benchmarks.models import (
    BenchmarkCase,
    BenchmarkCategory,
    BenchmarkSuite,
    DefinitionModel,
    Identifier,
)
from modelpilot.benchmarks.results import (
    BenchmarkCaseResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkTarget,
    Count,
)

Ratio = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class QualityMetrics(DefinitionModel):
    quality_score: Ratio | None
    coverage: Ratio
    execution_completeness: Ratio
    weighted_evaluation_coverage: Ratio
    confidence: Ratio
    total_cases: Annotated[int, Field(strict=True, gt=0)]
    observed_cases: Count
    evaluated_cases: Count
    execution_failed_cases: Count

    @model_validator(mode="after")
    def consistent_metrics(self) -> Self:
        if not self.evaluated_cases <= self.observed_cases <= self.total_cases:
            raise ValueError("inconsistent evidence counts")
        if self.execution_failed_cases != self.observed_cases - self.evaluated_cases:
            raise ValueError("failure count disagrees")
        if (self.quality_score is None) != (self.evaluated_cases == 0):
            raise ValueError("quality must be null exactly when no cases were evaluated")
        if self.coverage != self.observed_cases / self.total_cases:
            raise ValueError("coverage disagrees with counts")
        if self.execution_completeness != self.evaluated_cases / self.total_cases:
            raise ValueError("completeness disagrees with counts")
        if self.evaluated_cases == 0 and self.weighted_evaluation_coverage != 0:
            raise ValueError("no evaluations means zero evaluated weight")
        if self.evaluated_cases == self.total_cases and self.weighted_evaluation_coverage != 1:
            raise ValueError("all evaluations means full evaluated weight")
        expected = _confidence(
            self.evaluated_cases, self.weighted_evaluation_coverage, self.execution_completeness
        )
        if self.confidence != expected:
            raise ValueError("confidence disagrees with policy")
        return self


class CategoryQualitySnapshot(QualityMetrics):
    category: BenchmarkCategory


class QualitySnapshot(QualityMetrics, BenchmarkTarget):
    suite_id: Identifier
    suite_version: Identifier
    suite_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Literal["latest_attempt_v1"] = "latest_attempt_v1"
    run_config: BenchmarkRunConfig | None
    source_run_count: Count
    source_run_ids: tuple[Identifier, ...]
    latest_run_id: Identifier | None
    latest_run_coverage: Ratio | None
    latest_run_completeness: Ratio | None
    categories: tuple[CategoryQualitySnapshot, ...]
    generated_at: AwareDatetime

    @field_validator("generated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_provenance(self) -> Self:
        if self.source_run_ids != tuple(sorted(set(self.source_run_ids))):
            raise ValueError("source IDs must be unique and sorted")
        if self.source_run_count != len(self.source_run_ids):
            raise ValueError("source count disagrees")
        if bool(self.source_run_count) != bool(self.observed_cases):
            raise ValueError("sources disagree with observations")
        if self.source_run_count:
            if self.latest_run_id not in self.source_run_ids or self.run_config is None:
                raise ValueError("latest run and config required")
            if self.latest_run_coverage is None or self.latest_run_completeness is None:
                raise ValueError("latest run diagnostics required")
            if not self.latest_run_completeness <= self.latest_run_coverage <= self.coverage:
                raise ValueError("inconsistent latest run diagnostics")
            if self.latest_run_completeness > self.execution_completeness:
                raise ValueError("latest evaluations exceed combined evaluations")
        elif any(
            v is not None
            for v in (
                self.latest_run_id,
                self.latest_run_coverage,
                self.latest_run_completeness,
                self.run_config,
            )
        ):
            raise ValueError("empty evidence cannot have latest run metadata")
        if len({c.category for c in self.categories}) != len(self.categories):
            raise ValueError("duplicate categories")
        for field in ("total_cases", "observed_cases", "evaluated_cases", "execution_failed_cases"):
            if sum(getattr(c, field) for c in self.categories) != getattr(self, field):
                raise ValueError("category counts disagree with overall")
        return self


def _confidence(n: int, weighted_coverage: float, completeness: float) -> float:
    return max(0.0, min(1.0, (n - 5) / 45)) * weighted_coverage * completeness


def _metrics(
    cases: Sequence[BenchmarkCase],
    evidence: dict[str, BenchmarkCaseResult],
) -> QualityMetrics:
    evaluated = [
        (c, evidence[c.case_id].evaluation)
        for c in cases
        if c.case_id in evidence and evidence[c.case_id].evaluation is not None
    ]
    # Exact rational arithmetic avoids overflow/underflow with valid extreme finite weights.
    total_weight = sum((Fraction(c.weight) for c in cases), Fraction())
    evaluated_weight = sum((Fraction(c.weight) for c, _ in evaluated), Fraction())
    quality = (
        float(
            sum((Fraction(c.weight) * Fraction(e.score) for c, e in evaluated), Fraction())
            / evaluated_weight
        )
        if evaluated
        else None
    )
    observed = sum(c.case_id in evidence for c in cases)
    completeness = len(evaluated) / len(cases)
    weighted_coverage = float(evaluated_weight / total_weight)
    return QualityMetrics(
        quality_score=quality,
        coverage=observed / len(cases),
        execution_completeness=completeness,
        weighted_evaluation_coverage=weighted_coverage,
        confidence=_confidence(len(evaluated), weighted_coverage, completeness),
        total_cases=len(cases),
        observed_cases=observed,
        evaluated_cases=len(evaluated),
        execution_failed_cases=observed - len(evaluated),
    )


def aggregate_quality(
    suite: BenchmarkSuite,
    runs: Sequence[BenchmarkRun],
    *,
    target: BenchmarkTarget,
    generated_at: datetime,
) -> QualitySnapshot:
    """Aggregate only supplied compatible history; does not discover or fetch runs."""
    suite = BenchmarkSuite.model_validate(suite.model_dump())
    target = BenchmarkTarget.model_validate(target.model_dump())
    fingerprint = sha256(suite.model_dump_json().encode("utf-8")).hexdigest()
    unique: dict[str, BenchmarkRun] = {}
    config = None
    for original in runs:
        run = BenchmarkRun.model_validate(original.model_dump())
        if (run.provider, run.model) != (target.provider, target.model):
            raise ValueError("run target mismatch")
        if (run.suite_id, run.suite_version, run.suite_fingerprint) != (
            suite.suite_id,
            suite.version,
            fingerprint,
        ):
            raise ValueError("run suite identity/fingerprint mismatch")
        if config is not None and run.config != config:
            raise ValueError("incompatible run configuration")
        config = run.config
        if run.total_cases != len(suite.cases):
            raise ValueError("run total disagrees with definition")
        for case, result in zip(suite.cases, run.case_results, strict=False):
            if (case.case_id, case.category) != (result.case_id, result.category):
                raise ValueError("case identity/category/order mismatch")
            if result.evaluation is not None and (
                result.evaluation.evaluator_kind,
                result.evaluation.evaluator_version,
            ) != (case.evaluator.kind, case.evaluator.evaluator_version):
                raise ValueError("evaluator provenance mismatch")
        if run.run_id in unique and unique[run.run_id] != run:
            raise ValueError("conflicting duplicate run ID")
        unique[run.run_id] = run
    ordered = sorted(unique.values(), key=lambda r: (r.finished_at, r.run_id))
    evidence: dict[str, BenchmarkCaseResult] = {}
    sources: dict[str, str] = {}
    for run in ordered:
        for result in run.case_results:
            evidence[result.case_id] = result
            sources[result.case_id] = run.run_id
    categories = tuple(dict.fromkeys(c.category for c in suite.cases))
    latest = ordered[-1] if ordered else None
    source_ids = tuple(sorted(set(sources.values())))
    return QualitySnapshot(
        **_metrics(suite.cases, evidence).model_dump(),
        provider=target.provider,
        model=target.model,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_fingerprint=fingerprint,
        run_config=config,
        source_run_count=len(source_ids),
        source_run_ids=source_ids,
        latest_run_id=latest.run_id if latest else None,
        latest_run_coverage=len(latest.case_results) / len(suite.cases) if latest else None,
        latest_run_completeness=latest.completed_cases / len(suite.cases) if latest else None,
        categories=tuple(
            CategoryQualitySnapshot(
                category=category,
                **_metrics(
                    [c for c in suite.cases if c.category == category], evidence
                ).model_dump(),
            )
            for category in categories
        ),
        generated_at=generated_at,
    )
