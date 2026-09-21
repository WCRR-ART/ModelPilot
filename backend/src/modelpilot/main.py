from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from modelpilot import __version__
from modelpilot.benchmarks import SQLiteBenchmarkStore, load_benchmark_suite
from modelpilot.benchmarks.api import router as benchmark_router
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.benchmarks.store import BenchmarkStore
from modelpilot.config import Settings
from modelpilot.health import ProviderHealthStore
from modelpilot.health.api import router as health_router
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.logging import RequestLogStore
from modelpilot.metrics import MetricsStore, SQLiteMetricsStore
from modelpilot.metrics.api import router as metrics_router
from modelpilot.providers.factory import create_provider
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
    health_store: ProviderHealthStore | None = None,
) -> GatewayService:
    # Explicit suite errors are configuration errors; fail once, before database setup.
    suite = (
        load_benchmark_suite(settings.quality_suite_path)
        if settings.quality_suite_path is not None else None
    )
    providers = {
        name: create_provider(name, settings, client) for name in ("openai", "gemini", "deepseek")
    }
    metrics_store = metrics if metrics is not None else build_metrics_store(settings)
    quality_source = (
        BenchmarkQualityResolver(suite, SQLiteBenchmarkStore(settings.metrics_db_path))
        if suite is not None else None
    )
    if health_store is None and isinstance(metrics_store, ProviderHealthStore):
        health_store = metrics_store
    health = (
        ProviderHealthManager(
            health_store,
            failure_threshold=settings.circuit_failure_threshold,
            cooldown=timedelta(seconds=settings.circuit_cooldown_seconds),
        )
        if health_store is not None else None
    )
    router = ModelRouter(
        providers,
        default_candidates(
            settings.openai_model,
            settings.gemini_model,
            settings.deepseek_model,
        ),
        metrics_store,
        health_store=health_store,
        quality_source=quality_source,
    )
    return GatewayService(
        router,
        RequestLogStore(settings.request_log_limit),
        metrics_store,
        health,
        probes=HalfOpenProbeCoordinator(),
    )


def create_app(
    settings: Settings | None = None,
    gateway: GatewayService | None = None,
    benchmark_store: BenchmarkStore | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()

    def configure_benchmarks(app: FastAPI, service: GatewayService) -> None:
        source = service.router.quality_source
        resolver = source if isinstance(source, BenchmarkQualityResolver) else None
        store = benchmark_store
        if store is None and resolver is not None:
            store = resolver.store
        if store is None and gateway is None:
            store = SQLiteBenchmarkStore(app_settings.metrics_db_path)
        if resolver is not None and store is not resolver.store:
            resolver = BenchmarkQualityResolver(resolver.suite, store, clock=resolver.clock)
        app.state.benchmark_quality = resolver
        app.state.benchmark_store = store

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if gateway is not None:
            app.state.gateway = gateway
            configure_benchmarks(app, gateway)
            yield
            return
        async with httpx.AsyncClient(timeout=30) as client:
            app.state.gateway = build_gateway(app_settings, client)
            configure_benchmarks(app, app.state.gateway)
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
    app.include_router(health_router)
    app.include_router(benchmark_router)

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
