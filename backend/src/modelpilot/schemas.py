from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None


class RoutingPreferences(BaseModel):
    quality: float = Field(default=0.25, ge=0)
    cost: float = Field(default=0.25, ge=0)
    latency: float = Field(default=0.25, ge=0)
    reliability: float = Field(default=0.25, ge=0)

    @model_validator(mode="after")
    def require_positive_total(self) -> "RoutingPreferences":
        if self.quality + self.cost + self.latency + self.reliability <= 0:
            raise ValueError("at least one routing preference must be positive")
        return self

    def normalized(self) -> dict[str, float]:
        values = self.model_dump()
        total = sum(values.values())
        return {name: value / total for name, value in values.items()}


class ModelPilotOptions(BaseModel):
    preferences: RoutingPreferences = Field(default_factory=RoutingPreferences)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "auto"
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    modelpilot: ModelPilotOptions = Field(default_factory=ModelPilotOptions)


class AttemptLog(BaseModel):
    provider: str
    model: str
    score: float
    latency_ms: float
    success: bool
    error: str | None = None


class RequestLog(BaseModel):
    request_id: str
    requested_model: str
    selected_provider: str | None = None
    selected_model: str | None = None
    success: bool
    total_latency_ms: float
    attempts: list[AttemptLog]
    created_at: str


ChatCompletionPayload = dict[str, Any]
