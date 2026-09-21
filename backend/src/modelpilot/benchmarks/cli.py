"""Explicit, bounded benchmark execution: no gateway, routing or circuit breaker."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

import httpx

from modelpilot.benchmarks.loader import load_benchmark_suite
from modelpilot.benchmarks.models import BenchmarkSuite
from modelpilot.benchmarks.results import BenchmarkRun, BenchmarkRunConfig, BenchmarkTarget
from modelpilot.benchmarks.runner import BenchmarkRunner
from modelpilot.benchmarks.sqlite_store import SQLiteBenchmarkStore
from modelpilot.config import Settings
from modelpilot.providers.base import ProviderErrorType, sanitize_error_message
from modelpilot.providers.factory import create_provider


class _ConfigurationError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default can echo arbitrary invalid arguments, including credentials.
        raise _ConfigurationError("invalid command arguments; use --help")


def _parser() -> argparse.ArgumentParser:
    defaults = BenchmarkRunConfig()
    parser = _Parser(prog="python -m modelpilot.benchmarks.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run and save one explicit benchmark target")
    run.add_argument("--suite", required=True, help="explicit local suite JSON file")
    run.add_argument("--provider", required=True, choices=("openai", "gemini", "deepseek"))
    run.add_argument("--model", required=True, help="explicit model identifier; auto is forbidden")
    run.add_argument("--max-cases", type=int, default=defaults.max_cases)
    run.add_argument("--timeout", type=float, default=defaults.case_timeout_seconds)
    run.add_argument("--temperature", type=float, default=defaults.temperature)
    run.add_argument("--json", action="store_true", help="print a machine-readable safe summary")
    return parser


async def _execute(
    suite: BenchmarkSuite,
    target: BenchmarkTarget,
    config: BenchmarkRunConfig,
    settings: Settings,
) -> BenchmarkRun:
    async with httpx.AsyncClient(timeout=config.case_timeout_seconds) as client:
        provider = create_provider(target.provider, settings, client)
        if not provider.configured:
            raise _ConfigurationError("selected provider is not configured")
        # Fail before spending provider API calls when the store cannot be initialized.
        settings.metrics_db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteBenchmarkStore(settings.metrics_db_path)
        run = await BenchmarkRunner().run(suite, target, provider, config)
        store.save_run(run)
        return run


def _print_summary(run: BenchmarkRun, settings: Settings, *, as_json: bool) -> None:
    secrets = tuple(
        value
        for value in (
            settings.openai_api_key,
            settings.gemini_api_key,
            settings.deepseek_api_key,
        )
        if value
    )
    summary = {
        "run_id": run.run_id,
        "suite_id": run.suite_id,
        "suite_version": run.suite_version,
        "provider": run.provider,
        "model": run.model,
        "status": run.status,
        "total_cases": run.total_cases,
        "completed_cases": run.completed_cases,
        "execution_failed_cases": run.execution_failed_cases,
        "terminated_early": run.terminated_early,
        "authentication_error": _authentication_failed(run),
        "saved": True,
    }
    safe = {
        key: sanitize_error_message(value, secrets) if isinstance(value, str) else value
        for key, value in summary.items()
    }
    if as_json:
        print(json.dumps(safe))
        return
    print("ModelPilot Benchmark")
    print(f"Suite: {safe['suite_id']} / {safe['suite_version']}")
    print(f"Target: {safe['provider']} / {safe['model']}")
    print(f"Run: {safe['run_id']}")
    print(
        f"Cases: {run.total_cases} total, {run.completed_cases} evaluated, "
        f"{run.execution_failed_cases} provider failures"
    )
    print(f"Status: {safe['status']}")
    print(f"Terminated early: {'yes' if run.terminated_early else 'no'}")
    if _authentication_failed(run):
        print("Error: authentication_error")
    print("Saved: yes")


def _authentication_failed(run: BenchmarkRun) -> bool:
    return any(
        case.error_type == ProviderErrorType.AUTHENTICATION_ERROR for case in run.case_results
    )


def _error(message: str, exit_code: int) -> int:
    # Only stable messages are emitted: never exception text, local paths or raw inputs.
    print(sanitize_error_message(f"benchmark: {message}"), file=sys.stderr)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        target = BenchmarkTarget(provider=args.provider, model=args.model)
        config = BenchmarkRunConfig(
            max_cases=args.max_cases,
            case_timeout_seconds=args.timeout,
            temperature=args.temperature,
        )
        suite = load_benchmark_suite(args.suite)
        if len(suite.cases) > config.max_cases:
            raise _ConfigurationError("suite exceeds max_cases")
        settings = Settings.from_env()
    except _ConfigurationError as error:
        return _error(str(error), 2)
    except (ValueError, OSError):
        return _error("invalid suite, target or configuration", 2)
    except Exception:
        return _error("benchmark setup failed", 4)
    try:
        run = asyncio.run(_execute(suite, target, config, settings))
    except _ConfigurationError as error:
        return _error(str(error), 2)
    except Exception:
        return _error("execution or persistence failed; run was not confirmed saved", 4)
    _print_summary(run, settings, as_json=args.json)
    return 3 if _authentication_failed(run) else 0


if __name__ == "__main__":
    raise SystemExit(main())
