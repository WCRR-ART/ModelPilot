from abc import ABC, abstractmethod

from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest


class ProviderError(RuntimeError):
    """A provider request failed and may be retried through fallback."""


class Provider(ABC):
    name: str

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest, model: str) -> ChatCompletionPayload:
        raise NotImplementedError
