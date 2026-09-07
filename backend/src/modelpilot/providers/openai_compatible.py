from typing import Any

import httpx

from modelpilot.providers.base import (
    InvalidProviderResponse,
    Provider,
    ProviderOutcome,
    normalize_token_usage,
)
from modelpilot.schemas import ChatCompletionRequest


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

    async def complete(self, request: ChatCompletionRequest, model: str) -> ProviderOutcome:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        started_at, monotonic_started_at = self.start_call()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                raise InvalidProviderResponse("expected an OpenAI-compatible completion object")
            usage = data.get("usage")
            input_tokens, output_tokens, total_tokens = normalize_token_usage(usage)
            return self.successful_outcome(
                model=model,
                response=data,
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
