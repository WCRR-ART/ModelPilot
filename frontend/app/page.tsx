"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import BenchmarkDashboard from "../components/benchmark-dashboard";

import {
  fetchHealth,
  fetchProviderHealth,
  fetchProviderMetrics,
  fetchRecentFailures,
  fetchRoutingDecisions,
  fetchSummary,
  formatCost,
  formatDateTime,
  formatLatency,
  formatPercent,
  formatScore,
  type Health,
  type ProviderHealth,
  type MetricsSummary,
  type ProviderMetrics,
  type RecentFailure,
  type RoutingDecision,
} from "../lib/api";

type Loadable<T> =
  | { status: "loading"; data: null }
  | { status: "success"; data: T }
  | { status: "error"; data: null };

const loading = <T,>(): Loadable<T> => ({ status: "loading", data: null });

function settled<T>(result: PromiseSettledResult<T>): Loadable<T> {
  return result.status === "fulfilled"
    ? { status: "success", data: result.value }
    : { status: "error", data: null };
}

function SectionState({ kind }: { kind: "loading" | "error" }) {
  return (
    <div className={`sectionState ${kind}`} role={kind === "error" ? "alert" : "status"}>
      {kind === "loading" ? "Loading metrics…" : "Unable to load metrics."}
    </div>
  );
}

function ProviderHealthTable({ providers }: { providers: ProviderHealth[] }) {
  if (providers.length === 0) {
    return <div className="emptyState">No configured providers.</div>;
  }
  const labels = { CLOSED: "Healthy", OPEN: "Open", HALF_OPEN: "Recovering" };
  const timestamp = (value: string | null) => value === null ? "—" : formatDateTime(value);
  return (
    <div className="tableWrap">
      <table>
        <caption className="srOnly">Provider circuit health snapshot</caption>
        <thead><tr>
          <th>Provider</th><th>Model</th><th>Health</th><th>Consecutive Failures</th>
          <th>Cooldown</th><th>Probe</th><th>Last Success</th><th>Last Failure</th>
        </tr></thead>
        <tbody>{providers.map((provider) => (
          <tr key={`${provider.provider}:${provider.model}`}>
            <td><span className="providerBadge">{provider.provider}</span></td>
            <td className="modelName">{provider.model}</td>
            <td><span className={`circuitBadge ${provider.reason === "health_unknown" ? "unknown" : provider.state}`}>
              ● {provider.reason === "health_unknown" ? "No health record" : labels[provider.state]}
            </span></td>
            <td>{provider.consecutive_failures ?? "—"}</td>
            <td>{provider.cooldown_until === null ? "—" : new Date(provider.cooldown_until).toISOString().replace("T", " ").replace(".000Z", " UTC")}</td>
            <td>{provider.probe_in_flight ? "Probe in progress" : provider.state === "HALF_OPEN" ? "Ready to probe" : "—"}</td>
            <td>{timestamp(provider.last_success_at)}</td>
            <td>{timestamp(provider.last_failure_at)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function SummaryCards({ summary }: { summary: MetricsSummary }) {
  const empty = summary.attempt_count === 0;
  const cards = [
    ["Requests", summary.request_count.toLocaleString(), "Distinct requests · 24h"],
    ["Attempts", summary.attempt_count.toLocaleString(), "Includes fallback attempts"],
    ["Success Rate", formatPercent(summary.success_rate), `${summary.success_count} successful · ${summary.failure_count} failed`],
    ["Average Latency", formatLatency(summary.average_latency_ms), "Across completed attempts"],
    ["Estimated Cost", formatCost(summary.estimated_total_cost), "Configured prices · not billing"],
  ];

  return (
    <>
      {empty && (
        <p className="emptyNotice" role="status">
          No requests in this 24-hour window. Send a request through ModelPilot to begin collecting metrics.
        </p>
      )}
      <div className="metrics" aria-label="Gateway metrics">
        {cards.map(([label, value, note]) => (
          <article key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <small>{note}</small>
          </article>
        ))}
      </div>
    </>
  );
}

function ProviderTable({ providers }: { providers: ProviderMetrics[] }) {
  if (providers.length === 0) {
    return <div className="emptyState">No provider metrics yet. Send requests through ModelPilot to build routing metrics.</div>;
  }

  return (
    <div className="tableWrap">
      <table>
        <caption className="srOnly">Recent metrics by provider and model</caption>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Model</th>
            <th>Samples</th>
            <th>Success Rate</th>
            <th>Avg Latency</th>
            <th>P50</th>
            <th>P95</th>
            <th>Avg Estimated Cost</th>
          </tr>
        </thead>
        <tbody>
          {providers.map((provider) => (
            <tr key={`${provider.provider}:${provider.model}`}>
              <td><span className="providerBadge">{provider.provider}</span></td>
              <td className="modelName">{provider.model}</td>
              <td>{provider.sample_count}</td>
              <td>{formatPercent(provider.success_rate)}</td>
              <td>{formatLatency(provider.average_latency_ms)}</td>
              <td>{formatLatency(provider.p50_latency_ms)}</td>
              <td>{formatLatency(provider.p95_latency_ms)}</td>
              <td>{formatCost(provider.estimated_average_cost)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RoutingList({ decisions }: { decisions: RoutingDecision[] }) {
  if (decisions.length === 0) {
    return <div className="emptyState">No routing decisions yet.</div>;
  }

  return (
    <div className="eventList">
      {decisions.map((decision) => {
        const served = decision.served_provider;
        const fallback =
          served !== null &&
          (served !== decision.selected_provider ||
            decision.served_model !== decision.selected_model);
        const routeStatus = served === null ? "unknown" : fallback ? "yes" : "no";
        return (
          <article className="event" key={decision.request_id}>
            <time dateTime={decision.created_at}>{formatDateTime(decision.created_at)}</time>
            <div className="eventRoute">
              <span>Selected</span>
              <strong>{decision.selected_provider}</strong>
              <small>{decision.selected_model}</small>
            </div>
            <div className="eventRoute">
              <span>Served by</span>
              <strong>{served ?? "—"}</strong>
              <small>{decision.served_model ?? "Unavailable"}</small>
            </div>
            <div className="eventMeta">
              <span>Top score</span>
              <strong>{formatScore(decision.explanation.selected.final_score)}</strong>
            </div>
            <span className={`fallback ${routeStatus}`}>
              {served === null ? "Not served" : fallback ? "Fallback" : "Direct"}
            </span>
          </article>
        );
      })}
    </div>
  );
}

function FailureList({ failures }: { failures: RecentFailure[] }) {
  if (failures.length === 0) {
    return <div className="emptyState positive">No recent provider failures.</div>;
  }

  return (
    <div className="eventList failures">
      {failures.map((failure) => (
        <article className="event failure" key={`${failure.request_id}:${failure.provider}:${failure.finished_at}`}>
          <time dateTime={failure.finished_at}>{formatDateTime(failure.finished_at)}</time>
          <div className="eventRoute">
            <span>Provider</span>
            <strong>{failure.provider}</strong>
            <small>{failure.model}</small>
          </div>
          <div className="eventMeta">
            <span>Error type</span>
            <strong>{failure.error_type?.replaceAll("_", " ") ?? "Unknown"}</strong>
          </div>
          <div className="eventMeta alignRight">
            <span>Latency</span>
            <strong>{formatLatency(failure.latency_ms)}</strong>
          </div>
        </article>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const [health, setHealth] = useState<Loadable<Health>>(loading);
  const [providerHealth, setProviderHealth] = useState<Loadable<ProviderHealth[]>>(loading);
  const [summary, setSummary] = useState<Loadable<MetricsSummary>>(loading);
  const [providers, setProviders] = useState<Loadable<ProviderMetrics[]>>(loading);
  const [decisions, setDecisions] = useState<Loadable<RoutingDecision[]>>(loading);
  const [failures, setFailures] = useState<Loadable<RecentFailure[]>>(loading);
  const requestSequence = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  const [benchmarkRefresh, setBenchmarkRefresh] = useState(0);
  const apiUrl = process.env.NEXT_PUBLIC_MODELPILOT_API_URL?.replace(/\/$/, "") ?? "";

  const load = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const sequence = ++requestSequence.current;
    const results = await Promise.allSettled([
      fetchHealth(apiUrl, controller.signal),
      fetchSummary(apiUrl, controller.signal),
      fetchProviderMetrics(apiUrl, controller.signal),
      fetchRoutingDecisions(apiUrl, controller.signal),
      fetchRecentFailures(apiUrl, controller.signal),
      fetchProviderHealth(apiUrl, controller.signal),
    ] as const);

    if (sequence !== requestSequence.current) return;
    setHealth(settled(results[0]));
    setSummary(settled(results[1]));
    setProviders(settled(results[2]));
    setDecisions(settled(results[3]));
    setFailures(settled(results[4]));
    setProviderHealth(settled(results[5]));
  }, [apiUrl]);

  const refresh = useCallback(async () => {
    setBenchmarkRefresh(value => value + 1);
    setHealth(loading());
    setProviderHealth(loading());
    setSummary(loading());
    setProviders(loading());
    setDecisions(loading());
    setFailures(loading());
    await load();
  }, [load]);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
      activeRequest.current?.abort();
    };
  }, [load]);

  const loadingAny = [health, providerHealth, summary, providers, decisions, failures].some(
    (resource) => resource.status === "loading",
  );

  return (
    <main>
      <header className="topbar">
        <div className="brand">
          <span className="brandMark" aria-hidden="true">M</span>
          <span>ModelPilot</span>
          <span className="version">V0.3.0</span>
        </div>
        <div className={`health ${health.status}`} aria-live="polite">
          <span className="healthDot" aria-hidden="true" />
          {health.status === "success"
            ? `Backend online · ${health.data.version}`
            : health.status === "error"
              ? "Backend offline"
              : "Checking backend"}
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">LOCAL ROUTING OBSERVABILITY</p>
          <h1>Gateway metrics.<br /><span>Measured locally.</span></h1>
        </div>
        <div className="introActions">
          <p>Operational metrics from completed provider attempts. Costs are estimates, not billing data.</p>
          <button type="button" onClick={() => void refresh()} disabled={loadingAny}>
            {loadingAny ? "Refreshing…" : "Refresh dashboard"}
          </button>
        </div>
      </section>

      <section className="dashboardSection" aria-labelledby="summary-heading">
        <div className="sectionHeading">
          <div>
            <p className="eyebrow">LAST 24 HOURS</p>
            <h2 id="summary-heading">Gateway summary</h2>
          </div>
          {summary.status === "success" && (
            <time dateTime={summary.data.window.until}>Updated {formatDateTime(summary.data.window.until)}</time>
          )}
        </div>
        {summary.status === "loading" && <SectionState kind="loading" />}
        {summary.status === "error" && <SectionState kind="error" />}
        {summary.status === "success" && <SummaryCards summary={summary.data} />}
      </section>

      <section className="panel dashboardSection" aria-labelledby="provider-health-heading">
        <div className="sectionHeading compact">
          <div><p className="eyebrow">CIRCUIT STATUS</p><h2 id="provider-health-heading">Provider Health</h2></div>
          <span className="formula">SNAPSHOT ON REFRESH · COOLDOWN UTC</span>
        </div>
        {providerHealth.status === "loading" && <div className="sectionState" role="status">Loading provider health…</div>}
        {providerHealth.status === "error" && <div className="sectionState error" role="alert">Unable to load provider health.</div>}
        {providerHealth.status === "success" && <ProviderHealthTable providers={providerHealth.data} />}
      </section>

      <section className="panel dashboardSection" aria-labelledby="providers-heading">
        <div className="sectionHeading compact">
          <div>
            <p className="eyebrow">PROVIDER PERFORMANCE</p>
            <h2 id="providers-heading">Provider metrics</h2>
          </div>
          <span className="formula">7 DAYS / 100 ATTEMPTS</span>
        </div>
        {providers.status === "loading" && <SectionState kind="loading" />}
        {providers.status === "error" && <SectionState kind="error" />}
        {providers.status === "success" && <ProviderTable providers={providers.data} />}
      </section>

      <div className="activityGrid">
        <section className="panel" aria-labelledby="routing-heading">
          <div className="sectionHeading compact">
            <div>
              <p className="eyebrow">RECENT ACTIVITY</p>
              <h2 id="routing-heading">Routing decisions</h2>
            </div>
            <span className="formula">LATEST 10</span>
          </div>
          {decisions.status === "loading" && <SectionState kind="loading" />}
          {decisions.status === "error" && <SectionState kind="error" />}
          {decisions.status === "success" && <RoutingList decisions={decisions.data} />}
        </section>

        <section className="panel" aria-labelledby="failures-heading">
          <div className="sectionHeading compact">
            <div>
              <p className="eyebrow dangerText">FALLBACK SIGNALS</p>
              <h2 id="failures-heading">Recent failures</h2>
            </div>
            <span className="formula">LATEST 10</span>
          </div>
          {failures.status === "loading" && <SectionState kind="loading" />}
          {failures.status === "error" && <SectionState kind="error" />}
          {failures.status === "success" && <FailureList failures={failures.data} />}
        </section>
      </div>

      <BenchmarkDashboard apiUrl={apiUrl} refreshKey={benchmarkRefresh} />

      <footer>
        <span>MODEL PILOT / LOCAL CONTROL PLANE</span>
        <span>{apiUrl || "SAME-ORIGIN API"}</span>
      </footer>
    </main>
  );
}
