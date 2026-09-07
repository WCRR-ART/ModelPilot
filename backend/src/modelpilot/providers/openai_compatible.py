from typing import Any

import httpx

from modelpilot.providers.base import Provider, ProviderError
from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest


class OpenAICompatibleProvider(Provider):
    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        client: httpx.AsyncClient,
    ) -> None:
        super().__init__(api_key)
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def complete(self, request: ChatCompletionRequest, model: str) -> ChatCompletionPayload:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"{self.name} request failed: {exc}") from exc
