import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_health_aware_routing import opened
from test_metrics_api import make_attempt, make_client, make_decision

from modelpilot.benchmarks import (
    BenchmarkCase,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkRunner,
    BenchmarkSuite,
    BenchmarkTarget,
    load_benchmark_suite,
)
from modelpilot.metrics import SCHEMA_VERSION, SQLiteMetricsStore
from modelpilot.providers.base import Provider, ProviderErrorType, ProviderOutcome
from modelpilot.schemas import ChatCompletionRequest

NOW = datetime(2026, 9, 18, tzinfo=UTC)
TARGET = BenchmarkTarget(provider="openai", model="openai-model")
SMOKE = Path(__file__).resolve().parents[2] / "benchmarks/suites/smoke-v1.json"


def suite(count=1):
    return BenchmarkSuite(
        suite_id="test",
        version="1",
        name="Test",
        cases=tuple(
            BenchmarkCase(
                case_id=f"case-{i}",
                category="instruction_following",
                messages=[
                    {"role": "system", "content": "  Do not change me.\n"},
                    {"role": "user", "content": f"Prompt {i}"},
                ],
                evaluator={"kind": "exact_match", "expected_text": "PINE"},
            )
            for i in range(count)
        ),
    )


def outcome(text="PINE", *, error=None, **overrides):
    fields = dict(
        provider=TARGET.provider,
        model=TARGET.model,
        success=error is None,
        response={"choices": [{"message": {"content": text}}]} if error is None else None,
        error_type=error,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=12.5,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
    )
    fields.update(overrides)
    return ProviderOutcome(**fields)


class FakeProvider(Provider):
    name = "openai"

    def __init__(self, outcomes):
        super().__init__("fake-credential-do-not-retain")
        self.outcomes = iter(outcomes)
        self.calls: list[ChatCompletionRequest] = []
        self.entered = asyncio.Event()
        self.cancelled = False

    async def complete(self, request, model):
        assert model == request.model == TARGET.model
        self.calls.append(request)
        value = next(self.outcomes)
        if value == "hang":
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(value, BaseException):
            raise value
        return value


def run(definition, provider, config=None):
    return asyncio.run(
        BenchmarkRunner(clock=lambda: NOW).run(
            definition,
            TARGET,
            provider,
            config,
        )
    )


def test_minimal_and_metadata():
    provider = FakeProvider([outcome()])
    result = run(suite(), provider)
    assert result.status == "completed"
    assert result.completed_cases == result.total_cases == 1
    assert result.execution_failed_cases == 0
    case = result.case_results[0]
    assert (case.provider, case.model) == (TARGET.provider, TARGET.model)
    assert case.evaluation.score == 1
    assert (case.input_tokens, case.output_tokens, case.total_tokens) == (10, 2, 12)
    assert case.latency_ms == 12.5
    assert result.started_at == result.finished_at == NOW
    request = provider.calls[0]
    assert request.temperature == 0 and request.max_tokens == 1024
    assert not request.stream
    assert [(m.role, m.content) for m in request.messages] == [
        (m.role, m.content) for m in suite().cases[0].messages
    ]
    assert BenchmarkRun.model_validate_json(result.model_dump_json()) == result


def test_order_wrong_answer_and_determinism():
    definition = suite(3)
    runs = []
    for _ in range(2):
        provider = FakeProvider([outcome(), outcome("wrong"), outcome()])
        result = run(definition, provider)
        assert [r.case_id for r in result.case_results] == [c.case_id for c in definition.cases]
        assert [c.messages[-1].content for c in provider.calls] == [f"Prompt {i}" for i in range(3)]
        assert [r.evaluation.score for r in result.case_results] == [1, 0, 1]
        assert all(r.execution_status == "completed" for r in result.case_results)
        assert result.status == "completed"
        runs.append(result)
    assert runs[0].run_id != runs[1].run_id
    assert runs[0].model_dump(exclude={"run_id"}) == runs[1].model_dump(exclude={"run_id"})


@pytest.mark.parametrize(
    "error", [e for e in ProviderErrorType if e != ProviderErrorType.AUTHENTICATION_ERROR]
)
def test_normalized_failures_continue_without_retry(error):
    provider = FakeProvider([outcome(error=error), outcome()])
    result = run(suite(2), provider)
    assert len(provider.calls) == 2
    assert result.status == "completed_with_failures"
    assert result.execution_failed_cases == result.completed_cases == 1
    assert result.case_results[0].evaluation is None
    assert result.case_results[0].error_type == error
    assert result.case_results[1].evaluation.score == 1


@pytest.mark.parametrize("count", [2, 3])
def test_auth_terminal_preserves_results(count):
    provider = FakeProvider([outcome(), outcome(error="authentication_error")])
    result = run(suite(count), provider)
    assert len(provider.calls) == len(result.case_results) == 2
    assert result.terminated_early == (count == 3)
    assert result.case_results[0].evaluation.score == 1
    assert result.case_results[1].evaluation is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_cases", 0),
        ("max_cases", -1),
        ("max_cases", True),
        ("case_timeout_seconds", 0),
        ("case_timeout_seconds", -1),
        ("case_timeout_seconds", float("inf")),
        ("case_timeout_seconds", float("nan")),
        ("max_tokens", 0),
        ("temperature", -1),
        ("temperature", 3),
    ],
)
def test_invalid_config(field, value):
    with pytest.raises(ValidationError):
        BenchmarkRunConfig(**{field: value})


def test_max_cases_rejected_before_any_call_and_boundary_accepted():
    provider = FakeProvider([outcome(), outcome()])
    with pytest.raises(ValueError, match="max_cases"):
        run(suite(3), provider, BenchmarkRunConfig(max_cases=2))
    assert not provider.calls
    assert run(suite(2), provider, BenchmarkRunConfig(max_cases=2)).completed_cases == 2


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("value", ["auto", " AUTO ", "", "  ", " padded "])
def test_target_requires_explicit_identity(field, value):
    with pytest.raises(ValidationError):
        BenchmarkTarget(**{**TARGET.model_dump(), field: value})


def test_provider_mismatch_rejected_upfront():
    provider = FakeProvider([])
    provider.name = "other"
    with pytest.raises(ValueError, match="provider does not match"):
        run(suite(), provider)
    assert not provider.calls


@pytest.mark.parametrize("field", ["provider", "model"])
def test_outcome_identity_mismatch_is_invariant_error(field):
    with pytest.raises(ValueError, match="outcome does not match"):
        run(suite(), FakeProvider([outcome(**{field: "other"})]))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": None},
        {"choices": {}},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": []}}]},
        {"choices": [{"message": {"content": 12}}]},
    ],
)
def test_malformed_output_is_execution_failure_and_continues(response):
    result = run(suite(2), FakeProvider([outcome(response=response), outcome()]))
    assert result.case_results[0].error_type == "invalid_response"
    assert result.case_results[0].evaluation is None
    assert result.case_results[1].evaluation.score == 1


def test_missing_usage_stays_none_and_output_is_not_retained():
    result = run(
        suite(2),
        FakeProvider(
            [
                outcome("x" * 2_000_000, input_tokens=None, output_tokens=None, total_tokens=None),
                outcome(error="provider_error", error_message="Authorization: Bearer fake-private"),
            ]
        ),
    )
    case = result.case_results[0]
    assert (case.input_tokens, case.output_tokens, case.total_tokens) == (None, None, None)
    serialized = result.model_dump_json()
    assert len(serialized) < 4000
    for forbidden in (
        "actual_output",
        "messages",
        "expected_text",
        "Prompt",
        "Do not change",
        "Authorization",
        "fake-private",
        "fake-credential",
        "error_message",
    ):
        assert forbidden not in serialized


def test_deadline_cancels_hung_provider_and_continues():
    provider = FakeProvider(["hang", outcome()])
    result = run(suite(2), provider, BenchmarkRunConfig(case_timeout_seconds=0.01))
    assert provider.cancelled and len(provider.calls) == 2
    failed = result.case_results[0]
    assert failed.error_type == "timeout" and failed.evaluation is None
    assert failed.latency_ms >= 0 and failed.input_tokens is None
    assert result.case_results[1].evaluation.score == 1


def test_external_cancellation_propagates():
    async def exercise():
        provider = FakeProvider(["hang"])
        task = asyncio.create_task(BenchmarkRunner().run(suite(), TARGET, provider))
        await asyncio.wait_for(provider.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.cancelled and len(provider.calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [RuntimeError("invariant"), TimeoutError("adapter bug")])
def test_unexpected_adapter_errors_propagate(error):
    with pytest.raises(type(error), match=str(error)):
        run(suite(), FakeProvider([error]))


def test_evaluator_invariant_propagates(monkeypatch):
    def broken(*args):
        raise RuntimeError("evaluator invariant")

    monkeypatch.setattr("modelpilot.benchmarks.runner.evaluate_case", broken)
    with pytest.raises(RuntimeError, match="evaluator invariant"):
        run(suite(), FakeProvider([outcome()]))


def test_smoke_load_run_evaluate_and_invalid_answers():
    definition = load_benchmark_suite(SMOKE)
    result = run(
        definition, FakeProvider([outcome("PINE"), outcome("42"), outcome('{"ready":true}')])
    )
    assert result.completed_cases == 3
    assert all(r.evaluation.score == 1 for r in result.case_results)
    assert [r.case_id for r in result.case_results] == [c.case_id for c in definition.cases]
    bad = run(definition, FakeProvider([outcome("wrong"), outcome("not a number"), outcome("{")]))
    assert bad.completed_cases == 3 and bad.execution_failed_cases == 0
    assert [r.evaluation.reason for r in bad.case_results] == [
        "mismatch",
        "invalid_number",
        "invalid_json",
    ]


def test_clock_and_id_injection_and_naive_rejection():
    runner = BenchmarkRunner(
        clock=lambda: NOW.astimezone(timezone(timedelta(hours=8))), id_factory=lambda: "fixed-run"
    )
    result = asyncio.run(runner.run(suite(), TARGET, FakeProvider([outcome()])))
    assert result.run_id == "fixed-run" and result.started_at.tzinfo == UTC
    provider = FakeProvider([])
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            BenchmarkRunner(clock=lambda: NOW.replace(tzinfo=None)).run(
                suite(),
                TARGET,
                provider,
            )
        )
    assert not provider.calls


@pytest.mark.parametrize(
    "update",
    [
        {"completed_cases": 0},
        {"execution_failed_cases": 1},
        {"total_cases": 2},
        {"terminated_early": True},
        {"status": "completed_with_failures"},
        {"finished_at": NOW - timedelta(seconds=1)},
        {"started_at": NOW.replace(tzinfo=None)},
        {"case_results": []},
        {"provider": "other"},
    ],
)
def test_run_invariants(update):
    fields = run(suite(), FakeProvider([outcome()])).model_dump()
    with pytest.raises(ValidationError):
        BenchmarkRun.model_validate({**fields, **update})


def test_production_database_api_and_open_circuit_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "production.db"
    store = SQLiteMetricsStore(path)
    store.record_attempt(make_attempt("production-request", finished_at=NOW - timedelta(minutes=1)))
    store.record_routing_decision(make_decision("production-request", created_at=NOW))
    store.upsert_health(opened())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("modelpilot.metrics.api.datetime", FixedDatetime)

    def snapshot():
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute(
                    "SELECT version FROM schema_version WHERE singleton = 1"
                ).fetchone()[0]
                == SCHEMA_VERSION
                == 4
            )
            return tuple(connection.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("benchmark accessed production control plane")

    before = snapshot()
    with make_client(store) as client:
        response = client.get("/v1/metrics/summary")
        assert response.status_code == 200
        summary = response.json()
        with monkeypatch.context() as guards:
            guards.setattr(sqlite3, "connect", forbidden)
            for name in (
                "record_attempt",
                "record_routing_decision",
                "upsert_health",
                "get_health",
            ):
                guards.setattr(SQLiteMetricsStore, name, forbidden)
            guards.setattr("modelpilot.router.ModelRouter.rank_with_explanation", forbidden)
            guards.setattr("modelpilot.service.GatewayService.complete", forbidden)
            guards.setattr("modelpilot.health.manager.ProviderHealthManager.update", forbidden)
            result = run(suite(2), FakeProvider([outcome(), outcome(error="timeout")]))
            assert result.completed_cases == result.execution_failed_cases == 1
        assert client.get("/v1/metrics/summary").json() == summary
    assert snapshot() == before
    reopened = SQLiteMetricsStore(path)
    assert reopened.get_health(TARGET.provider, TARGET.model) == store.get_health(
        TARGET.provider,
        TARGET.model,
    )
    assert snapshot() == before
