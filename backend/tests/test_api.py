from fastapi.testclient import TestClient

from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.main import create_app
from modelpilot.providers.base import Provider
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest
from modelpilot.service import GatewayService


class ApiProvider(Provider):
    name = "test"

    def __init__(self) -> None:
        super().__init__("test-key")

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        return {
            "id": "chatcmpl-api",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        }


def make_client() -> TestClient:
    provider = ApiProvider()
    gateway = GatewayService(
        ModelRouter(
            {"test": provider},
            [ModelCandidate("test", "test-model", 1, 1, 1, 1)],
        ),
        RequestLogStore(),
    )
    return TestClient(create_app(Settings(), gateway))


def test_health() -> None:
    with make_client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.3.0"}


def test_chat_completions_is_openai_compatible() -> None:
    with make_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["modelpilot"]["provider"] == "test"


def test_streaming_is_explicitly_rejected_in_v01() -> None:
    with make_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 400
