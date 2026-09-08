import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import build_metrics_store
from modelpilot.metrics import AttemptRecord, SQLiteMetricsStore, attempt_from_outcome
from modelpilot.providers import Provider, ProviderErrorType, ProviderOutcome
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import AllProvidersFailed, GatewayService

NOW = datetime.now(UTC)
REQUEST = ChatCompletionRequest(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
)


class OutcomeProvider(Provider):
    def __init__(self, name: str, outcomes: list[ProviderOutcome]) -> None:
        super().__init__("test-key")
        self.name = name
        self.outcomes = outcomes
        self.calls = 0

    async def complete(self, request: ChatCompletionRequest, model: str) -> ProviderOutcome:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return outcome


class RecordingStore:
    def __init__(self) -> None:
        self.attempts: list[AttemptRecord] = []

    def record_attempt(self, attempt: AttemptRecord) -> None:
        self.attempts.append(attempt)


class FailingStore:
    def record_attempt(self, attempt: AttemptRecord) -> None:
        raise RuntimeError("Authorization: Bearer metrics-secret")


def make_outcome(
    provider: str,
    *,
    success: bool = True,
    input_tokens: int | None = 10,
    output_tokens: int | None = 5,
    total_tokens: int | None = 15,
    latency_ms: float = 125.5,
    error_type: ProviderErrorType | None = None,
) -> ProviderOutcome:
    return ProviderOutcome(
        provider=provider,
        model=f"{provider}-model",
        success=success,
        response=(
            {
                "id": f"chatcmpl-{provider}",
                "object": "chat.completion",
                "model": f"{provider}-model",
                "choices": [],
            }
            if success
            else None
        ),
        input_tokens=input_tokens if success else None,
        output_tokens=output_tokens if success else None,
        total_tokens=total_tokens if success else None,
        error_type=error_type,
        error_message="normalized provider failure" if not success else None,
        status_code=200 if success else 503,
        started_at=NOW - timedelta(milliseconds=latency_ms),
        finished_at=NOW,
        latency_ms=latency_ms,
    )


def make_gateway(
    providers: list[OutcomeProvider],
    metrics: Any,
) -> GatewayService:
    candidates = [
        ModelCandidate(provider.name, f"{provider.name}-model", score, score, score, score)
        for provider, score in zip(providers, range(len(providers), 0, -1), strict=True)
    ]
    return GatewayService(
        ModelRouter({provider.name: provider for provider in providers}, candidates),
        RequestLogStore(),
        metrics,
    )


def test_attempt_from_successful_outcome_preserves_measurements() -> None:
    outcome = make_outcome("openai")

    attempt = attempt_from_outcome(outcome, "req_shared", attempt_index=2)

    assert attempt.request_id == "req_shared"
    assert attempt.attempt_index == 2
    assert attempt.success is True
    assert attempt.input_tokens == 10
    assert attempt.output_tokens == 5
    assert attempt.total_tokens == 15
    assert attempt.latency_ms == 125.5
    assert attempt.error_type is None
    assert attempt.estimated_cost is None


def test_attempt_from_failed_outcome_preserves_error_type() -> None:
    outcome = make_outcome(
        "openai",
        success=False,
        error_type=ProviderErrorType.TIMEOUT,
    )

    attempt = attempt_from_outcome(outcome, "req_failed")

    assert attempt.success is False
    assert attempt.error_type == "timeout"
    assert attempt.input_tokens is None
    assert attempt.output_tokens is None
    assert attempt.total_tokens is None
    assert attempt.estimated_cost is None


def test_single_provider_success_records_one_attempt() -> None:
    provider = OutcomeProvider("openai", [make_outcome("openai")])
    metrics = RecordingStore()

    result = asyncio.run(make_gateway([provider], metrics).complete(REQUEST))

    assert result["model"] == "openai-model"
    assert len(metrics.attempts) == 1
    assert metrics.attempts[0].success is True


def test_missing_usage_remains_none_in_recorded_attempt() -> None:
    provider = OutcomeProvider(
        "openai",
        [make_outcome("openai", input_tokens=None, output_tokens=None, total_tokens=None)],
    )
    metrics = RecordingStore()

    asyncio.run(make_gateway([provider], metrics).complete(REQUEST))
    attempt = metrics.attempts[0]

    assert attempt.input_tokens is None
    assert attempt.output_tokens is None
    assert attempt.total_tokens is None
    assert attempt.estimated_cost is None


def test_one_failure_then_success_records_both_with_shared_request_id() -> None:
    first = OutcomeProvider(
        "openai",
        [make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)],
    )
    second = OutcomeProvider("gemini", [make_outcome("gemini")])
    metrics = RecordingStore()

    result = asyncio.run(make_gateway([first, second], metrics).complete(REQUEST))

    assert [attempt.success for attempt in metrics.attempts] == [False, True]
    assert [attempt.attempt_index for attempt in metrics.attempts] == [0, 1]
    assert len({attempt.request_id for attempt in metrics.attempts}) == 1
    assert result["modelpilot"]["request_id"] == metrics.attempts[0].request_id


def test_single_provider_failure_records_one_attempt() -> None:
    provider = OutcomeProvider(
        "openai",
        [make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)],
    )
    metrics = RecordingStore()

    with pytest.raises(AllProvidersFailed):
        asyncio.run(make_gateway([provider], metrics).complete(REQUEST))

    assert len(metrics.attempts) == 1
    assert metrics.attempts[0].success is False


def test_two_failures_then_success_records_three_attempts() -> None:
    providers = [
        OutcomeProvider(
            "openai",
            [make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)],
        ),
        OutcomeProvider(
            "gemini",
            [
                make_outcome(
                    "gemini", success=False, error_type=ProviderErrorType.RATE_LIMIT
                )
            ],
        ),
        OutcomeProvider("deepseek", [make_outcome("deepseek")]),
    ]
    metrics = RecordingStore()

    asyncio.run(make_gateway(providers, metrics).complete(REQUEST))

    assert len(metrics.attempts) == 3
    assert [attempt.success for attempt in metrics.attempts] == [False, False, True]
    assert len({attempt.request_id for attempt in metrics.attempts}) == 1


def test_all_provider_failures_are_recorded() -> None:
    providers = [
        OutcomeProvider(
            "openai",
            [make_outcome("openai", success=False, error_type=ProviderErrorType.TIMEOUT)],
        ),
        OutcomeProvider(
            "gemini",
            [
                make_outcome(
                    "gemini", success=False, error_type=ProviderErrorType.PROVIDER_ERROR
                )
            ],
        ),
    ]
    metrics = RecordingStore()

    with pytest.raises(AllProvidersFailed):
        asyncio.run(make_gateway(providers, metrics).complete(REQUEST))

    assert len(metrics.attempts) == 2
    assert all(not attempt.success for attempt in metrics.attempts)
    assert len({attempt.request_id for attempt in metrics.attempts}) == 1


def test_separate_requests_receive_distinct_request_ids() -> None:
    provider = OutcomeProvider(
        "openai",
        [make_outcome("openai"), make_outcome("openai")],
    )
    metrics = RecordingStore()
    gateway = make_gateway([provider], metrics)

    asyncio.run(gateway.complete(REQUEST))
    asyncio.run(gateway.complete(REQUEST))

    assert metrics.attempts[0].request_id != metrics.attempts[1].request_id


def test_metrics_failure_does_not_change_success_or_trigger_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = OutcomeProvider("openai", [make_outcome("openai")])
    second = OutcomeProvider("gemini", [make_outcome("gemini")])

    with caplog.at_level(logging.WARNING, logger="modelpilot.service"):
        result = asyncio.run(make_gateway([first, second], FailingStore()).complete(REQUEST))

    assert result["model"] == "openai-model"
    assert first.calls == 1
    assert second.calls == 0
    assert "failed to record provider attempt" in caplog.text
    assert "metrics-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text


def test_attempt_persists_across_sqlite_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "metrics.sqlite3"
    provider = OutcomeProvider("openai", [make_outcome("openai")])
    first_store = SQLiteMetricsStore(database_path)

    asyncio.run(make_gateway([provider], first_store).complete(REQUEST))
    reopened = SQLiteMetricsStore(database_path)
    attempts = reopened.get_recent_attempts(
        "openai", "openai-model", 10, NOW - timedelta(days=1)
    )

    assert len(attempts) == 1
    assert attempts[0].success is True


def test_default_metrics_store_creates_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "modelpilot.db"
    settings = Settings(metrics_db_path=database_path)

    store = build_metrics_store(settings)

    assert isinstance(store, SQLiteMetricsStore)
    assert database_path.exists()


def test_settings_reads_metrics_database_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("MODELPILOT_METRICS_DB", str(database_path))

    settings = Settings.from_env()

    assert settings.metrics_db_path == database_path
