"""Construct one named adapter using the shared production configuration."""

import httpx

from modelpilot.config import Settings
from modelpilot.providers import DeepSeekProvider, GeminiProvider, OpenAIProvider, Provider

_PROVIDER_TYPES = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "deepseek": DeepSeekProvider,
}


def create_provider(name: str, settings: Settings, client: httpx.AsyncClient) -> Provider:
    try:
        provider_type = _PROVIDER_TYPES[name]
    except KeyError:
        raise ValueError("unknown provider") from None
    return provider_type(
        getattr(settings, f"{name}_api_key"),
        getattr(settings, f"{name}_base_url"),
        client,
    )
