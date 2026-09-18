"""Immutable benchmark definitions, independent of execution and production routing."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from modelpilot.benchmarks._json import parse_json_document

Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
NonBlankText = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class DefinitionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BenchmarkCategory(StrEnum):
    CODING = "coding"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    MATH = "math"
    INSTRUCTION_FOLLOWING = "instruction_following"


class BenchmarkMessage(DefinitionModel):
    role: Literal["system", "user", "assistant"]
    content: NonBlankText


class _VersionedEvaluator(DefinitionModel):
    evaluator_version: Literal["1"] = "1"


class ExactMatchSpec(_VersionedEvaluator):
    kind: Literal["exact_match"]
    expected_text: StrictStr


class NormalizedExactMatchSpec(_VersionedEvaluator):
    kind: Literal["normalized_exact_match"]
    expected_text: StrictStr


class ContainsSpec(_VersionedEvaluator):
    kind: Literal["contains"]
    expected_text: Annotated[str, StringConstraints(strict=True, min_length=1)]


class NumericToleranceSpec(_VersionedEvaluator):
    kind: Literal["numeric_tolerance"]
    expected_number: Annotated[Decimal, Field(allow_inf_nan=False)]
    tolerance: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] = Decimal(0)

    @field_validator("expected_number", "tolerance", mode="before")
    @classmethod
    def decimal_input(cls, value: object, info: ValidationInfo) -> object:
        # Pydantic emits Decimal as strings for lossless model JSON round-trips.
        if info.mode == "json" and isinstance(value, str):
            return value
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
            raise ValueError("expected a finite numeric value")
        return Decimal(str(value))


class JsonEqualSpec(_VersionedEvaluator):
    kind: Literal["json_equal"]
    expected_json: StrictStr

    @field_validator("expected_json")
    @classmethod
    def validate_expected_json(cls, value: str) -> str:
        # Keep the document as immutable text; no mutable JSON tree escapes the model.
        parse_json_document(value)
        return value


EvaluatorSpec = Annotated[
    ExactMatchSpec | NormalizedExactMatchSpec | ContainsSpec | NumericToleranceSpec | JsonEqualSpec,
    Field(discriminator="kind"),
]


class BenchmarkCase(DefinitionModel):
    case_id: Identifier
    category: BenchmarkCategory
    messages: Annotated[tuple[BenchmarkMessage, ...], Field(min_length=1)]
    evaluator: EvaluatorSpec
    weight: Annotated[FiniteNumber, Field(gt=0)] = 1.0


class BenchmarkSuite(DefinitionModel):
    suite_id: Identifier
    version: Identifier
    name: NonBlankText
    description: StrictStr = ""
    cases: Annotated[tuple[BenchmarkCase, ...], Field(min_length=1)]

    @property
    def identity(self) -> tuple[str, str]:
        return self.suite_id, self.version

    @model_validator(mode="after")
    def unique_case_ids(self) -> Self:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case_id in benchmark suite")
        return self
