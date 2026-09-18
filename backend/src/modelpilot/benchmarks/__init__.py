"""Benchmark definitions and local dataset loading; no execution side effects."""

from modelpilot.benchmarks.evaluators import CaseEvaluation, evaluate_case, normalize_text
from modelpilot.benchmarks.loader import BenchmarkLoadError, load_benchmark_suite
from modelpilot.benchmarks.models import (
    BenchmarkCase,
    BenchmarkCategory,
    BenchmarkMessage,
    BenchmarkSuite,
    ContainsSpec,
    EvaluatorSpec,
    ExactMatchSpec,
    JsonEqualSpec,
    NormalizedExactMatchSpec,
    NumericToleranceSpec,
)

__all__ = [
    "BenchmarkCase",
    "BenchmarkCategory",
    "BenchmarkLoadError",
    "BenchmarkMessage",
    "BenchmarkSuite",
    "ContainsSpec",
    "CaseEvaluation",
    "EvaluatorSpec",
    "ExactMatchSpec",
    "JsonEqualSpec",
    "NormalizedExactMatchSpec",
    "NumericToleranceSpec",
    "load_benchmark_suite",
    "evaluate_case",
    "normalize_text",
]
