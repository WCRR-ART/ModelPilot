"""Synthetic answer text only; all evaluation runs without providers or storage."""

from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from modelpilot.benchmarks import (
    CaseEvaluation,
    ContainsSpec,
    ExactMatchSpec,
    JsonEqualSpec,
    NormalizedExactMatchSpec,
    NumericToleranceSpec,
    evaluate_case,
    load_benchmark_suite,
    normalize_text,
)


@pytest.mark.parametrize(
    "expected,actual,passed",
    [
        ("Paris", "Paris", True),
        ("Paris", "paris", False),
        ("Paris", " Paris", False),
        ("Paris", "Paris ", False),
        ("Paris", "Paris\n", False),
        ("a b", "a  b", False),
        ("a b", "a\tb", False),
        ("é", "e\u0301", False),
        ("é", "é", True),
        ("", "", True),
        ("", " ", False),
    ],
)
def test_exact(expected, actual, passed):
    result = evaluate_case(ExactMatchSpec(kind="exact_match", expected_text=expected), actual)
    assert result.passed is passed
    assert result.score == int(passed)
    assert result.reason == ("match" if passed else "mismatch")


@pytest.mark.parametrize(
    "expected,actual,passed",
    [
        ("Paris", "  PARIS \n", True),
        ("a b", "a  \n\t b", True),
        ("é", "e\u0301", True),
        ("ABC", "ＡＢＣ", True),
        ("strasse", "Straße", True),
        ("a b", "a\u00a0b", True),
        ("", " \n\t", True),
        ("Paris", "London", False),
        ("Paris", "Paris!", False),
        ("run", "running", False),
    ],
)
def test_normalized(expected, actual, passed):
    spec = NormalizedExactMatchSpec(kind="normalized_exact_match", expected_text=expected)
    assert evaluate_case(spec, actual).passed is passed
    assert normalize_text(normalize_text(actual)) == normalize_text(actual)


@pytest.mark.parametrize(
    "actual,passed",
    [
        ("ModelPilot", True),
        ("ModelPilot welcome", True),
        ("Hello ModelPilot world", True),
        ("Welcome to ModelPilot", True),
        ("modelpilot", False),
        ("Model Pilot", False),
        ("", False),
    ],
)
def test_contains(actual, passed):
    assert (
        evaluate_case(ContainsSpec(kind="contains", expected_text="ModelPilot"), actual).passed
        is passed
    )


def test_contains_literal_and_empty_validation():
    assert not evaluate_case(ContainsSpec(kind="contains", expected_text="a.*b"), "axxb").passed
    with pytest.raises(ValidationError):
        ContainsSpec(kind="contains", expected_text="")


def numeric(expected="1", tolerance="0"):
    return NumericToleranceSpec(
        kind="numeric_tolerance", expected_number=Decimal(expected), tolerance=Decimal(tolerance)
    )


@pytest.mark.parametrize(
    "expected,actual,tolerance,passed",
    [
        ("1", "1", "0", True),
        ("-1", "-1", "0", True),
        ("0", "-0", "0", True),
        ("1.25", "1.25", "0", True),
        ("1000", "1e3", "0", True),
        ("3.14", "3.141", ".01", True),
        (".1", "0.3", ".2", True),
        (".1", "-0.1", ".2", True),
        (".1", "0.3000000000000000000000000001", ".2", False),
        ("3.14", "3.2", ".01", False),
        ("1", "1.0001", "0", False),
        ("1", " \t1\n", "0", True),
        ("0.12345678901234567890123456789", "0.12345678901234567890123456789", "0", True),
        ("0.12345678901234567890123456789", "0.12345678901234567890123456790", "1e-29", True),
        ("1e10000", "1", "1e10000", True),
        ("1e10000", "-1", "1e10000", False),
    ],
)
def test_numeric_boundaries(expected, actual, tolerance, passed):
    result = evaluate_case(numeric(expected, tolerance), actual)
    assert result.passed is passed
    assert result.reason == ("match" if passed else "outside_tolerance")


@pytest.mark.parametrize(
    "actual",
    [
        "banana",
        "The answer is 3.14",
        "approximately 3.14",
        "",
        " ",
        "NaN",
        "Infinity",
        "-Infinity",
        "-inf",
        "true",
        "null",
        "[]",
        '"1"',
        "+1",
        "01",
        ".5",
        "1.",
        "1_000",
        "1 2",
        "１２",
        "1e",
        "0x10",
        "```1```",
        "1e999999999999999999999999999999",
    ],
)
def test_invalid_number_is_a_result(actual):
    result = evaluate_case(numeric(), actual)
    assert result.reason == "invalid_number"
    assert not result.passed and result.score == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("tolerance", Decimal("-1")),
        ("tolerance", Decimal("NaN")),
        ("expected_number", Decimal("Infinity")),
        ("expected_number", True),
        ("expected_number", "1"),
    ],
)
def test_invalid_numeric_definition(field, value):
    data = {"kind": "numeric_tolerance", "expected_number": Decimal(1)}
    data[field] = value
    with pytest.raises(ValidationError):
        NumericToleranceSpec(**data)


def test_decimal_context_cannot_change_evaluation():
    spec = numeric("0.100000000000000000000001", "0.200000000000000000000001")
    answer = "0.300000000000000000000002"
    expected = evaluate_case(spec, answer)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.Emax = 2
        context.Emin = -2
        assert evaluate_case(spec, answer) == expected
    assert expected.passed


def test_decimal_comparison_matches_exact_rational_oracle():
    values = [Decimal(s) for s in ["-1e40", "-1.0001", "-1e-40", "0", "1e-40", "1.0001", "1e40"]]
    for actual in values:
        for expected in values:
            for tolerance in [Decimal(0), Decimal("1e-40"), Decimal("1.0001"), Decimal("1e40")]:
                result = evaluate_case(numeric(str(expected), str(tolerance)), str(actual))
                assert result.passed == (
                    abs(Fraction(actual) - Fraction(expected)) <= Fraction(tolerance)
                )


def test_unrepresentable_json_exponent_returns_failure():
    spec = JsonEqualSpec(kind="json_equal", expected_json="0")
    assert evaluate_case(spec, "1e-99999999999999999999999999").reason == "invalid_json"


@pytest.mark.parametrize(
    "expected,actual,passed",
    [
        ('{"a":1,"b":2}', '{"b":2,"a":1}', True),
        ('{"x":{"a":[1,2]}}', '{"x":{"a":[1.0,2]}}', True),
        ("[1,2]", "[1,2]", True),
        ("[1,2]", "[2,1]", False),
        ("[[1],[2]]", "[[1],[2]]", True),
        ("[1]", "[1,2]", False),
        ("true", "true", True),
        ("true", "1", False),
        ("0", "false", False),
        ('{"x":[true]}', '{"x":[1]}', False),
        ("null", "null", True),
        ("null", "false", False),
        ('"a"', '"a"', True),
        ('"a"', '"b"', False),
        ('"1"', "1", False),
        ("1", "1.0", True),
        ("0", "-0.0", True),
        ("1000", "1e3", True),
        ("0.123456789012345678901", "0.123456789012345678902", False),
        ("9007199254740993", "9007199254740992.0", False),
        ('{"a":1}', ' \n { "a" : 1 } \t', True),
        ("{}", "[]", False),
    ],
)
def test_json_semantics(expected, actual, passed):
    result = evaluate_case(JsonEqualSpec(kind="json_equal", expected_json=expected), actual)
    assert result.passed is passed
    assert result.reason == ("match" if passed else "mismatch")


@pytest.mark.parametrize(
    "actual",
    [
        "banana",
        "",
        "{",
        '{"a":1,"a":2}',
        "NaN",
        "[Infinity]",
        "[-Infinity]",
        "```json\n{}\n```",
        "{} trailing",
        '{"a":1,}',
        "[1e999]",
    ],
)
def test_invalid_json_is_a_result(actual):
    result = evaluate_case(JsonEqualSpec(kind="json_equal", expected_json="{}"), actual)
    assert result.score == 0 and not result.passed and result.reason == "invalid_json"


def specimens():
    return [
        (ExactMatchSpec(kind="exact_match", expected_text="x"), "x"),
        (NormalizedExactMatchSpec(kind="normalized_exact_match", expected_text="x"), " X "),
        (ContainsSpec(kind="contains", expected_text="x"), "xyz"),
        (numeric(), "1"),
        (JsonEqualSpec(kind="json_equal", expected_json="{}"), "{}"),
    ]


def test_dispatch_determinism_serialization_and_purity(monkeypatch):
    import os
    import socket
    import sqlite3

    from modelpilot.providers import DeepSeekProvider, GeminiProvider, OpenAIProvider

    def forbidden(*args, **kwargs):
        pytest.fail("evaluators must not perform I/O or read the environment")

    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    for provider in [OpenAIProvider, GeminiProvider, DeepSeekProvider]:
        monkeypatch.setattr(provider, "complete", forbidden)
    for spec, actual in specimens():
        before = spec.model_dump_json()
        result = evaluate_case(spec, actual)
        assert result.evaluator_kind == spec.kind
        assert result.evaluator_version == "1" and result.passed and result.score == 1
        assert all(evaluate_case(spec, actual) == result for _ in range(10))
        assert CaseEvaluation.model_validate_json(result.model_dump_json()) == result
        assert spec.model_dump_json() == before


@pytest.mark.parametrize("score", [-1, 1.1, float("nan"), float("inf"), True])
def test_result_score_bounds(score):
    with pytest.raises(ValidationError):
        CaseEvaluation(evaluator_kind="exact_match", score=score, passed=False, reason="mismatch")


def test_result_consistency_and_frozen():
    with pytest.raises(ValidationError):
        CaseEvaluation(evaluator_kind="exact_match", score=0, passed=True, reason="match")
    result = evaluate_case(specimens()[0][0], "x")
    with pytest.raises(ValidationError):
        result.score = 0


def test_programmer_errors_are_not_answer_failures():
    spec = ExactMatchSpec(kind="exact_match", expected_text="x")
    with pytest.raises(ValueError, match="unsupported"):
        evaluate_case(spec.model_copy(update={"kind": "unknown"}), "x")
    with pytest.raises(ValueError, match="version"):
        evaluate_case(spec.model_copy(update={"evaluator_version": "9"}), "x")
    with pytest.raises(TypeError):
        evaluate_case(spec, None)
    with pytest.raises(ValueError):
        evaluate_case(JsonEqualSpec.model_construct(kind="json_equal", expected_json="bad"), "{}")


def test_results_never_copy_answer_or_expected_secrets():
    secret = "synthetic-secret-not-a-credential" * 10000
    specs = [
        ExactMatchSpec(kind="exact_match", expected_text=secret),
        NormalizedExactMatchSpec(kind="normalized_exact_match", expected_text=secret),
        ContainsSpec(kind="contains", expected_text=secret),
        numeric(),
        JsonEqualSpec(kind="json_equal", expected_json='"synthetic-only"'),
    ]
    for spec in specs:
        serialized = evaluate_case(spec, secret).model_dump_json()
        assert "synthetic" not in serialized and len(serialized) < 250


def test_smoke_and_lossless_decimal_loading(tmp_path):
    root = Path(__file__).resolve().parents[2]
    suite = load_benchmark_suite(root / "benchmarks/suites/smoke-v1.json")
    assert all(
        evaluate_case(case.evaluator, answer).passed
        for case, answer in zip(suite.cases, ["PINE", "42", '{"ready":true}'], strict=True)
    )
    path = tmp_path / "precise.json"
    data = suite.model_dump_json().replace(
        '"expected_number":"42"', '"expected_number":0.12345678901234567890123456789'
    )
    # Serialized Decimal strings are for model round trips, dataset input uses numeric tokens.
    data = data.replace('"tolerance":"0"', '"tolerance":0')
    path.write_text(data, encoding="utf-8")
    spec = load_benchmark_suite(path).cases[1].evaluator
    assert isinstance(spec.expected_number, Decimal)
    assert evaluate_case(spec, "0.12345678901234567890123456789").passed
