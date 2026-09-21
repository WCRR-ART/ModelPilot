import asyncio
import json
import sqlite3
import subprocess
import sys

import httpx
import pytest
from test_benchmark_runner import TARGET, FakeProvider, outcome, suite
from test_health_aware_routing import opened
from test_metrics_api import make_attempt, make_decision

from modelpilot.benchmarks import cli
from modelpilot.benchmarks.resolver import BenchmarkQualityResolver
from modelpilot.benchmarks.sqlite_store import SQLiteBenchmarkStore
from modelpilot.config import Settings
from modelpilot.metrics import SQLiteMetricsStore
from modelpilot.providers import DeepSeekProvider, GeminiProvider, OpenAIProvider
from modelpilot.providers.factory import create_provider


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("CLI tests must never send real provider HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def prepare_cli(tmp_path, monkeypatch, *, count=1, outcomes=None):
    definition = suite(count)
    path = tmp_path / "suite.json"
    path.write_text(definition.model_dump_json(), encoding="utf-8")
    settings = Settings(
        metrics_db_path=tmp_path / "nested" / "benchmark.db",
        openai_api_key="fake-private-credential",
        openai_model="different-configured-model",
    )
    provider = FakeProvider(outcomes if outcomes is not None else [outcome()] * count)
    constructions = []

    def factory(name, passed_settings, client):
        constructions.append(name)
        assert passed_settings is settings
        assert isinstance(client, httpx.AsyncClient)
        return provider

    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "create_provider", factory)
    args = ["run", "--suite", str(path), "--provider", TARGET.provider, "--model", TARGET.model]
    return args, provider, settings, constructions


def saved_run(settings):
    runs = SQLiteBenchmarkStore(settings.metrics_db_path).list_runs()
    assert len(runs) == 1
    return runs[0]


def test_valid_run_explicit_target_saved_and_summary(tmp_path, monkeypatch, capsys):
    args, provider, settings, constructions = prepare_cli(tmp_path, monkeypatch, count=2)
    assert cli.main(args) == 0
    result = saved_run(settings)
    output = capsys.readouterr()
    assert output.err == ""
    assert result.run_id in output.out
    assert "2 total, 2 evaluated, 0 provider failures" in output.out
    assert "Saved: yes" in output.out
    assert len(provider.calls) == 2 and constructions == ["openai"]
    assert result.model == TARGET.model != settings.openai_model
    assert result.suite_id == "test" and result.suite_version == "1"
    assert all(call.model == TARGET.model for call in provider.calls)


def test_json_summary_no_prompt_output_or_credentials(tmp_path, monkeypatch, capsys):
    args, _, settings, _ = prepare_cli(
        tmp_path,
        monkeypatch,
        outcomes=[outcome("raw-completion-do-not-print")],
    )
    assert cli.main([*args, "--json"]) == 0
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["run_id"] == saved_run(settings).run_id
    assert summary["saved"] and summary["completed_cases"] == 1
    assert not summary["authentication_error"]
    for forbidden in (
        "Prompt",
        "Do not change",
        "PINE",
        "raw-completion",
        "fake-private",
        str(settings.metrics_db_path),
        "case_results",
        "messages",
        "Authorization",
    ):
        assert forbidden not in output.out + output.err


def test_wrong_answer_is_evaluated_and_saved(tmp_path, monkeypatch):
    args, _, settings, _ = prepare_cli(tmp_path, monkeypatch, outcomes=[outcome("wrong")])
    assert cli.main(args) == 0
    result = saved_run(settings)
    assert result.case_results[0].evaluation.score == 0
    assert result.completed_cases == 1 and result.execution_failed_cases == 0


@pytest.mark.parametrize(
    "error",
    [
        "timeout",
        "connection_error",
        "rate_limit",
        "provider_error",
        "invalid_response",
        "unknown_error",
    ],
)
def test_ordinary_failure_continues_and_saves(tmp_path, monkeypatch, error):
    args, provider, settings, constructions = prepare_cli(
        tmp_path,
        monkeypatch,
        count=2,
        outcomes=[outcome(error=error), outcome()],
    )
    assert cli.main(args) == 0
    result = saved_run(settings)
    assert result.status == "completed_with_failures"
    assert result.completed_cases == result.execution_failed_cases == 1
    assert result.case_results[0].error_type == error
    assert result.case_results[0].evaluation is None
    assert len(provider.calls) == 2 and constructions == ["openai"]


@pytest.mark.parametrize("count", [2, 3])
def test_authentication_failure_saves_partial_or_terminal_run(
    tmp_path,
    monkeypatch,
    capsys,
    count,
):
    args, provider, settings, constructions = prepare_cli(
        tmp_path,
        monkeypatch,
        count=count,
        outcomes=[outcome(), outcome(error="authentication_error")],
    )
    assert cli.main(args) == 3
    result = saved_run(settings)
    assert result.terminated_early == (count == 3)
    assert len(provider.calls) == 2 and constructions == ["openai"]
    output = capsys.readouterr().out
    assert "authentication_error" in output and "Saved: yes" in output
    assert f"Terminated early: {'yes' if count == 3 else 'no'}" in output


@pytest.mark.parametrize("option", ["--suite", "--provider", "--model"])
def test_requires_explicit_arguments(tmp_path, monkeypatch, option):
    args, provider, settings, constructions = prepare_cli(tmp_path, monkeypatch)
    index = args.index(option)
    del args[index : index + 2]
    assert cli.main(args) == 2
    assert not provider.calls and not constructions and not settings.metrics_db_path.exists()


@pytest.mark.parametrize(
    "option,value",
    [
        ("--provider", "auto"),
        ("--provider", "AUTO"),
        ("--model", "auto"),
        ("--model", " AUTO "),
        ("--model", ""),
        ("--provider", "unknown"),
    ],
)
def test_rejects_implicit_or_unknown_target(tmp_path, monkeypatch, option, value):
    args, provider, _, constructions = prepare_cli(tmp_path, monkeypatch)
    args[args.index(option) + 1] = value
    assert cli.main(args) == 2
    assert not provider.calls and not constructions


def test_run_config_reused_and_explicit_timeout_and_temperature(tmp_path, monkeypatch):
    args, provider, settings, _ = prepare_cli(tmp_path, monkeypatch, count=2)
    assert cli.main([*args, "--max-cases", "2", "--timeout", "3.5", "--temperature", "0.25"]) == 0
    result = saved_run(settings)
    assert result.config.max_cases == 2
    assert result.config.case_timeout_seconds == 3.5
    assert result.config.temperature == 0.25
    assert all(request.temperature == 0.25 for request in provider.calls)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-cases", "0"),
        ("--max-cases", "-1"),
        ("--max-cases", "1.5"),
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "nan"),
        ("--timeout", "inf"),
        ("--temperature", "-1"),
        ("--temperature", "3"),
    ],
)
def test_invalid_config_before_execution(tmp_path, monkeypatch, option, value):
    args, provider, settings, constructions = prepare_cli(tmp_path, monkeypatch)
    assert cli.main([*args, option, value]) == 2
    assert not provider.calls and not constructions and not settings.metrics_db_path.exists()


def test_suite_larger_than_max_cases_is_input_error(tmp_path, monkeypatch):
    args, provider, _, constructions = prepare_cli(tmp_path, monkeypatch, count=2)
    assert cli.main([*args, "--max-cases", "1"]) == 2
    assert not provider.calls and not constructions


def test_timeout_bounds_hung_provider_and_continues(tmp_path, monkeypatch):
    args, provider, settings, _ = prepare_cli(
        tmp_path,
        monkeypatch,
        count=2,
        outcomes=["hang", outcome()],
    )
    assert cli.main([*args, "--timeout", "0.01"]) == 0
    assert provider.cancelled and len(provider.calls) == 2
    result = saved_run(settings)
    assert result.case_results[0].error_type == "timeout" and result.completed_cases == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "json", "schema", "utf8"])
def test_invalid_suite_sanitized(tmp_path, monkeypatch, capsys, kind):
    args, provider, _, constructions = prepare_cli(tmp_path, monkeypatch)
    path = tmp_path / "secret-path.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "json":
        path.write_text("not-json raw-secret-input", encoding="utf-8")
    elif kind == "schema":
        path.write_text('{"messages":"raw-secret-input"}', encoding="utf-8")
    elif kind == "utf8":
        path.write_bytes(b"\xff")
    args[args.index("--suite") + 1] = str(path)
    assert cli.main(args) == 2
    output = capsys.readouterr()
    assert "secret-path" not in output.err and "raw-secret" not in output.err
    assert not provider.calls and not constructions


def test_unconfigured_provider_fails_before_store_and_runner(tmp_path, monkeypatch, capsys):
    args, provider, settings, _ = prepare_cli(tmp_path, monkeypatch)
    settings.openai_api_key = None
    monkeypatch.setattr(cli, "create_provider", create_provider)
    assert cli.main(args) == 2
    assert "not configured" in capsys.readouterr().err
    assert not provider.calls and not settings.metrics_db_path.exists()


@pytest.mark.parametrize("stage", ["factory", "initialize", "run", "save"])
def test_system_failures_nonzero_and_sanitized(tmp_path, monkeypatch, capsys, stage):
    args, provider, settings, _ = prepare_cli(tmp_path, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError(
            "Authorization: Bearer raw-secret; "
            f"{settings.openai_api_key} {settings.metrics_db_path} raw-exception-payload"
        )

    if stage == "factory":
        monkeypatch.setattr(cli, "create_provider", broken)
    elif stage == "initialize":
        monkeypatch.setattr(cli, "SQLiteBenchmarkStore", broken)
    elif stage == "run":

        async def broken_run(*args, **kwargs):
            broken()

        monkeypatch.setattr(cli.BenchmarkRunner, "run", broken_run)
    else:
        monkeypatch.setattr(cli.SQLiteBenchmarkStore, "save_run", broken)
    assert cli.main(args) == 4
    output = capsys.readouterr()
    assert not output.out
    for forbidden in (
        "Authorization",
        "Bearer",
        "raw-secret",
        "fake-private",
        "benchmark.db",
        "raw-exception-payload",
    ):
        assert forbidden not in output.err
    if stage != "save":
        assert not provider.calls


def test_runner_invariant_is_system_failure_not_input_error(tmp_path, monkeypatch):
    args, _, settings, _ = prepare_cli(
        tmp_path,
        monkeypatch,
        outcomes=[outcome(model="wrong-target")],
    )
    assert cli.main(args) == 4
    assert SQLiteBenchmarkStore(settings.metrics_db_path).list_runs() == []


def test_auth_save_failure_takes_precedence_over_auth_exit(tmp_path, monkeypatch):
    args, _, _, _ = prepare_cli(
        tmp_path,
        monkeypatch,
        outcomes=[outcome(error="authentication_error")],
    )

    def broken(*args):
        raise OSError("private database path")

    monkeypatch.setattr(cli.SQLiteBenchmarkStore, "save_run", broken)
    assert cli.main(args) == 4


def test_invalid_argument_does_not_echo_secret(tmp_path, monkeypatch, capsys):
    args, _, _, _ = prepare_cli(tmp_path, monkeypatch)
    assert cli.main([*args, "--unknown", "Bearer very-private-argument"]) == 2
    assert "very-private" not in capsys.readouterr().err


def test_invalid_settings_is_configuration_error(tmp_path, monkeypatch, capsys):
    args, _, _, constructions = prepare_cli(tmp_path, monkeypatch)

    def broken():
        raise ValueError("environment with raw-private-setting")

    monkeypatch.setattr(cli.Settings, "from_env", broken)
    assert cli.main(args) == 2 and not constructions
    assert "raw-private" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "name,adapter",
    [("openai", OpenAIProvider), ("gemini", GeminiProvider), ("deepseek", DeepSeekProvider)],
)
def test_shared_factory_uses_production_config_without_network(name, adapter):
    settings = Settings(
        **{f"{name}_api_key": "test-credential", f"{name}_base_url": "https://unit.invalid/v1/"}
    )

    async def check():
        async with httpx.AsyncClient() as client:
            provider = create_provider(name, settings, client)
            assert isinstance(provider, adapter) and provider.name == name
            assert provider.api_key == "test-credential"
            assert provider.base_url == "https://unit.invalid/v1"
            assert provider.client is client and provider.configured
            assert not create_provider(name, Settings(), client).configured

    asyncio.run(check())


def test_factory_rejects_unknown_without_echo():
    async def check():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="^unknown provider$"):
                create_provider("Bearer secret", Settings(), client)

    asyncio.run(check())


def test_cli_isolated_from_production_and_resolver_reads_saved_run(tmp_path, monkeypatch):
    args, _, settings, _ = prepare_cli(tmp_path, monkeypatch, count=5)
    settings.metrics_db_path.parent.mkdir(parents=True)
    metrics = SQLiteMetricsStore(settings.metrics_db_path)
    metrics.record_attempt(make_attempt("production"))
    metrics.record_routing_decision(make_decision("production"))
    metrics.upsert_health(opened())

    def production_rows():
        with sqlite3.connect(settings.metrics_db_path) as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone() == (4,)
            return {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("attempts", "pricing", "provider_health", "routing_decisions")
            }

    before = production_rows()

    def forbidden(*args, **kwargs):
        pytest.fail("CLI benchmark accessed the production control plane")

    for method in ("record_attempt", "record_routing_decision", "upsert_health", "get_health"):
        monkeypatch.setattr(SQLiteMetricsStore, method, forbidden)
    monkeypatch.setattr("modelpilot.router.ModelRouter.rank_with_explanation", forbidden)
    monkeypatch.setattr("modelpilot.service.GatewayService.complete", forbidden)
    monkeypatch.setattr("modelpilot.health.manager.ProviderHealthManager.update", forbidden)
    assert cli.main(args) == 0
    assert production_rows() == before
    store = SQLiteBenchmarkStore(settings.metrics_db_path)
    result = store.list_runs()[0]
    snapshot = BenchmarkQualityResolver(suite(5), store).get_quality_snapshot(
        TARGET.provider,
        TARGET.model,
    )
    assert snapshot.quality_score == 1 and snapshot.evaluated_cases == 5
    assert snapshot.source_run_ids == (result.run_id,)


def test_module_entrypoint_help_is_network_free():
    result = subprocess.run(
        [sys.executable, "-m", "modelpilot.benchmarks.cli", "run", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--suite" in result.stdout and "--provider" in result.stdout
    assert "--model" in result.stdout and "--timeout" in result.stdout
    assert not result.stderr
