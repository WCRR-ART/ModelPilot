"""Dedicated benchmark definitions, evaluation and explicit isolated execution."""

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
from modelpilot.benchmarks.results import (
    BenchmarkCaseResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkTarget,
)
from modelpilot.benchmarks.runner import BenchmarkRunner

__all__ = [
    "BenchmarkCaseResult",
    "BenchmarkRun",
    "BenchmarkRunConfig",
    "BenchmarkRunner",
    "BenchmarkTarget",
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
