import time
import uuid
from typing import Any

import httpx

from modelpilot.providers.base import (
    InvalidProviderResponse,
    Provider,
    ProviderOutcome,
    normalize_token_usage,
)
from modelpilot.schemas import ChatCompletionRequest


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

    async def complete(self, request: ChatCompletionRequest, model: str) -> ProviderOutcome:
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

        started_at, monotonic_started_at = self.start_call()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/models/{model}:generateContent",
                headers={"x-goog-api-key": self.api_key or ""},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            if not isinstance(text, str):
                raise InvalidProviderResponse("candidate text must be a string")
            usage = _gemini_usage(data.get("usageMetadata"))
            input_tokens, output_tokens, total_tokens = normalize_token_usage(usage)
            normalized_response = {
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
            }
            if usage is not None:
                normalized_response["usage"] = usage
            return self.successful_outcome(
                model=model,
                response=normalized_response,
                started_at=started_at,
                monotonic_started_at=monotonic_started_at,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                status_code=response.status_code,
            )
        except Exception as exc:
            return self.failed_outcome(
                model=model,
                error=exc,
                started_at=started_at,
                monotonic_started_at=monotonic_started_at,
                status_code=response.status_code if response is not None else None,
            )


def _gemini_usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise InvalidProviderResponse("usageMetadata must be an object when present")
    field_names = {
        "promptTokenCount": "prompt_tokens",
        "candidatesTokenCount": "completion_tokens",
        "totalTokenCount": "total_tokens",
    }
    normalized = {
        target: usage[source]
        for source, target in field_names.items()
        if source in usage and usage[source] is not None
    }
    return normalized or None
