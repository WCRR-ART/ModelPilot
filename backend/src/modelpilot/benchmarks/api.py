"""Read-only benchmark inspection; execution is deliberately absent from HTTP."""

import logging
from collections.abc import Callable
from typing import Annotated, Literal, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, ValidationError

from modelpilot.benchmarks.models import DefinitionModel
from modelpilot.benchmarks.quality import QualitySnapshot
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.benchmarks.results import (
    BenchmarkCaseResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkTarget,
    Count,
)
from modelpilot.benchmarks.store import BenchmarkStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/benchmarks", tags=["benchmarks"])
ResultT = TypeVar("ResultT")


class RunSummary(DefinitionModel):
    run_id: str
    suite_id: str
    suite_version: str
    suite_fingerprint: str
    provider: str
    model: str
    status: Literal["completed", "completed_with_failures"]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    total_cases: Count
    completed_cases: Count
    execution_failed_cases: Count
    terminated_early: bool


class IndexedCaseResult(BenchmarkCaseResult):
    case_index: Count


class RunDetail(RunSummary):
    config: BenchmarkRunConfig
    case_results: tuple[IndexedCaseResult, ...]


def _summary(run: BenchmarkRun) -> RunSummary:
    return RunSummary.model_validate(run.model_dump(include=set(RunSummary.model_fields)))


def _query(callback: Callable[[], ResultT]) -> ResultT:
    try:
        return callback()
    except Exception as exc:
        # Omit exception text entirely: it can contain paths, prompts and credentials.
        logger.warning("benchmark read failed; data unavailable")
        raise HTTPException(503, detail={"code": "benchmark_data_unavailable"}) from exc


def _store(request: Request) -> BenchmarkStore:
    store = request.app.state.benchmark_store
    if store is None:
        raise HTTPException(503, detail={"code": "benchmark_data_unavailable"})
    return store


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    provider: str | None = None,
    model: str | None = None,
    suite_id: str | None = None,
    suite_version: str | None = None,
) -> list[RunSummary]:
    store = _store(request)
    return _query(lambda: [
        _summary(run) for run in store.list_runs(
            limit, provider=provider, model=model, suite_id=suite_id, suite_version=suite_version,
        )
    ])


@router.get("/runs/{run_id}", response_model=RunDetail)
def run_detail(request: Request, run_id: str) -> RunDetail:
    store = _store(request)
    run = _query(lambda: store.get_run(run_id))
    if run is None:
        raise HTTPException(404, detail={"code": "benchmark_run_not_found"})
    return _query(lambda: RunDetail(
        **_summary(run).model_dump(),
        config=run.config,
        case_results=tuple(
            IndexedCaseResult(**case.model_dump(), case_index=index)
            for index, case in enumerate(run.case_results)
        ),
    ))


@router.get("/quality", response_model=QualitySnapshot)
def quality(request: Request, provider: str, model: str) -> QualitySnapshot:
    try:
        target = BenchmarkTarget(provider=provider, model=model)
    except ValidationError as exc:
        raise HTTPException(422, detail={"code": "invalid_benchmark_target"}) from exc
    resolver: BenchmarkQualityResolver | None = request.app.state.benchmark_quality
    if resolver is None:
        raise HTTPException(503, detail={"code": "quality_suite_not_configured"})
    snapshot = _query(lambda: resolver.get_quality_snapshot(target.provider, target.model))
    if snapshot is None:
        raise HTTPException(404, detail={"code": "no_quality_evidence"})
    return snapshot
