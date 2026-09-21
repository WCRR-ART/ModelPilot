export type Health = {
  status: string;
  version: string;
};

export type ProviderHealth = {
  provider: string;
  model: string;
  state: "CLOSED" | "OPEN" | "HALF_OPEN";
  eligible: boolean;
  reason: string;
  consecutive_failures: number;
  opened_at: string | null;
  cooldown_until: string | null;
  last_failure_at: string | null;
  last_success_at: string | null;
  updated_at: string | null;
  probe_in_flight: boolean;
};

export function fetchProviderHealth(apiUrl: string, signal?: AbortSignal): Promise<ProviderHealth[]> {
  return fetchJson(apiUrl, "/v1/health/providers", signal);
}

export type MetricsWindow = {
  since: string;
  until: string;
};

export type MetricsSummary = {
  window: MetricsWindow;
  request_count: number;
  attempt_count: number;
  success_count: number;
  failure_count: number;
  success_rate: number | null;
  average_latency_ms: number | null;
  estimated_total_cost: string | null;
  providers_count: number;
  models_count: number;
  routing_decision_count: number;
};

export type ProviderMetrics = {
  provider: string;
  model: string;
  sample_count: number;
  success_count: number;
  failure_count: number;
  success_rate: number | null;
  average_latency_ms: number | null;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  priced_sample_count: number;
  estimated_average_cost: string | null;
  p50_estimated_cost: string | null;
  window_start: string;
  window_end: string;
};

export type RoutingSignal = {
  provider: string;
  model: string;
  final_score: number;
};

export type RoutingExplanation = {
  selected: RoutingSignal;
  candidates: RoutingSignal[];
};

export type RoutingDecision = {
  request_id: string;
  created_at: string;
  routing_version: string;
  selected_provider: string;
  selected_model: string;
  served_provider: string | null;
  served_model: string | null;
  explanation: RoutingExplanation;
};

export type RecentFailure = {
  request_id: string;
  provider: string;
  model: string;
  created_at: string;
  finished_at: string;
  latency_ms: number;
  error_type: string | null;
};

export type BenchmarkTarget = { provider: string; model: string };

export type BenchmarkFilters = {
  provider?: string;
  model?: string;
  suite_id?: string;
  suite_version?: string;
};

export type BenchmarkRunSummary = BenchmarkTarget & {
  run_id: string;
  suite_id: string;
  suite_version: string;
  suite_fingerprint: string;
  status: "completed" | "completed_with_failures";
  started_at: string;
  finished_at: string;
  total_cases: number;
  completed_cases: number;
  execution_failed_cases: number;
  terminated_early: boolean;
};

export type BenchmarkRunConfig = {
  max_cases: number;
  case_timeout_seconds: number;
  temperature: number;
  max_tokens: number;
};

export type BenchmarkCategory =
  | "coding" | "reasoning" | "structured_output" | "math" | "instruction_following";

export type CaseEvaluation = {
  evaluator_kind:
    | "exact_match" | "normalized_exact_match" | "contains" | "numeric_tolerance" | "json_equal";
  evaluator_version: "1";
  score: number;
  passed: boolean;
  reason: "match" | "mismatch" | "invalid_number" | "outside_tolerance" | "invalid_json";
};

export type BenchmarkCaseResult = BenchmarkTarget & {
  case_index: number;
  case_id: string;
  category: BenchmarkCategory;
  execution_status: "completed" | "provider_failed";
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  error_type:
    | "timeout" | "connection_error" | "authentication_error" | "rate_limit"
    | "provider_error" | "invalid_response" | "unknown_error" | null;
  evaluation: CaseEvaluation | null;
};

export type BenchmarkRunDetail = BenchmarkRunSummary & {
  config: BenchmarkRunConfig;
  case_results: BenchmarkCaseResult[];
};

export type QualityMetrics = {
  quality_score: number | null;
  coverage: number;
  execution_completeness: number;
  weighted_evaluation_coverage: number;
  confidence: number;
  total_cases: number;
  observed_cases: number;
  evaluated_cases: number;
  execution_failed_cases: number;
};

export type CategoryQualitySnapshot = QualityMetrics & { category: BenchmarkCategory };

export type QualitySnapshot = QualityMetrics & BenchmarkTarget & {
  suite_id: string;
  suite_version: string;
  suite_fingerprint: string;
  policy_version: "latest_attempt_v1";
  run_config: BenchmarkRunConfig | null;
  source_run_count: number;
  source_run_ids: string[];
  latest_run_id: string | null;
  latest_run_coverage: number | null;
  latest_run_completeness: number | null;
  categories: CategoryQualitySnapshot[];
  generated_at: string;
};

const errorCodes = [
  "benchmark_run_not_found", "no_quality_evidence", "quality_suite_not_configured",
  "benchmark_data_unavailable", "invalid_benchmark_target",
  "request_timeout", "request_failed", "invalid_response",
] as const;

type ErrorCode = (typeof errorCodes)[number];

export class ApiError extends Error {
  constructor(public readonly status: number | null, public readonly code: ErrorCode) {
    super("Unable to load API data.");
    this.name = "ApiError";
  }
}

function safeErrorCode(payload: unknown): ErrorCode {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = payload.detail;
    if (typeof detail === "object" && detail !== null && "code" in detail) {
      const code = detail.code;
      if (typeof code === "string" && errorCodes.includes(code as ErrorCode)) {
        return code as ErrorCode;
      }
    }
  }
  return "request_failed";
}

async function fetchJson<T>(apiUrl: string, path: string, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) throw new DOMException("Request cancelled.", "AbortError");
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  const interrupted = new Promise<never>((_, reject) => {
    controller.signal.addEventListener("abort", () => {
      reject(timedOut
        ? new ApiError(null, "request_timeout")
        : new DOMException("Request cancelled.", "AbortError"));
    }, { once: true });
  });
  // The deadline includes response-body consumption, not just receipt of headers.
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 10_000);
  try {
    const request = (async () => {
      const response = await fetch(`${apiUrl}${path}`, {
        method: "GET", cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        throw new ApiError(response.status, safeErrorCode(payload));
      }
      try {
        return (await response.json()) as T;
      } catch {
        throw new ApiError(response.status, "invalid_response");
      }
    })();
    return await Promise.race([request, interrupted]);
  } catch (error) {
    if (signal?.aborted) throw new DOMException("Request cancelled.", "AbortError");
    if (timedOut) throw new ApiError(null, "request_timeout");
    if (error instanceof ApiError) throw error;
    throw new ApiError(null, "request_failed");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

export function fetchBenchmarkRuns(
  apiUrl: string, filters: BenchmarkFilters = {}, signal?: AbortSignal,
): Promise<BenchmarkRunSummary[]> {
  const params = new URLSearchParams({ limit: "20" });
  for (const key of ["provider", "model", "suite_id", "suite_version"] as const) {
    const value = filters[key];
    if (value !== undefined && value !== "") params.set(key, value);
  }
  return fetchJson(apiUrl, `/v1/benchmarks/runs?${params}`, signal);
}

export function fetchBenchmarkRun(
  apiUrl: string, runId: string, signal?: AbortSignal,
): Promise<BenchmarkRunDetail> {
  return fetchJson(apiUrl, `/v1/benchmarks/runs/${encodeURIComponent(runId)}`, signal);
}

export function fetchBenchmarkQuality(
  apiUrl: string, target: BenchmarkTarget, signal?: AbortSignal,
): Promise<QualitySnapshot> {
  const params = new URLSearchParams({ provider: target.provider, model: target.model });
  return fetchJson(apiUrl, `/v1/benchmarks/quality?${params}`, signal);
}

export function fetchHealth(apiUrl: string, signal?: AbortSignal): Promise<Health> {
  return fetchJson(apiUrl, "/health", signal);
}

export function fetchSummary(apiUrl: string, signal?: AbortSignal): Promise<MetricsSummary> {
  return fetchJson(apiUrl, "/v1/metrics/summary?hours=24", signal);
}

export function fetchProviderMetrics(apiUrl: string, signal?: AbortSignal): Promise<ProviderMetrics[]> {
  return fetchJson(apiUrl, "/v1/metrics/providers", signal);
}

export function fetchRoutingDecisions(apiUrl: string, signal?: AbortSignal): Promise<RoutingDecision[]> {
  return fetchJson(apiUrl, "/v1/metrics/routing-decisions?limit=10", signal);
}

export function fetchRecentFailures(apiUrl: string, signal?: AbortSignal): Promise<RecentFailure[]> {
  return fetchJson(apiUrl, "/v1/metrics/failures?limit=10", signal);
}

export function formatPercent(value: number | null | undefined): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatLatency(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value / 1000)} s`;
}

export function formatCost(value: string | null | undefined): string {
  return value == null ? "—" : `$${value}`;
}

export function formatScore(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(3);
}

const dateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

export function formatDateTime(value: string | null | undefined): string {
  if (value == null) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateTimeFormatter.format(date);
}
