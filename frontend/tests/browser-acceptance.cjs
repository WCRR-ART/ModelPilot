/* eslint-disable @typescript-eslint/no-require-imports */
// TEST DATA ONLY. Real Chromium interaction, separated from mocked-state and real-backend checks.
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium, expect } = require("playwright/test");
const { fixtures, runSummaries, runs, qualityFor } = require("./fixtures.cjs");

const base = process.env.ACCEPTANCE_URL ?? "http://127.0.0.1:3100";
const backend = process.env.ACCEPTANCE_API_URL ?? "http://127.0.0.1:8766";
const allowedOrigins = new Set([new URL(base).origin, new URL(backend).origin]);
const longRunId = "test-long-".repeat(60);
const longModel = "TEST-DATA-".repeat(60);
const artifacts = path.resolve(process.env.ACCEPTANCE_ARTIFACTS ?? "../.artifacts/v04-009-browser");
const results = [];
const pageErrors = [];
const consoleErrors = [];
const network = [];
let activePage;
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function check(name, operation) {
  const start = Date.now();
  await operation();
  results.push({ name, result: "PASS", duration_ms: Date.now() - start });
  console.log(`PASS ${name}`);
}

async function monitoredContext(browser, mocked) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
  context.on("page", page => {
    page.on("pageerror", error => pageErrors.push({ mode: mocked ? "state" : "real", message: error.message }));
    page.on("console", message => { if (message.type() === "error") consoleErrors.push({ mode: mocked ? "state" : "real", message: message.text() }); });
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname.startsWith("/v1/") || url.pathname === "/health") network.push({ mode: mocked ? "state" : "real", method: request.method(), path: url.pathname, query: url.search });
    });
  });
  return context;
}

async function query(page, model, provider = "openai") {
  await page.getByLabel("Quality provider", { exact: true }).fill(provider);
  await page.getByLabel("Quality model", { exact: true }).fill(model);
  await page.getByRole("button", { name: "Query quality", exact: true }).click();
}

async function consumed(page, model, count = 1) {
  await expect.poll(() => page.evaluate(name => window.__acceptanceConsumed.filter(item => item === name).length, model)).toBe(count);
  // The JSON body is consumed; allow the old promise chain and React render to finish.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function main() {
  await fs.mkdir(artifacts, { recursive: true });
  for (const value of [base, backend]) assert.equal(new URL(value).hostname, "127.0.0.1", "acceptance services must be loopback");
  const browser = await chromium.launch({
    headless: true, chromiumSandbox: true,
    ...(process.env.ACCEPTANCE_BROWSER_PATH ? { executablePath: process.env.ACCEPTANCE_BROWSER_PATH } : {}),
  });
  const evidence = { test_data: true, browser: browser.version(), viewports: ["1440x1000", "390x844"], results, pageErrors, consoleErrors, network };
  let stateContext;
  let realContext;
  try {
    stateContext = await monitoredContext(browser, true);
    const state = { empty: false, missing: false, long: false, gates: {} };
    const arrivals = new Map();
    await stateContext.addInitScript(() => {
      // Test a transport that ignores cancellation: stale completion must STILL not update React.
      const original = window.fetch.bind(window);
      window.__acceptanceConsumed = [];
      window.fetch = (url, options) => {
        if (String(url).includes("model=race-")) {
          const copy = { ...options }; delete copy.signal;
          return original(url, copy).then(response => {
            const json = response.json.bind(response);
            response.json = async () => {
              const body = await json();
              window.__acceptanceConsumed.push(new URL(String(url)).searchParams.get("model"));
              return body;
            };
            return response;
          });
        }
        return original(url, options);
      };
    });
    await stateContext.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (!allowedOrigins.has(url.origin)) return route.abort("blockedbyclient");
      if (!url.pathname.startsWith("/v1/") && url.pathname !== "/health") return route.continue();
      assert.equal(route.request().method(), "GET", "UI must never execute or mutate");
      const send = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (fixtures[url.pathname]) return send(200, fixtures[url.pathname]);
      if (url.pathname === "/v1/benchmarks/runs") {
        const selected = state.empty ? [] : runSummaries.filter(run => ["provider", "model", "suite_id", "suite_version"].every(key => !url.searchParams.has(key) || run[key] === url.searchParams.get(key)));
        return send(200, selected.map((run, index) => state.long && index === 0 ? { ...run, model: longModel, run_id: longRunId } : run));
      }
      if (url.pathname.startsWith("/v1/benchmarks/runs/")) {
        if (state.long && url.pathname.endsWith(longRunId)) return send(200, { ...runs[0], run_id: longRunId, model: longModel, case_results: runs[0].case_results.map(item => ({ ...item, model: longModel })) });
        const run = runs.find(item => url.pathname.endsWith(item.run_id));
        return state.missing || !run ? send(404, { detail: { code: "benchmark_run_not_found" } }) : send(200, run);
      }
      if (url.pathname === "/v1/benchmarks/quality") {
        const model = url.searchParams.get("model");
        const provider = url.searchParams.get("provider");
        if (arrivals.has(model)) arrivals.get(model).resolve();
        if (state.gates[model]) await state.gates[model].promise;
        if (model === "no-evidence") return send(404, { detail: { code: "no_quality_evidence" } });
        if (model === "not-configured") return send(503, { detail: { code: "quality_suite_not_configured" } });
        if (["error", "server-error", "race-error"].includes(model)) return send(model === "error" ? 503 : 500, { detail: { code: "benchmark_data_unavailable" }, debug: "SECRET_TEST_MARKER /private/db" });
        if (model === "network-error") return route.abort("failed");
        const snapshot = qualityFor(provider, model);
        if (model === longModel) Object.assign(snapshot, { source_run_ids: [longRunId, "fixture-quality-old"], latest_run_id: longRunId });
        return send(200, snapshot);
      }
      return send(404, { detail: { code: "not_found" } });
    });
    const page = await stateContext.newPage(); activePage = page;
    const runPanel = page.getByRole("region", { name: "Benchmark Runs", exact: true });
    const detail = page.getByRole("region", { name: "Run Details", exact: true });
    const quality = page.getByRole("region", { name: "Benchmark Quality", exact: true });
    await page.goto(base);
    await check("initial read-only list; details and quality remain on demand", async () => {
      await expect(page.getByRole("button", { name: "View details for fixture-run-a", exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: "Refresh dashboard" })).toBeEnabled();
      assert.equal(network.filter(r => r.path === "/v1/benchmarks/quality").length, 0);
      assert.equal(network.filter(r => r.path.startsWith("/v1/benchmarks/runs/")).length, 0);
      await expect(runPanel).toContainText("LATEST 20 MATCHING RUNS");
      await page.getByRole("button", { name: "Refresh dashboard" }).click();
      await expect(page.getByRole("button", { name: "Refresh dashboard" })).toBeEnabled();
      assert.equal(network.filter(r => r.path === "/v1/benchmarks/quality").length, 0);
    });
    await check("four filters apply only on submit; keyboard Tab and Enter", async () => {
      const count = network.length;
      await page.getByLabel("Provider filter", { exact: true }).fill("openai");
      await page.getByLabel("Provider filter", { exact: true }).press("Tab");
      await expect(page.getByLabel("Model filter", { exact: true })).toBeFocused();
      await page.getByLabel("Model filter", { exact: true }).fill("test-model-a");
      await page.getByLabel("Suite ID filter", { exact: true }).fill("historical-test-data");
      await page.getByLabel("Suite version filter", { exact: true }).fill("1");
      assert.equal(network.length, count);
      await page.getByLabel("Suite version filter", { exact: true }).press("Enter");
      await expect(runPanel.getByRole("button", { name: /View details/ })).toHaveCount(1);
      const search = new URLSearchParams(network.filter(r => r.path === "/v1/benchmarks/runs").at(-1).query);
      assert.deepEqual(Object.fromEntries(search), { limit: "20", provider: "openai", model: "test-model-a", suite_id: "historical-test-data", suite_version: "1" });
    });
    await check("empty vs filtered empty and clearing filters", async () => {
      await page.getByLabel("Provider filter", { exact: true }).fill("missing");
      await page.getByRole("button", { name: "Apply filters" }).click();
      await expect(runPanel).toContainText("No matches for the applied filters.");
      state.empty = true;
      for (const label of ["Provider filter", "Model filter", "Suite ID filter", "Suite version filter"]) await page.getByLabel(label, { exact: true }).fill("");
      await page.getByRole("button", { name: "Apply filters" }).click();
      await expect(runPanel).toContainText("No benchmark runs have been recorded yet.");
      state.empty = false;
      await page.getByRole("button", { name: "Apply filters" }).click();
      await expect(runPanel.getByRole("button", { name: /View details/ })).toHaveCount(2);
    });
    await check("details zero vs execution failure, order and no fabricated cases", async () => {
      const button = page.getByRole("button", { name: "View details for fixture-run-a", exact: true });
      await button.focus(); await button.press("Enter");
      await expect(detail.getByRole("cell", { name: "0.000", exact: true })).toBeVisible();
      await expect(detail.getByRole("cell", { name: "Unscored", exact: true })).toBeVisible();
      await expect(detail.locator("tbody tr")).toHaveCount(3);
      assert.deepEqual(await detail.locator("tbody tr td:first-child").allTextContents(), ["0 / test-case-0", "1 / test-case-1", "2 / test-case-2"]);
      await expect(detail).toContainText("unattempted cases have no results");
      await detail.getByText("Saved run configuration", { exact: true }).click();
      await expect(detail).toContainText("Max tokens");
      await detail.screenshot({ path: path.join(artifacts, "TEST-DATA-details.png") });
    });
    await check("detail 404 is isolated", async () => {
      state.missing = true;
      await page.getByRole("button", { name: "View details for fixture-run-b", exact: true }).click();
      await expect(detail).toContainText("Run details not found");
      await expect(page.getByRole("heading", { name: "Gateway summary" })).toBeVisible();
      state.missing = false;
      await page.getByRole("button", { name: "View details for fixture-run-a", exact: true }).click();
      await expect(detail).toContainText("One historical execution");
    });
    await check("quality semantics, mismatch, sources and latest completeness", async () => {
      await page.getByRole("button", { name: "Query current quality for this target" }).click();
      await expect(quality).toContainText("not this historical run's suite");
      for (const text of ["Benchmark score (0–1)", "Heuristic weight", "Coverage", "Execution completeness", "Snapshot calculated at", "Latest run completeness", "20%", "overall quality"]) await expect(quality).toContainText(text);
      await quality.getByText("Source run IDs (2)", { exact: true }).click();
      await expect(quality.getByText("fixture-quality-old", { exact: true })).toBeVisible();
      await quality.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(artifacts, "TEST-DATA-quality-desktop.png") });
    });
    await check("smoke exposes score 1 with confidence 0", async () => {
      await query(page, "smoke");
      await expect(quality).toContainText("insufficient to replace static quality (confidence 0%)");
      await expect(quality.locator(".benchmarkScores strong").first()).toHaveText("1.000");
      await expect(quality.locator(".benchmarkScores strong").nth(1)).toHaveText("0%");
    });
    await check("404, configured-suite 503, other 503/500, network isolation", async () => {
      for (const [model, message] of [["no-evidence", "No quality evidence"], ["not-configured", "Quality suite not configured"], ["error", "Unable to load benchmark data"], ["server-error", "Unable to load benchmark data"], ["network-error", "Unable to load benchmark data"]]) {
        await query(page, model); await expect(quality).toContainText(message);
        await expect(page.getByRole("button", { name: "Refresh dashboard" })).toBeEnabled();
        assert.equal((await page.locator("body").innerText()).includes("SECRET_TEST_MARKER"), false);
        if (model === "not-configured") await page.screenshot({ path: path.join(artifacts, "TEST-DATA-unconfigured.png") });
      }
    });
    await check("late A cannot overwrite B or clear pending B loading", async () => {
      state.gates["race-a"] = deferred(); state.gates["race-b"] = deferred();
      arrivals.set("race-a", deferred()); arrivals.set("race-b", deferred());
      await query(page, "race-a"); await arrivals.get("race-a").promise;
      await query(page, "race-b"); await arrivals.get("race-b").promise;
      state.gates["race-a"].resolve();
      await consumed(page, "race-a");
      await expect(quality).toContainText("Loading benchmark data");
      state.gates["race-b"].resolve();
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("race-b");
      // A second race explicitly returns A after B.
      state.gates["race-a"] = deferred(); arrivals.set("race-a", deferred());
      await query(page, "race-a"); await arrivals.get("race-a").promise;
      await query(page, "fast-b");
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("fast-b");
      const returned = page.waitForResponse(response => response.url().includes("model=race-a"));
      state.gates["race-a"].resolve(); await returned;
      await consumed(page, "race-a", 2);
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("fast-b");
      state.gates["race-error"] = deferred(); state.gates["race-b"] = deferred();
      arrivals.set("race-error", deferred()); arrivals.set("race-b", deferred());
      await query(page, "race-error"); await arrivals.get("race-error").promise;
      await query(page, "race-b"); await arrivals.get("race-b").promise;
      state.gates["race-error"].resolve(); await consumed(page, "race-error");
      await expect(quality).toContainText("Loading benchmark data");
      await expect(quality).not.toContainText("Unable to load benchmark data");
      state.gates["race-b"].resolve();
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("race-b");
    });
    await check("hung request expires and Refresh remains usable", async () => {
      state.gates.timeout = deferred();
      await query(page, "timeout");
      await expect(quality).toContainText("Loading benchmark data");
      await expect(quality).toContainText("Unable to load benchmark data", { timeout: 13000 });
      await expect(page.getByRole("button", { name: "Refresh dashboard" })).toBeEnabled();
      state.gates.timeout.resolve();
      await query(page, "fast-b");
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("fast-b");
    });
    await check("Refresh preserves applied filters and target, not unsaved drafts", async () => {
      await page.getByLabel("Provider filter", { exact: true }).fill("openai");
      await page.getByRole("button", { name: "Apply filters" }).click();
      await expect(runPanel.getByRole("button", { name: /View details/ })).toHaveCount(1);
      await page.getByLabel("Provider filter", { exact: true }).fill("UNSUBMITTED");
      await page.getByLabel("Quality model", { exact: true }).fill("UNSUBMITTED");
      const from = network.length;
      await page.getByRole("button", { name: "Refresh dashboard" }).click();
      await expect(page.getByRole("button", { name: "Refresh dashboard" })).toBeEnabled();
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText("fast-b");
      const refreshed = network.slice(from);
      for (const expectedPath of ["/health", "/v1/health/providers", "/v1/metrics/summary", "/v1/benchmarks/runs", "/v1/benchmarks/runs/fixture-run-a", "/v1/benchmarks/quality"]) assert.ok(refreshed.some(r => r.path === expectedPath), expectedPath);
      assert.ok(refreshed.some(r => r.path === "/v1/benchmarks/runs" && r.query.includes("provider=openai")));
      assert.ok(refreshed.some(r => r.path === "/v1/benchmarks/quality" && r.query.includes("model=fast-b")));
      assert.ok(refreshed.every(r => !r.query.includes("UNSUBMITTED")));
    });
    await check("desktop/narrow long identities do not overflow; keyboard focus visible", async () => {
      state.long = true;
      await page.getByRole("button", { name: "Refresh dashboard" }).click();
      await expect(runPanel).toContainText("TEST-DATA-");
      await page.getByRole("button", { name: `View details for ${longRunId}`, exact: true }).click();
      await expect(detail).toContainText(longRunId);
      await page.getByRole("button", { name: "Query current quality for this target" }).click();
      await expect(quality.locator(".benchmarkMetadata").first()).toContainText(longModel);
      await quality.getByText("Source run IDs (2)", { exact: true }).click();
      await expect(quality).toContainText(longRunId);
      for (const size of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
        await page.setViewportSize(size);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `whole page overflow at ${size.width}`);
      }
      await runPanel.scrollIntoViewIfNeeded();
      const scroller = runPanel.getByRole("region", { name: "Scrollable recent benchmark runs" });
      assert.ok(await scroller.evaluate(el => el.scrollWidth > el.clientWidth));
      await scroller.focus(); await scroller.press("ArrowRight");
      await expect.poll(() => scroller.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
      assert.notEqual(await scroller.evaluate(el => getComputedStyle(el).outlineStyle), "none");
      await page.screenshot({ path: path.join(artifacts, "TEST-DATA-narrow.png") });
      await detail.screenshot({ path: path.join(artifacts, "TEST-DATA-long-details-narrow.png") });
      await quality.screenshot({ path: path.join(artifacts, "TEST-DATA-long-quality-narrow.png") });
      state.long = false;
    });
    await stateContext.tracing.stop({ path: path.join(artifacts, "TEST-DATA-state-trace.zip") });
    await stateContext.close(); stateContext = null;

    realContext = await monitoredContext(browser, false);
    await realContext.route("**/*", route => allowedOrigins.has(new URL(route.request().url()).origin) ? route.continue() : route.abort("blockedbyclient"));
    const real = await realContext.newPage(); activePage = real;
    await check("real frontend -> real API -> temporary SQLite -> fake Runner evidence", async () => {
      await real.goto(base);
      await real.getByRole("button", { name: "View details for test-data-full-openai", exact: true }).click();
      const detail = real.getByRole("region", { name: "Run Details", exact: true });
      await expect(detail.locator("tbody tr")).toHaveCount(60);
      await real.getByRole("button", { name: "Query current quality for this target" }).click();
      const quality = real.getByRole("region", { name: "Benchmark Quality", exact: true });
      await expect(quality).toContainText("test-data-independent-60");
      await expect(quality.locator(".benchmarkScores strong").first()).toHaveText("1.000");
      await expect(quality.locator(".benchmarkScores strong").nth(1)).toHaveText("100%");
      const response = await realContext.request.get(`${backend}/v1/metrics/routing-decisions`);
      const decisions = await response.json();
      assert.ok(decisions.some(d => d.explanation.selected.benchmark_confidence === 1 && d.explanation.selected.benchmark_source_run_ids.includes("test-data-full-openai")));
      await real.getByRole("button", { name: "View details for test-data-mixed-gemini", exact: true }).click();
      await expect(detail.getByRole("cell", { name: "0.000", exact: true })).toBeVisible();
      await expect(detail.getByRole("cell", { name: "Unscored", exact: true }).first()).toBeVisible();
      await expect(detail).toContainText("unattempted cases have no results");
      await detail.screenshot({ path: path.join(artifacts, "TEST-DATA-real-backend.png") });
    });
    await check("no uncaught application/hydration errors or write/execution requests", async () => {
      assert.deepEqual(pageErrors, []);
      assert.ok(consoleErrors.every(item => item.mode === "state" && /Failed to load resource|net::ERR_FAILED/.test(item.message)), JSON.stringify(consoleErrors));
      assert.ok(network.every(item => item.method === "GET"));
      assert.ok(!network.some(item => /benchmarks\/(run|eval)$/.test(item.path)));
      assert.ok(network.length < 180, "unexpected request loop");
      await expect(real.locator("body")).not.toContainText("SECRET_TEST_MARKER");
    });
    await realContext.tracing.stop({ path: path.join(artifacts, "TEST-DATA-real-trace.zip") });
    evidence.status = "PASS";
  } catch (error) {
    evidence.status = "FAIL"; evidence.failure = error.message;
    if (activePage && !activePage.isClosed()) await activePage.screenshot({ path: path.join(artifacts, "TEST-DATA-failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    await fs.writeFile(path.join(artifacts, "results.json"), JSON.stringify(evidence, null, 2));
    if (stateContext) await stateContext.close();
    if (realContext) await realContext.close();
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
