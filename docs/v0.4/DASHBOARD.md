# V04-008 Benchmark Dashboard (development)

Status: implementation and automated checks complete; acceptance **PARTIAL** because browser tools
could not start. V0.4 has not been released. No backend endpoint, schema, scoring, Provider, circuit
or CLI behavior changes are part of this task.

## Usage and meaning

- Benchmark Runs loads the latest 20 matching summaries, not all history. Enter provider, model,
  suite ID and/or version, then Apply filters. Typing does not issue requests. The API applies filters
  before its limit; no client-side leaderboard or aggregation is calculated.
- View details fetches only the selected run. Details show its exact identity, UTC times, saved run
  config and case-index-ordered results. Completed means evaluated, not correct: score 0 is displayed
  as 0.000. Execution failure has no evaluation and is Unscored, never fabricated zero. Early stopping
  does not invent unattempted cases. No prompts/raw completions are available.
- Benchmark Quality requires an explicit provider/model submitted with Query quality. The detail
  panel can copy its target via Query current quality for this target. Arbitrary explicit targets
  remain available even when absent from the latest list; auto is not supported.
- Quality is current resolver evidence for the configured suite, not one historical run's grade.
  The actual suite ID/version/fingerprint are visible. A mismatch with the selected historical run
  is called out, including fingerprint-only differences. The UI never passes historical suite
  parameters to the quality endpoint or switches the server's configured suite.
- Benchmark score is 0..1, not universal accuracy. Confidence is the backend's heuristic blending
  weight, not a statistical confidence interval. Coverage counts attempted unique cases; execution
  completeness counts evaluated unique cases, including wrong answers. No formula is recomputed.
- Category evidence is display-only; production routing uses overall quality. Source run count is
  not independent sample count. Full source IDs, latest run ID and latest completeness remain visible
  or expandable, exposing historical evidence carried forward after a partial recent run.
- Snapshot calculated at is generated_at in UTC, not the model's last test time. All benchmark time
  formatting uses the existing formatter, now explicitly labelled UTC. Null/undefined is a dash;
  numeric zero is retained. Smoke score=1/confidence=0 explicitly warns that evidence cannot replace
  static quality.

Benchmark execution is an explicit local CLI action and may consume Provider API credits. There is
no Run/Retry button, key entry, shell/CLI invocation, write request or quality-suite control in the UI.
The page uses only the existing API URL configuration and GET endpoints. No mock fallback is shipped
in production; test fixtures live exclusively under frontend/tests.

## Loading, errors and Refresh

Runs, details and quality have separate request slots. Initial load fetches only runs; it does not
fetch all details or aggregate every model. Each selection cancels/invalidate its prior request,
including old errors; unmount cancels outstanding work. The shared helper has a 10-second deadline
covering both response headers and JSON body. Timeouts release loading and display a safe error.

Refresh dashboard reloads Metrics/Health plus applied run filters, selected details and selected
quality target. Unsaved input drafts are not applied by Refresh. Benchmark requests do not block
existing metrics updates or disable Refresh. No polling or WebSocket exists.

| Response | UI |
| --- | --- |
| Runs 200 [] | No benchmark runs found; distinguishes unfiltered absence from no filter matches |
| Detail 404 | Run details not found |
| Quality 404 + no_quality_evidence | No evidence for this target in the configured suite |
| Quality 503 + quality_suite_not_configured | Configure MODELPILOT_QUALITY_SUITE_PATH; no server value/path displayed |
| Other HTTP/network/timeout failure | Unable to load benchmark data; Refresh can retry |

Only allowlisted detail.code is retained. Raw exception text, stack traces and arbitrary server error
payloads are not rendered. All data text is React-escaped. Semantic labels accompany colors; labelled
forms/buttons, keyboard-focusable scrolling tables and normal disclosure controls are provided.

## Automated validation evidence (2026-09-21)

- Backend pytest: 990 passed, 2 existing dependency deprecation warnings; Ruff: passed.
- Frontend npm test: 50 passed. Node's built-in runner and installed TypeScript/React are reused,
  with no added dependencies or install scripts.
- npm run lint, npm run typecheck, npm run build: passed.
- Helper tests cover encoded filters/targets/IDs, direct JSON contracts, error codes, cancellations,
  body-inclusive timeout, stale result/loading protection, independent slots, UTC and missing/zero.
- React static-render tests cover empty/list/detail/quality semantics, category/provenance notices,
  safe states, escaping and retained original Dashboard sections.
- Fixture data were checked against existing backend response/domain models. Fixtures use no DB,
  Provider, credential or real model output.

These are helper/static-render tests, **not browser interaction or layout tests**. They do not prove
actual clicks, focus, viewport overflow, console/network behavior or the full mounted effect lifecycle.
BROWSER_INTERACTION = NOT_RUN. Desktop/narrow visual acceptance and screenshots = NOT_RUN.
Browser/Node UI tooling failed during startup with a sandbox helper setup error / kernel exit.
No permission or organization restriction was bypassed to produce preview evidence.

## Repeatable isolated browser acceptance (pending)

Use two local terminals in frontend; these commands affect only this explicit test session, not
production service configuration. No populated .env or user database is used or modified.

Terminal 1:

```powershell
npm run test:fixtures
```

Terminal 2:

```powershell
$env:NEXT_PUBLIC_MODELPILOT_API_URL = 'http://127.0.0.1:8765'
npm run dev -- --hostname 127.0.0.1 --port 3100
```

Open http://127.0.0.1:3100. Every screenshot must be captioned **TEST DATA**, never real model quality.
The fixture API is loopback-only and separate from the product. Stop both processes when finished;
remove the temporary process environment variable before starting against a real API.

1. At 1440px and 390px widths, verify original Summary/Health/Routing/Failures and new panels.
   No whole-page overflow; case/category tables should scroll horizontally and be keyboard focusable.
2. Inspect Network: initial load has one run-list fetch per active mount, no detail/quality request.
   React development Strict Mode may cancel its first mount request; this is not polling.
3. Type a filter without submitting: no new request. Apply openai + test-model-a +
   historical-test-data + version 1: only fixture-run-a. Apply unknown provider: filtered empty state.
   Clear/apply to recover the list. Check URL-encoded combined filters.
4. View fixture-run-a: confirm 0.000, 1.000 and Unscored/authentication_error, only three case rows
   despite four total cases, saved config and UTC timestamps. fixture-run-b includes timeout.
5. Query current quality for that target: configured-test-data v2 differs from the historical suite.
   Confirm separate score/confidence/coverage/completeness, latest completeness 20%, source run IDs
   and snapshot-calculated timestamp. Expand provenance. Category display is not a routing claim.
6. Enter provider openai and model smoke: score 1.000 with confidence 0% and insufficient-evidence
   notice. Enter no-evidence, not-configured, error, server-error and network-error to exercise their
   distinct states. SECRET_TEST_MARKER or /private/database.sqlite must never appear in the page.
7. Submit slow-a then immediately fast-b: late A must not replace B or clear B's loading state.
   Submit timeout: error within about 10 seconds; other sections stay usable. Navigate away while
   loading and verify no stale completion warning.
8. Change input drafts without submitting, then Refresh: use applied filters/selected target, not
   drafts; selected run details also reload. Editing/submitting only quality must not refetch all rows.
9. Detail-404 and unfiltered-empty UI states have static tests; additionally use browser local response
   overrides to return 404 for the clicked detail and [] for the unfiltered list (test origin only).
10. Inspect console/network for unexpected failures (deliberately injected failures excepted).
    Product UI requests must all be GET and have no execution endpoint. Read fixture request journal
    at http://127.0.0.1:8765/__test/requests; reset it through /__test/reset before a scenario if useful.

Record actual browser/version, desktop/narrow screenshots, console/network observations and any
defects before changing the task's acceptance from PARTIAL to PASS. Do not substitute an HTTP 200
or successful production build for this acceptance.
