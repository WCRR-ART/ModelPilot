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

export function fetchProviderHealth(apiUrl: string): Promise<ProviderHealth[]> {
  return fetchJson(apiUrl, "/v1/health/providers");
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

async function fetchJson<T>(apiUrl: string, path: string): Promise<T> {
  const response = await fetch(`${apiUrl}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Request failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

export function fetchHealth(apiUrl: string): Promise<Health> {
  return fetchJson(apiUrl, "/health");
}

export function fetchSummary(apiUrl: string): Promise<MetricsSummary> {
  return fetchJson(apiUrl, "/v1/metrics/summary?hours=24");
}

export function fetchProviderMetrics(apiUrl: string): Promise<ProviderMetrics[]> {
  return fetchJson(apiUrl, "/v1/metrics/providers");
}

export function fetchRoutingDecisions(apiUrl: string): Promise<RoutingDecision[]> {
  return fetchJson(apiUrl, "/v1/metrics/routing-decisions?limit=10");
}

export function fetchRecentFailures(apiUrl: string): Promise<RecentFailure[]> {
  return fetchJson(apiUrl, "/v1/metrics/failures?limit=10");
}

export function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatLatency(value: number | null): string {
  if (value === null) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value / 1000)} s`;
}

export function formatCost(value: string | null): string {
  return value === null ? "—" : `$${value}`;
}

export function formatScore(value: number | null): string {
  return value === null ? "—" : value.toFixed(3);
}

const dateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateTimeFormatter.format(date);
}
