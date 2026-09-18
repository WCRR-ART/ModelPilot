"""Original synthetic fixtures; no production prompts or provider execution."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from modelpilot.benchmarks import (
    BenchmarkLoadError,
    BenchmarkSuite,
    load_benchmark_suite,
)
from modelpilot.metrics import SCHEMA_VERSION


def definition():
    return {
        "suite_id": "test-suite",
        "version": "1",
        "name": "Synthetic test",
        "cases": [
            {
                "case_id": "echo",
                "category": "instruction_following",
                "messages": [{"role": "user", "content": "Reply PINE."}],
                "evaluator": {"kind": "exact_match", "expected_text": "PINE"},
            }
        ],
    }


def test_minimal_definition_defaults_and_round_trip():
    suite = BenchmarkSuite.model_validate(definition())
    assert suite.identity == ("test-suite", "1")
    assert suite.description == ""
    assert suite.cases[0].weight == 1
    assert suite.cases[0].evaluator.evaluator_version == "1"
    assert BenchmarkSuite.model_validate_json(suite.model_dump_json()) == suite


@pytest.mark.parametrize("field", ["suite_id", "version", "name", "cases"])
def test_required_suite_fields(field):
    raw = definition()
    del raw[field]
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize("field", ["suite_id", "version", "name"])
@pytest.mark.parametrize("value", ["", "  ", None, 1])
def test_invalid_suite_text(field, value):
    raw = definition()
    raw[field] = value
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize("field", ["case_id", "category", "messages", "evaluator"])
def test_required_case_fields(field):
    raw = definition()
    del raw["cases"][0][field]
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_id", ""),
        ("case_id", "  "),
        ("category", "unknown"),
        ("messages", []),
        ("weight", 0),
        ("weight", -1),
        ("weight", True),
        ("weight", "2"),
        ("weight", float("nan")),
        ("weight", float("inf")),
    ],
)
def test_invalid_case_fields(field, value):
    raw = definition()
    raw["cases"][0][field] = value
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_valid_message_roles_and_content_preserved(role):
    raw = definition()
    raw["cases"][0]["messages"] = [{"role": role, "content": "  自建样例 🌲\n"}]
    assert BenchmarkSuite.model_validate(raw).cases[0].messages[0].content == "  自建样例 🌲\n"


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "x"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "  "},
        {"role": "user", "content": 42},
        {"role": "user"},
        {"content": "x"},
    ],
)
def test_invalid_messages(message):
    raw = definition()
    raw["cases"][0]["messages"] = [message]
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "exact_match", "expected_text": ""},
        {"kind": "normalized_exact_match", "expected_text": "PINE"},
        {"kind": "contains", "expected_text": "PINE"},
        {"kind": "numeric_tolerance", "expected_number": 2, "tolerance": 0.01},
        {"kind": "json_equal", "expected_json": '{"ok": [true, null, 2.5]}'},
        {"kind": "json_equal", "expected_json": "null"},
    ],
)
def test_valid_evaluator_specs(spec):
    raw = definition()
    raw["cases"][0]["evaluator"] = spec
    suite = BenchmarkSuite.model_validate(raw)
    assert suite.cases[0].evaluator.kind == spec["kind"]
    assert BenchmarkSuite.model_validate_json(suite.model_dump_json()) == suite


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "llm_judge", "expected_text": "x"},
        {"kind": "exact_match"},
        {"kind": "exact_match", "expected_text": 2},
        {"kind": "exact_match", "expected_text": None},
        {"kind": "exact_match", "expected_text": "x", "evaluator_version": "2"},
        {"kind": "contains", "expected_text": ""},
        {"kind": "numeric_tolerance", "expected_number": "2"},
        {"kind": "numeric_tolerance", "expected_number": True},
        {"kind": "numeric_tolerance", "expected_number": float("nan")},
        {"kind": "numeric_tolerance", "expected_number": 2, "tolerance": -1},
        {"kind": "numeric_tolerance", "expected_number": 2, "tolerance": float("inf")},
        {"kind": "json_equal", "expected_json": {"ok": True}},
        {"kind": "json_equal", "expected_json": "{bad}"},
        {"kind": "json_equal", "expected_json": '{"a":1,"a":2}'},
        {"kind": "json_equal", "expected_json": "NaN"},
        {"kind": "json_equal", "expected_json": "[1e999]"},
    ],
)
def test_invalid_evaluator_specs(spec):
    raw = definition()
    raw["cases"][0]["evaluator"] = spec
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


@pytest.mark.parametrize("level", ["suite", "case", "message", "evaluator"])
def test_unknown_fields_rejected(level):
    raw = definition()
    targets = {
        "suite": raw,
        "case": raw["cases"][0],
        "message": raw["cases"][0]["messages"][0],
        "evaluator": raw["cases"][0]["evaluator"],
    }
    targets[level]["unknown_critical_field"] = True
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


def test_empty_and_duplicate_cases_rejected():
    raw = definition()
    raw["cases"] *= 2
    with pytest.raises(ValidationError, match="duplicate case_id"):
        BenchmarkSuite.model_validate(raw)
    raw["cases"] = []
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(raw)


def test_utf8_order_and_nested_immutability(tmp_path):
    raw = definition()
    second = deepcopy(raw["cases"][0])
    second["case_id"] = "a-first-alphabetically"
    raw["cases"].append(second)
    raw["name"] = "自建测试 🌲"
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    suite = load_benchmark_suite(path)
    assert suite.name == "自建测试 🌲"
    assert [c.case_id for c in suite.cases] == ["echo", "a-first-alphabetically"]
    assert load_benchmark_suite(path) == suite
    for obj, field, value in [
        (suite, "name", "changed"),
        (suite.cases[0], "weight", 3),
        (suite.cases[0].messages[0], "content", "changed"),
        (suite.cases[0].evaluator, "expected_text", "changed"),
    ]:
        with pytest.raises(ValidationError, match="frozen"):
            setattr(obj, field, value)
    with pytest.raises(TypeError):
        suite.cases[0] = second
    with pytest.raises(TypeError):
        suite.cases[0].messages[0] = second


@pytest.mark.parametrize(
    "content", ["{bad}", "{}", "[]", "null", '{"a":1,"a":2}', '{"number": NaN}', '{"number":1e999}']
)
def test_invalid_json_or_schema_fails_clearly(tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BenchmarkLoadError):
        load_benchmark_suite(path)


def test_missing_and_non_utf8_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_benchmark_suite(tmp_path / "missing.json")
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    with pytest.raises(BenchmarkLoadError, match="UTF-8"):
        load_benchmark_suite(path)


def test_code_shaped_content_is_only_data(tmp_path, monkeypatch):
    import os

    def forbidden(*args, **kwargs):
        pytest.fail("dataset content must not be executed")

    monkeypatch.setattr(os, "system", forbidden)
    raw = definition()
    text = "__import__('os').system('not-a-command')"
    raw["cases"][0]["messages"][0]["content"] = text
    path = tmp_path / "data.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_benchmark_suite(path).cases[0].messages[0].content == text


def test_smoke_loads_without_provider_calls_or_schema_change(monkeypatch):
    from modelpilot.providers import DeepSeekProvider, GeminiProvider, OpenAIProvider

    async def forbidden(*args, **kwargs):
        pytest.fail("definition loading must not call providers")

    for provider in [DeepSeekProvider, GeminiProvider, OpenAIProvider]:
        monkeypatch.setattr(provider, "complete", forbidden)
    root = Path(__file__).resolve().parents[2]
    suite = load_benchmark_suite(root / "benchmarks/suites/smoke-v1.json")
    assert suite.identity == ("modelpilot-smoke", "1")
    assert 2 <= len(suite.cases) <= 5
    assert SCHEMA_VERSION == 3
