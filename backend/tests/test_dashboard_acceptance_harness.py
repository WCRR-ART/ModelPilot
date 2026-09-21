"""Verify executable acceptance fixtures are real components, not fabricated API JSON."""

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def acceptance_app(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    path = Path(__file__).resolve().parents[2] / "scripts" / "dashboard_acceptance.py"
    spec = importlib.util.spec_from_file_location("dashboard_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return asyncio.run(module.build_acceptance_app(tmp_path, 3100))


def test_acceptance_runner_store_api_and_quality(acceptance_app):
    with TestClient(acceptance_app) as client:
        runs = client.get("/v1/benchmarks/runs").json()
        assert len(runs) == 3
        assert {run["run_id"] for run in runs} == {
            "test-data-full-openai", "test-data-mixed-gemini", "test-data-historical-smoke",
        }
        detail = client.get("/v1/benchmarks/runs/test-data-mixed-gemini").json()
        assert detail["total_cases"] == 60
        assert detail["terminated_early"] is True
        assert len(detail["case_results"]) == 6
        assert [case["case_index"] for case in detail["case_results"]] == list(range(6))
        wrong = detail["case_results"][1]
        assert wrong["execution_status"] == "completed"
        assert wrong["evaluation"]["score"] == 0
        assert detail["case_results"][2]["evaluation"] is None
        assert detail["case_results"][2]["error_type"] == "timeout"
        assert detail["case_results"][3]["error_type"] == "rate_limit"
        assert detail["case_results"][5]["error_type"] == "authentication_error"
        quality = client.get(
            "/v1/benchmarks/quality", params={"provider": "openai", "model": "test-model-a"},
        ).json()
        assert quality["evaluated_cases"] == 60
        assert quality["quality_score"] == quality["confidence"] == 1
        assert quality["source_run_ids"] == ["test-data-full-openai"]


def test_acceptance_only_normal_chat_created_production_evidence(acceptance_app):
    with TestClient(acceptance_app) as client:
        decisions = client.get("/v1/metrics/routing-decisions").json()
        assert len(decisions) == 1
        selected = decisions[0]["explanation"]["selected"]
        assert selected["provider"] == "openai"
        assert selected["static_quality_score"] == 0.6
        assert selected["quality_score"] == selected["benchmark_confidence"] == 1
        assert selected["benchmark_source_run_ids"] == ["test-data-full-openai"]
        assert len(client.get("/v1/logs").json()) == 1
        assert client.get("/v1/benchmarks/runs/missing-test-run").status_code == 404
