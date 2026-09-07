import time
import uuid
from typing import Any

import httpx

from modelpilot.providers.base import Provider, ProviderError
from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest


class GeminiProvider(Provider):
    name = "gemini"

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
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append({"text": message.content})
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})

        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        generation_config: dict[str, Any] = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config

        try:
            response = await self.client.post(
                f"{self.base_url}/models/{model}:generateContent",
                headers={"x-goog-api-key": self.api_key or ""},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"gemini request failed: {exc}") from exc

        usage = data.get("usageMetadata", {})
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
        }
