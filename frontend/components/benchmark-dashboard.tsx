"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError, fetchBenchmarkRuns, fetchBenchmarkRun, fetchBenchmarkQuality,
  type BenchmarkFilters, type BenchmarkRunSummary, type BenchmarkTarget,
} from "../lib/api";
import { LatestRequest } from "../lib/latest-request";
import { QualityEvidence, RunDetailsView, RunsView } from "./benchmark-evidence";

export type ReadState<T> =
  | { status: "idle" | "loading"; data?: never }
  | { status: "success"; data: T }
  | { status: "error"; data?: never; error: unknown };

function useRead<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): ReadState<T> {
  const latest = useRef(new LatestRequest());
  const [result, setResult] = useState<{ key: string; state: ReadState<T> } | null>(null);
  useEffect(() => {
    if (key === null) return;
    const owner = latest.current;
    const request = owner.begin();
    void load(request.signal).then(
      data => { if (request.isCurrent()) setResult({ key, state: { status: "success", data } }); },
      error => { if (request.isCurrent()) setResult({ key, state: { status: "error", error } }); },
    );
    return () => owner.cancel();
  }, [key, load]);
  return key === null ? { status: "idle" } : result?.key === key ? result.state : { status: "loading" };
}

export function BenchmarkState({ state, kind }: {
  state: ReadState<unknown>; kind: "runs" | "detail" | "quality";
}) {
  if (state.status === "success") return null;
  let message = state.status === "loading" ? "Loading benchmark data…"
    : kind === "detail" ? "Select a run to view its historical details."
    : "Choose an explicit provider and model to query current quality.";
  let failure = false;
  if (state.status === "error") {
    const error = state.error;
    failure = true;
    message = "Unable to load benchmark data. Please try Refresh.";
    if (error instanceof ApiError) {
      if (kind === "quality" && error.status === 503 && error.code === "quality_suite_not_configured") {
        message = "Quality suite not configured. Set MODELPILOT_QUALITY_SUITE_PATH on the server.";
        failure = false;
      } else if (kind === "quality" && error.status === 404 && error.code === "no_quality_evidence") {
        message = "No quality evidence for this target in the currently configured suite.";
        failure = false;
      } else if (kind === "detail" && error.status === 404) {
        message = "Run details not found. The selected run does not exist.";
        failure = false;
      }
    }
  }
  return <div className={`sectionState ${failure ? "error" : ""}`} role={failure ? "alert" : "status"}>{message}</div>;
}

const emptyFilters = { provider: "", model: "", suite_id: "", suite_version: "" };

export default function BenchmarkDashboard({ apiUrl, refreshKey }: { apiUrl: string; refreshKey: number }) {
  const [draft, setDraft] = useState(emptyFilters);
  const [filters, setFilters] = useState<BenchmarkFilters>({});
  const [filterVersion, setFilterVersion] = useState(0);
  const [selected, setSelected] = useState<{ run: BenchmarkRunSummary; version: number } | null>(null);
  const [targetDraft, setTargetDraft] = useState({ provider: "", model: "" });
  const [target, setTarget] = useState<{ value: BenchmarkTarget; version: number } | null>(null);
  const listLoad = useCallback((signal: AbortSignal) => fetchBenchmarkRuns(apiUrl, filters, signal), [apiUrl, filters]);
  const detailLoad = useCallback((signal: AbortSignal) => fetchBenchmarkRun(apiUrl, selected!.run.run_id, signal), [apiUrl, selected]);
  const qualityLoad = useCallback((signal: AbortSignal) => fetchBenchmarkQuality(apiUrl, target!.value, signal), [apiUrl, target]);
  const runs = useRead(`${apiUrl}:${refreshKey}:${filterVersion}`, listLoad);
  const detail = useRead(selected ? `${apiUrl}:${refreshKey}:${selected.version}:${selected.run.run_id}` : null, detailLoad);
  const quality = useRead(target ? `${apiUrl}:${refreshKey}:${target.version}:${JSON.stringify(target.value)}` : null, qualityLoad);
  const filtered = Object.values(filters).some(Boolean);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFilters(Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, value.trim()])));
    setFilterVersion(version => version + 1);
  }
  function selectTarget(value: BenchmarkTarget) {
    setTargetDraft(value);
    setTarget(previous => ({ value, version: (previous?.version ?? 0) + 1 }));
  }
  function queryQuality(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    selectTarget({ provider: targetDraft.provider.trim(), model: targetDraft.model.trim() });
  }
  const validTarget = [targetDraft.provider, targetDraft.model].every(value => value.trim() && value.trim().toLowerCase() !== "auto");

  return <div className="benchmarkDashboard">
    <section className="panel dashboardSection" aria-labelledby="benchmark-runs-heading">
      <div className="sectionHeading compact"><div><p className="eyebrow">READ-ONLY BENCHMARK EVIDENCE</p><h2 id="benchmark-runs-heading">Benchmark Runs</h2></div><span className="formula">LATEST 20 MATCHING RUNS</span></div>
      <p className="benchmarkNote">Recent returned records, not all history or a model ranking. Completed does not mean every answer was correct. Benchmarks are explicitly executed with the local CLI and may consume Provider API credits.</p>
      <form className="benchmarkForm" onSubmit={applyFilters} aria-label="Benchmark run filters">
        {([['provider', 'Provider filter'], ['model', 'Model filter'], ['suite_id', 'Suite ID filter'], ['suite_version', 'Suite version filter']] as const).map(([key, label]) => <label key={key}>{label}<input value={draft[key]} onChange={event => setDraft({ ...draft, [key]: event.target.value })} /></label>)}
        <button type="submit">Apply filters</button>
      </form>
      <BenchmarkState state={runs} kind="runs" />
      {runs.status === "success" && <RunsView runs={runs.data} filtered={filtered} onSelect={run => setSelected(previous => ({ run, version: (previous?.version ?? 0) + 1 }))} />}
    </section>
    <section className="panel dashboardSection" aria-labelledby="benchmark-details-heading">
      <div className="sectionHeading compact"><h2 id="benchmark-details-heading">Run Details</h2></div>
      <BenchmarkState state={detail} kind="detail" />
      {detail.status === "success" && <><RunDetailsView run={detail.data} /><button type="button" onClick={() => selectTarget({ provider: detail.data.provider, model: detail.data.model })}>Query current quality for this target</button></>}
    </section>
    <section className="panel dashboardSection" aria-labelledby="benchmark-quality-heading">
      <div className="sectionHeading compact"><h2 id="benchmark-quality-heading">Benchmark Quality</h2></div>
      <form className="benchmarkForm qualityForm" onSubmit={queryQuality} aria-label="Quality target">
        <label>Quality provider<input required value={targetDraft.provider} onChange={event => setTargetDraft({ ...targetDraft, provider: event.target.value })} /></label>
        <label>Quality model<input required value={targetDraft.model} onChange={event => setTargetDraft({ ...targetDraft, model: event.target.value })} /></label>
        <button type="submit" disabled={!validTarget}>Query quality</button>
      </form>
      <p className="benchmarkNote">Enter any explicit target, including models absent from the recent run list. Queries use the server&apos;s configured quality suite.</p>
      {target && <p className="benchmarkNote">Selected query: {target.value.provider} / {target.value.model}</p>}
      <BenchmarkState state={quality} kind="quality" />
      {quality.status === "success" && <QualityEvidence snapshot={quality.data} selectedRun={selected?.run} />}
    </section>
  </div>;
}
