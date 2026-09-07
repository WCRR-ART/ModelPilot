import asyncio

from modelpilot.logging import RequestLogStore
from modelpilot.providers.base import Provider, ProviderError
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import GatewayService


class FakeProvider(Provider):
    def __init__(self, name: str, fails: bool = False) -> None:
        super().__init__("test-key")
        self.name = name
        self.fails = fails

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        if self.fails:
            raise ProviderError("simulated failure")
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [],
        }


def test_gateway_falls_back_and_records_attempts() -> None:
    first = FakeProvider("first", fails=True)
    second = FakeProvider("second")
    router = ModelRouter(
        {"first": first, "second": second},
        [
            ModelCandidate("first", "primary", 1, 1, 1, 1),
            ModelCandidate("second", "fallback", 0.8, 0.8, 0.8, 0.8),
        ],
    )
    logs = RequestLogStore()
    gateway = GatewayService(router, logs)
    request = ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "hello"}])

    result = asyncio.run(gateway.complete(request))

    assert result["model"] == "fallback"
    assert result["modelpilot"]["provider"] == "second"
    assert result["modelpilot"]["attempts"] == 2
    record = logs.list()[0]
    assert record.success is True
    assert [attempt.success for attempt in record.attempts] == [False, True]
