import asyncio
from collections.abc import Callable
from datetime import UTC
from typing import Any

import httpx
import pytest

from modelpilot.logging import RequestLogStore
from modelpilot.providers import (
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
    ProviderErrorType,
    ProviderOutcome,
)
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import GatewayService

API_KEY = "test-provider-key"
MODEL = "test-model"
REQUEST = ChatCompletionRequest(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
)
Handler = Callable[[httpx.Request], httpx.Response]
ProviderClass = type[OpenAIProvider] | type[DeepSeekProvider] | type[GeminiProvider]


def openai_payload(*, usage: dict[str, int] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def gemini_payload(*, usage: dict[str, int] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
    }
    if usage is not None:
        payload["usageMetadata"] = usage
    return payload


def complete_with(
    provider_class: ProviderClass,
    handler: Handler,
) -> ProviderOutcome:
    async def invoke() -> ProviderOutcome:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = provider_class(API_KEY, "https://provider.test/v1", client)
            return await provider.complete(REQUEST, MODEL)

    return asyncio.run(invoke())


def assert_failure(
    outcome: ProviderOutcome,
    error_type: ProviderErrorType,
    status_code: int | None,
) -> None:
    assert outcome.success is False
    assert outcome.response is None
    assert outcome.error_type is error_type
    assert outcome.status_code == status_code
    assert outcome.input_tokens is None
    assert outcome.output_tokens is None
    assert outcome.total_tokens is None


def test_openai_success_normalizes_usage() -> None:
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(200, json=openai_payload(usage=usage)),
    )

    assert outcome.success is True
    assert outcome.provider == "openai"
    assert outcome.input_tokens == 11
    assert outcome.output_tokens == 7
    assert outcome.total_tokens == 18
    assert outcome.status_code == 200
    assert outcome.response is not None
    assert outcome.response["usage"] == usage


def test_openai_success_preserves_missing_usage() -> None:
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(200, json=openai_payload()),
    )

    assert outcome.success is True
    assert outcome.input_tokens is None
    assert outcome.output_tokens is None
    assert outcome.total_tokens is None


def test_openai_timeout_is_normalized() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    assert_failure(
        complete_with(OpenAIProvider, timeout),
        ProviderErrorType.TIMEOUT,
        None,
    )


def test_openai_connection_failure_is_normalized() -> None:
    def connection_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert_failure(
        complete_with(OpenAIProvider, connection_error),
        ProviderErrorType.CONNECTION_ERROR,
        None,
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_openai_authentication_error_is_normalized(status_code: int) -> None:
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(status_code, json={"error": "auth"}),
    )

    assert_failure(outcome, ProviderErrorType.AUTHENTICATION_ERROR, status_code)


def test_openai_rate_limit_is_normalized() -> None:
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(429, json={"error": "rate limit"}),
    )

    assert_failure(outcome, ProviderErrorType.RATE_LIMIT, 429)


def test_openai_server_error_is_normalized() -> None:
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(503, json={"error": "unavailable"}),
    )

    assert_failure(outcome, ProviderErrorType.PROVIDER_ERROR, 503)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"unexpected": "shape"}),
    ],
)
def test_openai_invalid_response_is_normalized(response: httpx.Response) -> None:
    outcome = complete_with(OpenAIProvider, lambda request: response)

    assert_failure(outcome, ProviderErrorType.INVALID_RESPONSE, 200)


def test_deepseek_success_uses_shared_openai_compatible_usage() -> None:
    outcome = complete_with(
        DeepSeekProvider,
        lambda request: httpx.Response(
            200,
            json=openai_payload(
                usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}
            ),
        ),
    )

    assert outcome.success is True
    assert outcome.provider == "deepseek"
    assert (outcome.input_tokens, outcome.output_tokens, outcome.total_tokens) == (20, 8, 28)


def test_deepseek_error_uses_shared_error_mapping() -> None:
    outcome = complete_with(
        DeepSeekProvider,
        lambda request: httpx.Response(429, json={"error": "rate limit"}),
    )

    assert_failure(outcome, ProviderErrorType.RATE_LIMIT, 429)


def test_gemini_success_normalizes_usage_metadata() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(
            200,
            json=gemini_payload(
                usage={
                    "promptTokenCount": 13,
                    "candidatesTokenCount": 9,
                    "totalTokenCount": 22,
                }
            ),
        ),
    )

    assert outcome.success is True
    assert outcome.provider == "gemini"
    assert (outcome.input_tokens, outcome.output_tokens, outcome.total_tokens) == (13, 9, 22)
    assert outcome.response is not None
    assert outcome.response["usage"] == {
        "prompt_tokens": 13,
        "completion_tokens": 9,
        "total_tokens": 22,
    }


def test_gemini_success_preserves_missing_usage() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(200, json=gemini_payload()),
    )

    assert outcome.success is True
    assert outcome.input_tokens is None
    assert outcome.output_tokens is None
    assert outcome.total_tokens is None
    assert outcome.response is not None
    assert "usage" not in outcome.response


def test_gemini_partial_usage_preserves_missing_fields() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(
            200,
            json=gemini_payload(usage={"promptTokenCount": 5, "totalTokenCount": 5}),
        ),
    )

    assert outcome.input_tokens == 5
    assert outcome.output_tokens is None
    assert outcome.total_tokens == 5


def test_gemini_authentication_error_is_normalized() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(401, json={"error": "auth"}),
    )

    assert_failure(outcome, ProviderErrorType.AUTHENTICATION_ERROR, 401)


def test_gemini_rate_limit_is_normalized() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(429, json={"error": "rate limit"}),
    )

    assert_failure(outcome, ProviderErrorType.RATE_LIMIT, 429)


def test_gemini_invalid_response_is_normalized() -> None:
    outcome = complete_with(
        GeminiProvider,
        lambda request: httpx.Response(200, json={"candidates": []}),
    )

    assert_failure(outcome, ProviderErrorType.INVALID_RESPONSE, 200)


def test_outcome_uses_measured_latency_and_aware_utc_timestamps() -> None:
    outcome = complete_with(
        OpenAIProvider,
        lambda request: httpx.Response(200, json=openai_payload()),
    )

    assert outcome.latency_ms >= 0
    assert outcome.started_at.tzinfo is not None
    assert outcome.finished_at.tzinfo is not None
    assert outcome.started_at.utcoffset() == outcome.finished_at.utcoffset() == UTC.utcoffset(None)
    assert outcome.finished_at >= outcome.started_at


def test_error_message_redacts_credentials_and_headers() -> None:
    def leaks_secret(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Authorization: Bearer {API_KEY}; api_key={API_KEY}; token={API_KEY}",
            request=request,
        )

    outcome = complete_with(OpenAIProvider, leaks_secret)

    assert outcome.error_message is not None
    assert API_KEY not in outcome.error_message
    assert "Bearer" not in outcome.error_message
    assert "Authorization" not in outcome.error_message


def test_service_returns_openai_compatible_response_from_outcome() -> None:
    async def invoke() -> dict[str, Any]:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=openai_payload())
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAIProvider(API_KEY, "https://provider.test/v1", client)
            gateway = GatewayService(
                ModelRouter(
                    {"openai": provider},
                    [ModelCandidate("openai", MODEL, 1, 1, 1, 1)],
                ),
                RequestLogStore(),
            )
            return await gateway.complete(REQUEST)

    result = asyncio.run(invoke())

    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "ok"
    assert result["modelpilot"]["provider"] == "openai"
