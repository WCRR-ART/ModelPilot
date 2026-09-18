"""Explicit sequential provider execution, without production control-plane wiring."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from uuid import uuid4

from modelpilot.benchmarks.evaluators import evaluate_case
from modelpilot.benchmarks.models import BenchmarkCase, BenchmarkSuite
from modelpilot.benchmarks.results import (
    BenchmarkCaseResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkTarget,
)
from modelpilot.providers.base import InvalidProviderResponse, Provider, ProviderErrorType
from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest, ChatMessage


def extract_assistant_text(response: ChatCompletionPayload) -> str:
    """Accept only the common normalized text shape, not provider-specific payloads."""
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    raise InvalidProviderResponse("benchmark requires assistant text")


class BenchmarkRunner:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.clock = clock
        self.id_factory = id_factory

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("benchmark clock must be timezone-aware")
        return now.astimezone(UTC)

    async def run(
        self,
        suite: BenchmarkSuite,
        target: BenchmarkTarget,
        provider: Provider,
        config: BenchmarkRunConfig | None = None,
    ) -> BenchmarkRun:
        config = config or BenchmarkRunConfig()
        if provider.name != target.provider:
            raise ValueError("provider does not match benchmark target")
        if len(suite.cases) > config.max_cases:
            raise ValueError("suite exceeds max_cases")
        started_at = self._now()
        run_id = self.id_factory()
        results: list[BenchmarkCaseResult] = []
        for case in suite.cases:
            result = await self._run_case(case, target, provider, config)
            results.append(result)
            if result.error_type == ProviderErrorType.AUTHENTICATION_ERROR:
                break
        failures = sum(r.execution_status == "provider_failed" for r in results)
        return BenchmarkRun(
            run_id=run_id,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_fingerprint=sha256(suite.model_dump_json().encode("utf-8")).hexdigest(),
            provider=target.provider,
            model=target.model,
            config=config,
            started_at=started_at,
            finished_at=self._now(),
            status="completed_with_failures" if failures else "completed",
            total_cases=len(suite.cases),
            completed_cases=len(results) - failures,
            execution_failed_cases=failures,
            terminated_early=len(results) < len(suite.cases),
            case_results=tuple(results),
        )

    async def _run_case(
        self,
        case: BenchmarkCase,
        target: BenchmarkTarget,
        provider: Provider,
        config: BenchmarkRunConfig,
    ) -> BenchmarkCaseResult:
        request = ChatCompletionRequest(
            model=target.model,
            messages=[ChatMessage(role=m.role, content=m.content) for m in case.messages],
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        started = perf_counter()
        timeout = asyncio.timeout(config.case_timeout_seconds)
        try:
            async with timeout:
                outcome = await provider.complete(request, target.model)
        except TimeoutError:
            if not timeout.expired():
                raise  # An adapter invariant error, not the runner's deadline.
            return BenchmarkCaseResult(
                case_id=case.case_id,
                category=case.category,
                provider=target.provider,
                model=target.model,
                execution_status="provider_failed",
                error_type=ProviderErrorType.TIMEOUT,
                latency_ms=max(0.0, (perf_counter() - started) * 1000),
            )
        if (outcome.provider, outcome.model) != (target.provider, target.model):
            raise ValueError("outcome does not match benchmark target")
        error = outcome.error_type
        evaluation = None
        if outcome.success:
            assert outcome.response is not None
            try:
                text = extract_assistant_text(outcome.response)
            except InvalidProviderResponse:
                error = ProviderErrorType.INVALID_RESPONSE
            else:
                evaluation = evaluate_case(case.evaluator, text)
        return BenchmarkCaseResult(
            case_id=case.case_id,
            category=case.category,
            provider=target.provider,
            model=target.model,
            execution_status="provider_failed" if error else "completed",
            latency_ms=outcome.latency_ms,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            total_tokens=outcome.total_tokens,
            error_type=error,
            evaluation=evaluation,
        )
