"""Local TEST DATA server: real app/stores/runner, exclusively fake providers.

The launcher supplies a fresh temporary directory. Nothing in this script loads
.env files, calls a cloud provider, or accepts a user database path.
"""

import argparse
import asyncio
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from modelpilot.benchmarks import SQLiteBenchmarkStore, load_benchmark_suite
from modelpilot.benchmarks.models import BenchmarkSuite
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.benchmarks.results import BenchmarkTarget
from modelpilot.benchmarks.runner import BenchmarkRunner
from modelpilot.config import Settings
from modelpilot.health.manager import ProviderHealthManager
from modelpilot.health.probes import HalfOpenProbeCoordinator
from modelpilot.logging import RequestLogStore
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers.base import Provider, ProviderErrorType, ProviderOutcome
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    ModelPilotOptions,
    RoutingPreferences,
)
from modelpilot.service import GatewayService


class AcceptanceProvider(Provider):
    """Synthetic arithmetic only; deliberately contains no HTTP client."""

    def __init__(self, name: str, *, mixed: bool = False) -> None:
        super().__init__(None)
        self.name = name
        self.mixed = mixed

    @property
    def configured(self) -> bool:
        return True

    async def complete(
        self, request: ChatCompletionRequest, model: str
    ) -> ProviderOutcome:
        now = datetime.now(UTC)
        match = re.fullmatch(
            r"TEST DATA synthetic arithmetic (\d+) \+ 1", request.messages[0].content
        )
        index = int(match[1]) if match else 0
        errors = {
            3: ProviderErrorType.TIMEOUT,
            4: ProviderErrorType.RATE_LIMIT,
            6: ProviderErrorType.AUTHENTICATION_ERROR,
        }
        error = errors.get(index) if self.mixed and match else None
        if error is not None:
            return ProviderOutcome(
                provider=self.name,
                model=model,
                success=False,
                error_type=error,
                started_at=now,
                finished_at=now,
                latency_ms=12.0,
            )
        answer = str(index + 1) if match else "TEST DATA acceptance reply"
        if self.mixed and index == 2:
            answer = "-1"
        return ProviderOutcome(
            provider=self.name,
            model=model,
            success=True,
            response={
                "id": "chatcmpl-test-data",
                "object": "chat.completion",
                "created": int(now.timestamp()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            started_at=now,
            finished_at=now,
            latency_ms=12.0,
        )


def definition(count: int, suite_id: str) -> BenchmarkSuite:
    return BenchmarkSuite.model_validate(
        {
            "suite_id": suite_id,
            "version": "1",
            "name": "TEST DATA synthetic arithmetic",
            "description": "Acceptance fixture only. NOT real model quality measurements.",
            "cases": [
                {
                    "case_id": f"test-case-{index:03d}",
                    "category": "math",
                    "messages": [
                        {
                            "role": "user",
                            "content": f"TEST DATA synthetic arithmetic {index} + 1",
                        }
                    ],
                    "evaluator": {
                        "kind": "exact_match",
                        "expected_text": str(index + 1),
                    },
                }
                for index in range(1, count + 1)
            ],
        }
    )


async def build_acceptance_app(directory: Path, frontend_port: int):
    # main's module-level app uses Settings.from_env. The isolated launcher/CI
    # supplies no secrets. Also discard relevant variable names without reading
    # their values when this standalone harness is invoked directly (e.g. CI).
    for key in tuple(os.environ):
        if key.startswith(("OPENAI_", "GEMINI_", "DEEPSEEK_", "MODELPILOT_")):
            del os.environ[key]
    from modelpilot.main import create_app

    database = directory / "test-data.sqlite3"
    suite_path = directory / "test-data-suite.json"
    if database.exists() or suite_path.exists():
        raise ValueError(
            "acceptance directory already contains test data; use a fresh directory"
        )
    suite_path.write_text(
        definition(60, "test-data-independent-60").model_dump_json(indent=2),
        encoding="utf-8",
    )
    suite = load_benchmark_suite(suite_path)
    store = SQLiteBenchmarkStore(database)
    providers = {
        "openai": AcceptanceProvider("openai"),
        "gemini": AcceptanceProvider("gemini", mixed=True),
    }
    for run_id, provider, model, run_suite in (
        ("test-data-full-openai", "openai", "test-model-a", suite),
        ("test-data-mixed-gemini", "gemini", "test-model-b", suite),
        (
            "test-data-historical-smoke",
            "openai",
            "test-model-a",
            definition(3, "test-data-smoke-3"),
        ),
    ):
        run = await BenchmarkRunner(id_factory=lambda name=run_id: name).run(
            run_suite,
            BenchmarkTarget(provider=provider, model=model),
            providers[provider],
        )
        store.save_run(run)
    metrics = SQLiteMetricsStore(database)
    gateway = GatewayService(
        ModelRouter(
            providers,
            [
                ModelCandidate("openai", "test-model-a", 0.6, 0.7, 0.7, 0.9),
                ModelCandidate("gemini", "test-model-b", 0.9, 0.7, 0.7, 0.9),
            ],
            metrics,
            health_store=metrics,
            quality_source=BenchmarkQualityResolver(suite, store),
        ),
        RequestLogStore(50),
        metrics,
        ProviderHealthManager(
            metrics, failure_threshold=3, cooldown=timedelta(seconds=60)
        ),
        probes=HalfOpenProbeCoordinator(),
    )
    # Benchmark records are separate. Only this ordinary fake chat creates
    # production attempts/health/decision records for the existing dashboard.
    await gateway.complete(
        ChatCompletionRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="TEST DATA normal chat")],
            modelpilot=ModelPilotOptions(
                preferences=RoutingPreferences(
                    quality=1, cost=0, latency=0, reliability=0
                )
            ),
        )
    )
    return create_app(
        settings=Settings(
            metrics_db_path=database,
            quality_suite_path=suite_path,
            cors_origins=[
                f"http://127.0.0.1:{frontend_port}",
                f"http://localhost:{frontend_port}",
            ],
        ),
        gateway=gateway,
        benchmark_store=store,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Fresh directory directly under the OS temporary directory",
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--frontend-port", type=int, default=3100)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if (
        directory.parent != Path(tempfile.gettempdir()).resolve()
        or not directory.is_dir()
    ):
        parser.error(
            "--directory must be an existing fresh child of the OS temporary directory"
        )
    if not (1 <= args.port <= 65535 and 1 <= args.frontend_port <= 65535):
        parser.error("ports must be between 1 and 65535")
    app = asyncio.run(build_acceptance_app(directory, args.frontend_port))
    print(f"TEST DATA ONLY. Fake providers; isolated SQLite: {directory}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
