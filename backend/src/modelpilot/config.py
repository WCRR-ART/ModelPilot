import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    cors_origins: list[str] = ["http://localhost:3000"]
    request_log_limit: int = Field(default=500, ge=1, le=10_000)
    metrics_db_path: Path = Path("./data/modelpilot.db")
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_cooldown_seconds: int = Field(default=60, ge=0)

    openai_api_key: str | None = Field(default=None, exclude=True, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    gemini_api_key: str | None = Field(default=None, exclude=True, repr=False)
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.0-flash"

    deepseek_api_key: str | None = Field(default=None, exclude=True, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    @classmethod
    def from_env(cls) -> "Settings":
        origins = os.getenv("MODELPILOT_CORS_ORIGINS", "http://localhost:3000")
        return cls(
            cors_origins=[origin.strip() for origin in origins.split(",") if origin.strip()],
            request_log_limit=int(os.getenv("MODELPILOT_REQUEST_LOG_LIMIT", "500")),
            circuit_failure_threshold=int(os.getenv("MODELPILOT_CIRCUIT_FAILURE_THRESHOLD", "3")),
            circuit_cooldown_seconds=int(os.getenv("MODELPILOT_CIRCUIT_COOLDOWN_SECONDS", "60")),
            metrics_db_path=Path(
                os.getenv("MODELPILOT_METRICS_DB", "./data/modelpilot.db")
            ),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_base_url=os.getenv(
                "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
            ),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )
