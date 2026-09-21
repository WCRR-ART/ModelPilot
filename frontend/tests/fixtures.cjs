// TEST DATA ONLY. These fixtures never enter the production Dashboard bundle.
const startedAt = "2026-09-21T02:30:00Z";
const finishedAt = "2026-09-21T02:30:05Z";
const runConfig = {
  max_cases: 100,
  case_timeout_seconds: 30,
  temperature: 0,
  max_tokens: 1024,
};

function result(caseIndex, provider, model, score, errorType = null) {
  return {
    case_index: caseIndex,
    provider,
    model,
    case_id: `test-case-${caseIndex}`,
    category: caseIndex === 1 ? "reasoning" : "math",
    execution_status: errorType ? "provider_failed" : "completed",
    latency_ms: caseIndex * 125,
    input_tokens: caseIndex === 0 ? 0 : null,
    output_tokens: caseIndex === 0 ? 0 : null,
    total_tokens: caseIndex === 0 ? 0 : null,
    error_type: errorType,
    evaluation: errorType ? null : {
      evaluator_kind: "exact_match",
      evaluator_version: "1",
      score,
      passed: score === 1,
      reason: score === 1 ? "match" : "mismatch",
    },
  };
}

const runDetail = {
  run_id: "fixture-run-a",
  suite_id: "historical-test-data",
  suite_version: "1",
  suite_fingerprint: "a".repeat(64),
  provider: "openai",
  model: "test-model-a",
  status: "completed_with_failures",
  started_at: startedAt,
  finished_at: finishedAt,
  total_cases: 4,
  completed_cases: 2,
  execution_failed_cases: 1,
  terminated_early: true,
  config: runConfig,
  case_results: [
    result(0, "openai", "test-model-a", 0),
    result(1, "openai", "test-model-a", 1),
    result(2, "openai", "test-model-a", null, "authentication_error"),
  ],
};

const secondRun = {
  ...runDetail,
  run_id: "fixture-run-b",
  provider: "gemini",
  model: "test-model-b",
  started_at: "2026-09-20T02:30:00Z",
  finished_at: "2026-09-20T02:30:05Z",
  total_cases: 2,
  completed_cases: 1,
  execution_failed_cases: 1,
  terminated_early: false,
  case_results: [
    result(0, "gemini", "test-model-b", 1),
    result(1, "gemini", "test-model-b", null, "timeout"),
  ],
};

const runs = [runDetail, secondRun];
const runSummaries = runs.map((run) => Object.fromEntries(
  Object.entries(run).filter(([key]) => key !== "config" && key !== "case_results"),
));

const category = (name, score) => ({
  category: name,
  quality_score: score,
  coverage: 0.8,
  execution_completeness: 0.6,
  weighted_evaluation_coverage: 0.6,
  confidence: 0,
  total_cases: 5,
  observed_cases: 4,
  evaluated_cases: 3,
  execution_failed_cases: 1,
});

const quality = {
  provider: "openai",
  model: "test-model-a",
  suite_id: "configured-test-data",
  suite_version: "2",
  suite_fingerprint: "b".repeat(64),
  policy_version: "latest_attempt_v1",
  quality_score: 5 / 6,
  coverage: 0.8,
  execution_completeness: 0.6,
  weighted_evaluation_coverage: 0.6,
  confidence: (1 / 45) * 0.6 * 0.6,
  total_cases: 10,
  observed_cases: 8,
  evaluated_cases: 6,
  execution_failed_cases: 2,
  run_config: runConfig,
  source_run_count: 2,
  source_run_ids: ["fixture-quality-new", "fixture-quality-old"],
  latest_run_id: "fixture-quality-new",
  latest_run_coverage: 0.3,
  latest_run_completeness: 0.2,
  categories: [category("math", 2 / 3), category("reasoning", 1)],
  generated_at: "2026-09-21T04:00:00Z",
};

function qualityFor(provider, model) {
  const snapshot = structuredClone({ ...quality, provider, model });
  if (model === "smoke") {
    const metrics = {
      quality_score: 1,
      confidence: 0,
      coverage: 1,
      execution_completeness: 1,
      weighted_evaluation_coverage: 1,
      total_cases: 3,
      observed_cases: 3,
      evaluated_cases: 3,
      execution_failed_cases: 0,
    };
    Object.assign(snapshot, metrics, {
      suite_id: "smoke-test-data",
      source_run_count: 1,
      source_run_ids: ["fixture-smoke"],
      latest_run_id: "fixture-smoke",
      latest_run_coverage: 1,
      latest_run_completeness: 1,
      categories: [{ ...metrics, category: "math" }],
    });
  }
  return snapshot;
}

const fixtures = {
  "/health": { status: "ok", version: "0.4.0 · TEST DATA" },
  "/v1/metrics/summary": {
    window: { since: "2026-09-20T04:00:00Z", until: "2026-09-21T04:00:00Z" },
    request_count: 2,
    attempt_count: 3,
    success_count: 2,
    failure_count: 1,
    success_rate: 2 / 3,
    average_latency_ms: 250,
    estimated_total_cost: "0.0012",
    providers_count: 2,
    models_count: 2,
    routing_decision_count: 1,
  },
  "/v1/metrics/providers": [{
    provider: "openai", model: "test-model-a", sample_count: 2,
    success_count: 2, failure_count: 0, success_rate: 1,
    average_latency_ms: 200, p50_latency_ms: 200, p95_latency_ms: 240,
    priced_sample_count: 0, estimated_average_cost: null, p50_estimated_cost: null,
    window_start: startedAt, window_end: finishedAt,
  }],
  "/v1/health/providers": [{
    provider: "openai", model: "test-model-a", state: "CLOSED",
    eligible: true, reason: "healthy", consecutive_failures: 0,
    opened_at: null, cooldown_until: null, last_failure_at: null,
    last_success_at: finishedAt, updated_at: finishedAt, probe_in_flight: false,
  }],
  "/v1/metrics/routing-decisions": [{
    request_id: "fixture-request", created_at: finishedAt, routing_version: "v0.3",
    selected_provider: "openai", selected_model: "test-model-a",
    served_provider: "openai", served_model: "test-model-a",
    explanation: {
      selected: { provider: "openai", model: "test-model-a", final_score: 0.8 },
      candidates: [{ provider: "openai", model: "test-model-a", final_score: 0.8 }],
    },
  }],
  "/v1/metrics/failures": [{
    request_id: "fixture-failure", provider: "gemini", model: "test-model-b",
    created_at: startedAt, finished_at: finishedAt, latency_ms: 1000, error_type: "timeout",
  }],
};

module.exports = { fixtures, quality, qualityFor, runDetail, runSummaries, runs };
