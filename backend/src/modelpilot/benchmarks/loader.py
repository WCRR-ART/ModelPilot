from pathlib import Path

from pydantic import ValidationError

from modelpilot.benchmarks._json import parse_json_document
from modelpilot.benchmarks.models import BenchmarkSuite


class BenchmarkLoadError(ValueError):
    """A local file is not a valid UTF-8 JSON benchmark definition."""


def load_benchmark_suite(path: str | Path) -> BenchmarkSuite:
    """Read exactly one explicit local file; reject the entire suite on any bad case.

    Filesystem errors (including FileNotFoundError) retain their standard types.
    Dataset/schema errors expose field locations, not input messages or expected values.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise BenchmarkLoadError("benchmark file must be UTF-8") from exc
    try:
        data = parse_json_document(text)
    except ValueError as exc:
        raise BenchmarkLoadError("invalid benchmark JSON document") from exc
    try:
        return BenchmarkSuite.model_validate(data)
    except ValidationError as exc:
        locations = ", ".join(
            ".".join(str(part) for part in error["loc"]) or "suite"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise BenchmarkLoadError(f"invalid benchmark definition at: {locations}") from exc
