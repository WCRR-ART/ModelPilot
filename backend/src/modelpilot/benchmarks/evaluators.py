"""Pure deterministic evaluators. No provider, storage, clock or environment dependencies."""

import re
import unicodedata
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_FLOOR,
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    localcontext,
)
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from modelpilot.benchmarks._json import parse_json_document
from modelpilot.benchmarks.models import (
    ContainsSpec,
    EvaluatorSpec,
    ExactMatchSpec,
    JsonEqualSpec,
    NormalizedExactMatchSpec,
    NumericToleranceSpec,
)

EvaluatorKind = Literal[
    "exact_match", "normalized_exact_match", "contains", "numeric_tolerance", "json_equal"
]
EvaluationReason = Literal[
    "match", "mismatch", "invalid_number", "outside_tolerance", "invalid_json"
]


class CaseEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluator_kind: EvaluatorKind
    evaluator_version: Literal["1"] = "1"
    score: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
    passed: StrictBool
    reason: EvaluationReason

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if self.passed != (self.score == 1) or self.passed != (self.reason == "match"):
            raise ValueError("passed, score and reason disagree")
        if self.reason in {"invalid_number", "invalid_json"} and self.score != 0:
            raise ValueError("invalid answers must score zero")
        return self


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", re.ASCII)


def _numeric_reason(spec: NumericToleranceSpec, actual: str) -> EvaluationReason:
    text = actual.strip()
    if _NUMBER.fullmatch(text) is None:
        return "invalid_number"
    try:
        number = Decimal(text)
    except InvalidOperation:
        return "invalid_number"
    if not number.is_finite():
        return "invalid_number"
    expected, tolerance = spec.expected_number, spec.tolerance
    if tolerance == 0 or number == expected:
        return "match" if number == expected else "outside_tolerance"
    # Tolerance is exactly representable at this precision. A downward-rounded gap
    # below it is inside; equality is inside only when subtraction was exact.
    # This avoids allocating digits for a potentially enormous exponent gap.
    precision = max(len(v.as_tuple().digits) for v in (number, expected, tolerance)) + 2
    try:
        with localcontext(
            Context(
                prec=precision,
                rounding=ROUND_FLOOR,
                Emax=MAX_EMAX,
                Emin=MIN_EMIN,
                clamp=0,
                traps=[],
                flags=[],
            )
        ) as context:
            difference = max(number, expected) - min(number, expected)
            if not difference.is_finite():
                return "invalid_number"
            within = difference < tolerance or (
                difference == tolerance and not context.flags[Inexact]
            )
    except DecimalException:
        return "invalid_number"
    return "match" if within else "outside_tolerance"


def _json_equal(left: object, right: object) -> bool:
    # Never inherit Python's accidental True == 1 behavior.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, Decimal)) and isinstance(right, (int, Decimal)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(v, right[k]) for k, v in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def evaluate_case(spec: EvaluatorSpec, actual_output: str) -> CaseEvaluation:
    """Bad answer text scores zero; bad spec/type/dispatch is a programmer error."""
    if not isinstance(actual_output, str):
        raise TypeError("actual_output must be text")
    if not isinstance(
        spec,
        (
            ExactMatchSpec,
            NormalizedExactMatchSpec,
            ContainsSpec,
            NumericToleranceSpec,
            JsonEqualSpec,
        ),
    ):
        raise TypeError("expected a validated evaluator spec")
    if spec.evaluator_version != "1":
        raise ValueError("unsupported evaluator version")
    reason: EvaluationReason
    if isinstance(spec, ExactMatchSpec) and spec.kind == "exact_match":
        reason = "match" if actual_output == spec.expected_text else "mismatch"
    elif isinstance(spec, NormalizedExactMatchSpec) and spec.kind == "normalized_exact_match":
        reason = (
            "match"
            if normalize_text(actual_output) == normalize_text(spec.expected_text)
            else "mismatch"
        )
    elif isinstance(spec, ContainsSpec) and spec.kind == "contains":
        reason = "match" if spec.expected_text in actual_output else "mismatch"
    elif isinstance(spec, NumericToleranceSpec) and spec.kind == "numeric_tolerance":
        reason = _numeric_reason(spec, actual_output)
    elif isinstance(spec, JsonEqualSpec) and spec.kind == "json_equal":
        # Expected data is trusted only after validation; an invalid spec must raise.
        expected = parse_json_document(spec.expected_json)
        try:
            actual = parse_json_document(actual_output)
            reason = "match" if _json_equal(expected, actual) else "mismatch"
        except (ValueError, RecursionError):
            reason = "invalid_json"
    else:
        raise ValueError("unsupported evaluator spec/kind")
    return CaseEvaluation(
        evaluator_kind=spec.kind,
        score=1.0 if reason == "match" else 0.0,
        passed=reason == "match",
        reason=reason,
    )
