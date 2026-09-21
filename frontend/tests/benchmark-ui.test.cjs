/* eslint-disable @typescript-eslint/no-require-imports */
// Static React render tests, NOT browser interaction or layout acceptance.
const { test, after } = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { installTsHook } = require("./load-ts.cjs");
const restore = installTsHook();
const { RunsView, RunDetailsView, QualityEvidence } = require("../components/benchmark-evidence.tsx");
const { default: BenchmarkDashboard, BenchmarkState } = require("../components/benchmark-dashboard.tsx");
const { default: Dashboard } = require("../app/page.tsx");
const { ApiError } = require("../lib/api.ts");
const { runSummaries, runDetail, quality, qualityFor } = require("./fixtures.cjs");
after(restore);
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

for (const filtered of [false, true]) test(`empty list distinguishes applied filters: ${filtered}`, () => {
  const html = render(RunsView, { runs: [], filtered, onSelect() {} });
  assert.match(html, /No benchmark runs found\./);
  assert.match(html, filtered ? /No matches for the applied filters/ : /No benchmark runs have been recorded/);
});

test("run summaries are recent records and have accessible on-demand detail buttons", () => {
  const html = render(RunsView, { runs: runSummaries, filtered: false, onSelect() {} });
  assert.match(html, /up to 20 matching records/);
  assert.match(html, /View details for fixture-run-a/);
  assert.match(html, /View details for fixture-run-b/);
  assert.match(html, /completed_with_failures/);
  assert.match(html, /UTC/);
  assert.doesNotMatch(html, /test-case-0/);
});

test("details preserve real zero and distinguish unscored execution failures", () => {
  const html = render(RunDetailsView, { run: runDetail });
  assert.match(html, /<td>0\.000<\/td>/);
  assert.match(html, /<td>Unscored<\/td>/);
  assert.match(html, /Execution failed/);
  assert.match(html, /authentication_error/);
  assert.match(html, /mismatch/);
  assert.match(html, /<td>0 \/ 0 \/ 0<\/td>/);
  assert.match(html, /— \/ — \/ —/);
  assert.match(html, /unattempted cases have no results/);
  assert.doesNotMatch(html, /test-case-3/);
  assert.match(html, /Saved run configuration/);
  assert.match(html, /Max tokens/);
});

test("case_index ordering is deterministic without mutating the supplied result", () => {
  const run = structuredClone(runDetail);
  run.case_results.reverse();
  const html = render(RunDetailsView, { run });
  assert.ok(html.indexOf("test-case-0") < html.indexOf("test-case-1"));
  assert.ok(html.indexOf("test-case-1") < html.indexOf("test-case-2"));
  assert.equal(run.case_results[0].case_index, 2);
});

test("current quality exposes distinct backend metrics, source and time semantics", () => {
  const html = render(QualityEvidence, { snapshot: quality, selectedRun: runSummaries[0] });
  for (const phrase of ["Benchmark score (0–1)", "Confidence", "Coverage", "Execution completeness",
    "Heuristic weight for quality blending, not a statistical confidence interval.",
    "Snapshot calculated at", "Source run count", "contributing runs, not independent cases",
    "Latest run completeness", "older evidence may fill gaps", "fixture-quality-old",
    "Display only: production routing uses overall quality", "configured-test-data"]) {
    assert.ok(html.includes(phrase), phrase);
  }
  assert.match(html, /not this historical run/);
  assert.match(html, /20%/);
  assert.doesNotMatch(html, /accuracy|last tested|leaderboard/i);
});

test("matching suite identity does not display a false mismatch", () => {
  const selectedRun = { ...runSummaries[0], suite_id: quality.suite_id,
    suite_version: quality.suite_version, suite_fingerprint: quality.suite_fingerprint };
  assert.doesNotMatch(render(QualityEvidence, { snapshot: quality, selectedRun }), /not this historical run/);
});

test("fingerprint alone is sufficient for a suite mismatch warning", () => {
  const selectedRun = { ...runSummaries[0], suite_id: quality.suite_id, suite_version: quality.suite_version };
  assert.match(render(QualityEvidence, { snapshot: quality, selectedRun }), /not this historical run/);
});

test("smoke score1 and confidence0 remain simultaneously visible", () => {
  const html = render(QualityEvidence, { snapshot: qualityFor("openai", "smoke") });
  assert.match(html, /1\.000/);
  assert.match(html, /<strong>0%<\/strong>/);
  assert.match(html, /A benchmark score is available/);
  assert.match(html, /Evidence is insufficient to replace static quality/);
});

test("null quality and zero counts are not fabricated scores", () => {
  const snapshot = { ...quality, quality_score: null, confidence: 0, observed_cases: 0,
    evaluated_cases: 0, source_run_count: 0, source_run_ids: [], latest_run_id: null,
    latest_run_completeness: null, categories: [] };
  const html = render(QualityEvidence, { snapshot });
  assert.match(html, /<strong>—<\/strong>/);
  assert.match(html, /0 observed · 0 evaluated/);
  assert.match(html, /0 contributing runs/);
  assert.match(html, /No evaluated quality evidence/);
});

for (const [kind, status, code, expected] of [
  ["detail", 404, "benchmark_run_not_found", /Run details not found/],
  ["quality", 404, "no_quality_evidence", /No quality evidence/],
  ["quality", 503, "quality_suite_not_configured", /Set MODELPILOT_QUALITY_SUITE_PATH/],
  ["quality", 503, "benchmark_data_unavailable", /Unable to load benchmark data/],
  ["quality", 500, "quality_suite_not_configured", /Unable to load benchmark data/],
  ["quality", 404, "request_failed", /Unable to load benchmark data/],
  ["runs", 503, "quality_suite_not_configured", /Unable to load benchmark data/],
  ["quality", null, "request_failed", /Unable to load benchmark data/],
  ["quality", null, "request_timeout", /Unable to load benchmark data/],
]) test(`safe state mapping: ${kind}/${status}/${code}`, () => {
  assert.match(render(BenchmarkState, { kind, state: { status: "error", error: new ApiError(status, code) } }), expected);
});

test("raw server errors cannot leak into rendered UI", () => {
  const html = render(BenchmarkState, { kind: "quality", state: {
    status: "error", error: new Error("SECRET_TEST_MARKER Authorization: Bearer test /private/db.sqlite"),
  } });
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /SECRET|Authorization|Bearer|private|sqlite/);
});

test("server strings render as escaped text, not HTML", () => {
  const run = { ...runDetail, model: '<script>alert("test")</script>' };
  const html = render(RunDetailsView, { run });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("initial benchmark UI requests no target/detail and provides labelled controls", () => {
  const html = render(BenchmarkDashboard, { apiUrl: "", refreshKey: 0 });
  for (const phrase of ["Provider filter", "Model filter", "Suite ID filter", "Suite version filter",
    "Apply filters", "Quality provider", "Quality model", "Select a run", "Choose an explicit",
    "may consume Provider API credits"]) assert.ok(html.includes(phrase), phrase);
  assert.match(html, /Loading benchmark data/);
  assert.doesNotMatch(html, /Run Benchmark|Retry Benchmark|API Key|fixture-run/);
});

test("existing Dashboard sections and shared Refresh are retained", () => {
  const html = render(Dashboard);
  for (const heading of ["Gateway summary", "Provider Health", "Provider metrics", "Routing decisions",
    "Recent failures", "Benchmark Runs", "Run Details", "Benchmark Quality"]) assert.ok(html.includes(heading));
  assert.match(html, /Refreshing/);
});
