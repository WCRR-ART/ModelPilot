from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from re import IGNORECASE
from re import compile as compile_pattern
from time import perf_counter
from typing import Any

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest


class ProviderError(RuntimeError):
    """A provider request failed and may be retried through fallback."""


class ProviderErrorType(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTHENTICATION_ERROR = "authentication_error"
    RATE_LIMIT = "rate_limit"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN_ERROR = "unknown_error"


class InvalidProviderResponse(ValueError):
    """A provider response cannot be normalized safely."""


class ProviderOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    success: bool
    response: ChatCompletionPayload | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error_type: ProviderErrorType | None = None
    error_message: str | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    latency_ms: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_datetime_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("error_message")
    @classmethod
    def sanitize_message(cls, value: str | None) -> str | None:
        return sanitize_error_message(value) if value is not None else None

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> "ProviderOutcome":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.success:
            if self.response is None:
                raise ValueError("successful outcomes require a response")
            if self.error_type is not None:
                raise ValueError("successful outcomes cannot contain an error type")
        else:
            if self.response is not None:
                raise ValueError("failed outcomes cannot contain a response")
            if self.error_type is None:
                raise ValueError("failed outcomes require an error type")
        return self


_SENSITIVE_PATTERNS = (
    compile_pattern(r"\bBearer\s+[^\s,;]+", IGNORECASE),
    compile_pattern(r"\bAuthorization\s*[:=]\s*[^\r\n,;]+", IGNORECASE),
    compile_pattern(r"\b(?:api[_ -]?key|token|secret)\s*[:=]\s*[^\s,;]+", IGNORECASE),
)


def sanitize_error_message(message: str, secrets: tuple[str, ...] = ()) -> str:
    sanitized = message
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    for pattern in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def classify_provider_error(error: Exception) -> tuple[ProviderErrorType, int | None]:
    if isinstance(error, httpx.TimeoutException):
        return ProviderErrorType.TIMEOUT, None
    if isinstance(error, httpx.ConnectError):
        return ProviderErrorType.CONNECTION_ERROR, None
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in {401, 403}:
            return ProviderErrorType.AUTHENTICATION_ERROR, status_code
        if status_code == 429:
            return ProviderErrorType.RATE_LIMIT, status_code
        return ProviderErrorType.PROVIDER_ERROR, status_code
    if isinstance(error, (InvalidProviderResponse, KeyError, IndexError, TypeError, ValueError)):
        return ProviderErrorType.INVALID_RESPONSE, None
    if isinstance(error, httpx.HTTPError):
        return ProviderErrorType.PROVIDER_ERROR, None
    return ProviderErrorType.UNKNOWN_ERROR, None


def normalize_token_usage(usage: Any) -> tuple[int | None, int | None, int | None]:
    if usage is None:
        return None, None, None
    if not isinstance(usage, dict):
        raise InvalidProviderResponse("usage must be an object when present")
    return (
        _optional_token_count(usage, "prompt_tokens"),
        _optional_token_count(usage, "completion_tokens"),
        _optional_token_count(usage, "total_tokens"),
    )


def _optional_token_count(usage: dict[str, Any], field: str) -> int | None:
    value = usage.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidProviderResponse(f"{field} must be a non-negative integer")
    return value


class Provider(ABC):
    name: str

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest, model: str) -> ProviderOutcome:
        raise NotImplementedError

    @staticmethod
    def start_call() -> tuple[datetime, float]:
        return datetime.now(UTC), perf_counter()

    def successful_outcome(
        self,
        *,
        model: str,
        response: ChatCompletionPayload,
        started_at: datetime,
        monotonic_started_at: float,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        status_code: int | None,
    ) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            model=model,
            success=True,
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            status_code=status_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            latency_ms=max((perf_counter() - monotonic_started_at) * 1000, 0),
        )

    def failed_outcome(
        self,
        *,
        model: str,
        error: Exception,
        started_at: datetime,
        monotonic_started_at: float,
        status_code: int | None = None,
    ) -> ProviderOutcome:
        error_type, error_status_code = classify_provider_error(error)
        return ProviderOutcome(
            provider=self.name,
            model=model,
            success=False,
            error_type=error_type,
            error_message=sanitize_error_message(
                f"{self.name} request failed: {error}",
                (self.api_key or "",),
            ),
            status_code=error_status_code if error_status_code is not None else status_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            latency_ms=max((perf_counter() - monotonic_started_at) * 1000, 0),
        )
