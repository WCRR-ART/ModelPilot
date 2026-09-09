from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from modelpilot import __version__
from modelpilot.config import Settings
from modelpilot.logging import RequestLogStore
from modelpilot.metrics import MetricsStore, SQLiteMetricsStore
from modelpilot.metrics.api import router as metrics_router
from modelpilot.providers import DeepSeekProvider, GeminiProvider, OpenAIProvider
from modelpilot.router import ModelRouter, default_candidates
from modelpilot.schemas import ChatCompletionPayload, ChatCompletionRequest, RequestLog
from modelpilot.service import AllProvidersFailed, GatewayService, NoProviderAvailable


def build_metrics_store(settings: Settings) -> SQLiteMetricsStore:
    database_path = Path(settings.metrics_db_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteMetricsStore(database_path)


def build_gateway(
    settings: Settings,
    client: httpx.AsyncClient,
    metrics: MetricsStore | None = None,
) -> GatewayService:
    providers = {
        "openai": OpenAIProvider(settings.openai_api_key, settings.openai_base_url, client),
        "gemini": GeminiProvider(settings.gemini_api_key, settings.gemini_base_url, client),
        "deepseek": DeepSeekProvider(settings.deepseek_api_key, settings.deepseek_base_url, client),
    }
    metrics_store = metrics if metrics is not None else build_metrics_store(settings)
    router = ModelRouter(
        providers,
        default_candidates(
            settings.openai_model,
            settings.gemini_model,
            settings.deepseek_model,
        ),
        metrics_store,
    )
    return GatewayService(
        router,
        RequestLogStore(settings.request_log_limit),
        metrics_store,
    )


def create_app(
    settings: Settings | None = None,
    gateway: GatewayService | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if gateway is not None:
            app.state.gateway = gateway
            yield
            return
        async with httpx.AsyncClient(timeout=30) as client:
            app.state.gateway = build_gateway(app_settings, client)
            yield

    app = FastAPI(
        title="ModelPilot",
        description="Intelligent LLM gateway",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.include_router(metrics_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        payload: ChatCompletionRequest,
        request: Request,
    ) -> ChatCompletionPayload:
        if payload.stream:
            raise HTTPException(status_code=400, detail="streaming is not supported in V0.2")
        service: GatewayService = request.app.state.gateway
        try:
            return await service.complete(payload)
        except NoProviderAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AllProvidersFailed as exc:
            raise HTTPException(
                status_code=502,
                detail={"message": "all providers failed", "request_id": exc.request_id},
            ) from exc

    @app.get("/v1/logs")
    async def request_logs(request: Request) -> list[RequestLog]:
        service: GatewayService = request.app.state.gateway
        return service.logs.list()

    return app


app = create_app()
