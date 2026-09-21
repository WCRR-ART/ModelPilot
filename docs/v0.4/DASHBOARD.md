# V04-008 Benchmark Dashboard (development)

V04-008 baseline: implementation and static checks complete, browser acceptance **PARTIAL**.
V04-009 browser completion: **PASS**, 14 executed scenarios; evidence recorded below.
V0.4 has not been released. No new product feature, endpoint, schema, scoring rule or Provider is
introduced by Dashboard acceptance.

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
in production. UI fixtures live under frontend/tests; the isolated fake-provider acceptance backend
lives under scripts and is not the production application entry point.

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

## Historical V04-008 validation baseline (2026-09-21)

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
At that handoff, BROWSER_INTERACTION = NOT_RUN; desktop/narrow layout and screenshots = NOT_RUN.
Browser/Node UI tooling failed during startup with a sandbox helper setup error / kernel exit.
No permission or organization restriction was bypassed to produce preview evidence.

## V04-009 execution environment and current record

The normal execution/browser tool failed while starting its sandbox helper, before application
inspection. Approved local execution found an existing Chrome 153 browser and installed Playwright;
headless launch with `chromiumSandbox: true` succeeds. No global security policy was changed, browser
sandbox disabled, or duplicate browser downloaded. The initial isolated frontend hit a cross-drive
dependency-junction startup issue; the test-only launcher was corrected and full browser execution
then passed repeatedly, including after the 0.4.0 metadata update. This was a service harness issue,
not an application defect or browser launch failure.

| Acceptance item | Result |
| --- | --- |
| Actual browser interactions | PASS — 14 scenarios in `npm run test:browser` |
| Desktop 1440×1000 / narrow 390×844 | PASS — layout assertions and screenshot inspection |
| Keyboard/focus and table scrolling | PASS — Tab/Enter, visible focus and horizontal scroll |
| Delayed-response and loading races | PASS — older result/error cannot replace or clear the current state |
| Error/timeout/module isolation | PASS — includes actual browser deadline and retry |
| Real frontend → real backend → temporary SQLite → Fake Provider | PASS — separate unmocked API context |
| Console/network and no execution requests | PASS — 0 uncaught errors; all 81 recorded API requests GET |
| Screenshots/traces/results.json inspected | PASS — evidence saved separately from source control |
| Real cloud Provider calls | NOT_RUN; prohibited during acceptance |

Observed browser: **Chrome 153.0.8010.48**, Playwright headless with browser sandbox enabled.
The recorded 81 requests comprise 64 controlled-state and 17 real-backend page requests. Seven console
resource errors belong exclusively to deliberately injected state-test 404/503/500/network failures;
there are no uncaught page/hydration errors and no real-context console errors. The request bound
passed without an infinite loop; no execution or mutating UI request was issued. The timeout scenario
completed in about 10.2 seconds, and the Refresh control recovered.

The full backend reports 1003 passing tests, including two tests of the real fake-provider acceptance
harness. Frontend helper/static-rendering tests report 50 passing tests. These checks remain separate
from the executed browser evidence. See [COMPLETION.md](COMPLETION.md) for final release gates.

## Repeatable isolated browser acceptance

Use PowerShell 7, the installed backend environment and `frontend/node_modules`. The launcher
downloads nothing. From the repository root, Terminal 1:

```powershell
pwsh -File .\scripts\start-dashboard-acceptance.ps1 -DurationSeconds 900 -KeepArtifacts
```

The launcher creates a fresh direct child of the OS temporary directory for the backend test database
and logs, plus `.artifacts/acceptance-frontend-<GUID>` under the repository for the isolated frontend.
The same-drive frontend directory avoids the earlier dependency-junction problem. It prints the
resource path and starts only loopback services at `http://127.0.0.1:8766` (backend) and `http://127.0.0.1:3100`
(Dashboard). It rejects occupied ports rather than stopping existing services. It copies an
allowlisted frontend source set without any `.env`, supplies a restricted process environment without
Provider credentials, and uses a new test SQLite database. It does not inspect user database content.
Use `-BackendPort` / `-FrontendPort` only when necessary and match the URLs in the browser environment.

Terminal 2, from the repository root, with the path of an **existing** installed Chromium-compatible
browser (the example must exist locally):

```powershell
Set-Location frontend
$env:ACCEPTANCE_BROWSER_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
npm run test:browser
```

No browser download is part of this command. If using a different installed Chrome/Edge path, replace
only that explicit value. `ACCEPTANCE_URL` and `ACCEPTANCE_API_URL` default to the loopback URLs above;
set them only if launcher ports changed. The browser test refuses non-loopback acceptance service URLs
and blocks external page requests. It creates new isolated browser contexts, never reuses a personal
profile, and launches with Chromium sandbox enabled.

The command implements two distinct verification modes:

1. **Browser state tests:** real rendered frontend with controlled HTTP responses. This enables
   deterministic loading/404/503/network/timeout, stale-result/loading, long-identity and empty states.
   Fixture labels include TEST DATA. These tests do not claim a real API/database connection.
2. **Full connection test:** a new context sends unmocked Benchmark GET requests to the real local
   backend. Real Runner + Fake Provider + Evaluator generated and persisted the records. It reads
   60-case results, quality and stored production-route provenance through real API/SQLite components.
   The fake cloud-free provider is not a JSON substitute for that connection.

### Expected interactions and evidence

| Action | Expected result | Evidence to record |
| --- | --- | --- |
| Initial page load | Latest 20 matching runs; no eager detail/quality requests | Network requests and initial list assertion |
| Fill all four filters, then Apply/Enter | No requests on typing; exact combined encoded filters on submit | Request URL and resulting row count |
| Apply nonexistent filter, clear, then unfiltered empty | Distinct no-match/no-record messages; list recovers | Browser assertions and rendered state |
| Select a Run with Enter/click | On-demand details, saved config, ordered indexes | Request timing and case-row assertions |
| Inspect wrong answer, timeout, auth partial Run | Real 0.000 vs Unscored; no fabricated unattempted rows | Detail screenshot and case assertions |
| Select missing Run | Detail 404 state; other modules continue | Isolated 404 assertion |
| Query target/current quality | Separate quality/confidence/coverage/completeness; suite mismatch warning | Quality screenshot and provenance assertions |
| Expand source IDs | Source IDs/latest-run completeness trace old evidence | Expanded disclosure in screenshot/trace |
| Query smoke fixture | Quality 1.000, confidence 0%, insufficient-evidence notice | Exact score/confidence assertions |
| Query 404, configured-suite 503, other 503/500/network | Correct safe states; no raw secret/error payload | Allowlisted error assertions and expected failure log |
| Delay A, choose B, return A first or last | A cannot overwrite B or clear B's loading | Controlled response ordering and final target assertion |
| Leave a response hanging | About 10-second deadline, safe error, Refresh stays usable | Timed browser assertion and retry |
| Change unsaved drafts, then Refresh | Existing applied filters/target/details and metrics/health refresh | Network query journal; drafts absent from requests |
| Desktop/narrow and long identity fixture | No document overflow; tables scroll; focused controls visible | 1440×1000 and 390×844 screenshots/layout assertions |
| Tab, Enter and focused scroll region | Core actions keyboard-operable, not color-only | Focus/navigation assertions and trace |
| Inspect console/network | No uncaught/hydration errors, request loop or execution/write requests | Recorded errors separated from injected failures; all UI API methods GET |
| Real-backend target openai/test-model-a | 60 evaluated cases, quality/confidence 1, genuine stored provenance | Unmocked real API assertions and screenshot |
| Real-backend mixed Gemini Run | Wrong-answer zero, timeout/rate_limit/auth null evaluation, early stop | Actual backend/SQLite case details in browser |

Every screenshot is **TEST DATA**, not real model quality evidence. Default ignored artifacts are
`.artifacts/v04-009-browser/` relative to the repository root:

- `results.json`: browser version, viewports, scenario results and console/network observations;
- `TEST-DATA-*.png`: details, quality, configuration error, narrow view and real-backend screenshots,
  including `TEST-DATA-long-details-narrow.png` and `TEST-DATA-long-quality-narrow.png`;
- `TEST-DATA-state-trace.zip` and `TEST-DATA-real-trace.zip`: separate contexts and modes;
- `TEST-DATA-failure.png` when a scenario fails.

The launcher also retains `*.test-data.log` and the test database at its printed temporary path when
`-KeepArtifacts` is used. Review before sharing; do not commit SQLite/WAL/SHM, screenshots, traces,
build output or large test reports. Capture paths and actual results in this document instead.

### Stopping and cleanup

The sample launcher stops after 900 seconds; omit DurationSeconds for interactive Enter shutdown.
Let its cleanup finish rather than force-closing the terminal. It stops only its own process tree,
removes its dependency junction without following it,
then removes the validated `.artifacts/acceptance-frontend-<GUID>` copy, even with `-KeepArtifacts`.
It never stops a process merely because it occupies a port. With `-KeepArtifacts`, it prints the
exact directory-specific cleanup command after the owned services stop. Inspect the printed resolved
path and evidence first, then run only that command. Without `-KeepArtifacts`, the validated fresh
temporary directory is removed automatically. Clear temporary `ACCEPTANCE_*` environment settings
from Terminal 2 after inspection. A force-cancel of a tool/terminal can bypass PowerShell finally:
this was observed during acceptance. In that case, inspect only the printed session resource paths
and owned process IDs; never kill by a shared port/name or recursively remove a dependency junction.
Remove that session's junction itself before deleting its exact validated temporary frontend copy.
Timed shutdown was used for the final run to retain logs and perform normal cleanup.

An HTTP startup smoke check (`-SmokeTest`) proves only services/API availability, not browser behavior.
If legal available browser execution remains blocked, preserve PARTIAL and use this launcher for
actual operator acceptance; do not tick the table PASS on the operator's behalf.
