import {
  formatDateTime, formatLatency, formatPercent, formatScore,
  type BenchmarkRunDetail, type BenchmarkRunSummary, type QualitySnapshot,
} from "../lib/api";

export function RunsView({ runs, filtered, onSelect }: {
  runs: BenchmarkRunSummary[]; filtered: boolean; onSelect: (run: BenchmarkRunSummary) => void;
}) {
  if (runs.length === 0) return <div className="emptyState" role="status">No benchmark runs found.<br />{filtered ? "No matches for the applied filters." : "No benchmark runs have been recorded yet."}</div>;
  return <div className="tableWrap" tabIndex={0} role="region" aria-label="Scrollable recent benchmark runs">
    <table><caption className="srOnly">Recent benchmark runs, up to 20 matching records</caption>
      <thead><tr><th>Started (UTC)</th><th>Provider / model</th><th>Suite / version</th><th>Status</th><th>Total / evaluated / execution failures</th><th>Terminated early</th><th>Details</th></tr></thead>
      <tbody>{runs.map(run => <tr key={run.run_id}>
        <td><time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time></td><td>{run.provider} / {run.model}</td><td>{run.suite_id} / {run.suite_version}</td><td>{run.status}</td><td>{run.total_cases} / {run.completed_cases} / {run.execution_failed_cases}</td><td>{run.terminated_early ? "Yes" : "No"}</td>
        <td><button type="button" aria-label={`View details for ${run.run_id}`} onClick={() => onSelect(run)}>View details</button></td>
      </tr>)}</tbody>
    </table>
  </div>;
}

export function RunDetailsView({ run }: { run: BenchmarkRunDetail }) {
  return <>
    <p className="benchmarkNote">One historical execution, not the current aggregated quality snapshot.</p>
    <dl className="benchmarkMetadata">
      <div><dt>Run ID</dt><dd>{run.run_id}</dd></div>
      <div><dt>Target</dt><dd>{run.provider} / {run.model}</dd></div>
      <div><dt>Historical suite</dt><dd>{run.suite_id} / {run.suite_version}</dd></div>
      <div><dt>Fingerprint</dt><dd>{run.suite_fingerprint}</dd></div>
      <div><dt>Started</dt><dd>{formatDateTime(run.started_at)}</dd></div>
      <div><dt>Finished</dt><dd>{formatDateTime(run.finished_at)}</dd></div>
      <div><dt>Status</dt><dd>{run.status}</dd></div>
      <div><dt>Cases</dt><dd>{run.total_cases} total · {run.completed_cases} evaluated · {run.execution_failed_cases} execution failures</dd></div>
      <div><dt>Terminated early</dt><dd>{run.terminated_early ? "Yes — unattempted cases have no results." : "No"}</dd></div>
    </dl>
    <details className="benchmarkProvenance">
      <summary>Saved run configuration</summary>
      <dl className="benchmarkMetadata">
        <div><dt>Max cases</dt><dd>{run.config.max_cases}</dd></div>
        <div><dt>Timeout per case</dt><dd>{run.config.case_timeout_seconds} s</dd></div>
        <div><dt>Temperature</dt><dd>{run.config.temperature}</dd></div>
        <div><dt>Max tokens</dt><dd>{run.config.max_tokens}</dd></div>
      </dl>
    </details>
    <p className="benchmarkNote">Completed means an answer was evaluated, not necessarily correct. Execution failures are unscored.</p>
    <div className="tableWrap" tabIndex={0} role="region" aria-label="Scrollable case results">
      <table><caption className="srOnly">Historical run case results</caption>
        <thead><tr><th>Index / Case</th><th>Category</th><th>Execution</th><th>Benchmark score</th><th>Evaluator / version</th><th>Reason / error</th><th>Latency</th><th>Tokens in / out / total</th></tr></thead>
        <tbody>{[...run.case_results].sort((a, b) => a.case_index - b.case_index).map((item) => <tr key={item.case_index}>
          <td>{item.case_index} / {item.case_id}</td><td>{item.category}</td>
          <td>{item.execution_status === "provider_failed" ? "Execution failed" : "Completed"}</td>
          <td>{item.evaluation == null ? "Unscored" : formatScore(item.evaluation.score)}</td>
          <td>{item.evaluation == null ? "—" : `${item.evaluation.evaluator_kind} / ${item.evaluation.evaluator_version}`}</td>
          <td>{item.evaluation?.reason ?? item.error_type ?? "—"}</td>
          <td>{formatLatency(item.latency_ms)}</td>
          <td>{item.input_tokens ?? "—"} / {item.output_tokens ?? "—"} / {item.total_tokens ?? "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </>;
}

export function QualityEvidence({ snapshot, selectedRun }: {
  snapshot: QualitySnapshot; selectedRun?: BenchmarkRunSummary | null;
}) {
  const differentSuite = selectedRun && (
    selectedRun.suite_id !== snapshot.suite_id || selectedRun.suite_version !== snapshot.suite_version
    || selectedRun.suite_fingerprint !== snapshot.suite_fingerprint
  );
  return <>
    <p className="benchmarkNote">Current evidence aggregated by the resolver for the configured suite. This is not the total score of one historical run; each case may use its latest attempt from a different run.</p>
    {differentSuite && <p className="emptyNotice" role="status">The quality panel uses the configured suite, not this historical run&apos;s suite.</p>}
    <dl className="benchmarkMetadata">
      <div><dt>Target</dt><dd>{snapshot.provider} / {snapshot.model}</dd></div>
      <div><dt>Configured suite</dt><dd>{snapshot.suite_id} / {snapshot.suite_version}</dd></div>
      <div><dt>Fingerprint</dt><dd>{snapshot.suite_fingerprint}</dd></div>
      <div><dt>Snapshot calculated at</dt><dd>{formatDateTime(snapshot.generated_at)}</dd></div>
    </dl>
    <div className="benchmarkScores">
      <article><span>Benchmark score (0–1)</span><strong>{formatScore(snapshot.quality_score)}</strong><small>Evaluated answers only, including wrong answers.</small></article>
      <article><span>Confidence</span><strong>{formatPercent(snapshot.confidence)}</strong><small>Heuristic weight for quality blending, not a statistical confidence interval.</small></article>
      <article><span>Coverage</span><strong>{formatPercent(snapshot.coverage)}</strong><small>Proportion of unique cases attempted.</small></article>
      <article><span>Execution completeness</span><strong>{formatPercent(snapshot.execution_completeness)}</strong><small>Proportion of unique cases evaluated; wrong answers count.</small></article>
    </div>
    {snapshot.confidence === 0 && <p className="emptyNotice" role="status">
      {snapshot.quality_score == null ? "No evaluated quality evidence." : "A benchmark score is available."} Evidence is insufficient to replace static quality (confidence 0%).
    </p>}
    <dl className="benchmarkMetadata">
      <div><dt>Unique cases</dt><dd>{snapshot.total_cases} total · {snapshot.observed_cases} observed · {snapshot.evaluated_cases} evaluated · {snapshot.execution_failed_cases} execution failures</dd></div>
      <div><dt>Source run count</dt><dd>{snapshot.source_run_count} contributing runs, not independent cases</dd></div>
      <div><dt>Latest run ID</dt><dd>{snapshot.latest_run_id ?? "—"}</dd></div>
      <div><dt>Latest run completeness</dt><dd>{formatPercent(snapshot.latest_run_completeness)} — older evidence may fill gaps; not all evidence is from the latest run.</dd></div>
    </dl>
    <details className="benchmarkProvenance">
      <summary>Source run IDs ({snapshot.source_run_count})</summary>
      {snapshot.source_run_ids.length === 0 ? <p>—</p> : <ul>{snapshot.source_run_ids.map(id => <li key={id}>{id}</li>)}</ul>}
    </details>
    <h3>Category evidence</h3>
    <p className="benchmarkNote">Display only: production routing uses overall quality, not automatic task-category selection.</p>
    <div className="tableWrap" tabIndex={0} role="region" aria-label="Scrollable category evidence">
      <table><caption className="srOnly">Quality evidence by category</caption>
        <thead><tr><th>Category</th><th>Benchmark score</th><th>Confidence</th><th>Coverage</th><th>Completeness</th><th>Total / observed / evaluated / failed</th></tr></thead>
        <tbody>{snapshot.categories.map(category => <tr key={category.category}>
          <td>{category.category}</td><td>{formatScore(category.quality_score)}</td>
          <td>{formatPercent(category.confidence)}</td><td>{formatPercent(category.coverage)}</td>
          <td>{formatPercent(category.execution_completeness)}</td>
          <td>{category.total_cases} / {category.observed_cases} / {category.evaluated_cases} / {category.execution_failed_cases}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </>;
}
