/* eslint-disable @typescript-eslint/no-require-imports */
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { installTsHook } = require("./load-ts.cjs");
const restore = installTsHook();
const api = require("../lib/api.ts");
const { LatestRequest } = require("../lib/latest-request.ts");
restore();

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

test("runs use one GET with default limit 20 and return the direct array", async (t) => {
  const rows = [{ run_id: "run-1", completed_cases: 0 }];
  const fetch = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "http://localhost:8000/v1/benchmarks/runs?limit=20");
    assert.equal(options.method, "GET");
    assert.equal(options.cache, "no-store");
    assert.ok(options.signal instanceof AbortSignal);
    return json(rows);
  });
  assert.deepEqual(await api.fetchBenchmarkRuns("http://localhost:8000"), rows);
  assert.equal(fetch.mock.callCount(), 1);
});

test("empty runs remain an empty successful array", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json([]));
  assert.deepEqual(await api.fetchBenchmarkRuns(""), []);
});

test("combined filters are safely encoded and cannot inject other query parameters", async (t) => {
  const filters = {
    provider: "other & provider", model: "model/a+b?limit=1000",
    suite_id: "suite 中文", suite_version: "v1#?x=y",
  };
  t.mock.method(globalThis, "fetch", async (url) => {
    const parsed = new URL(url, "http://localhost");
    assert.equal(parsed.searchParams.get("limit"), "20");
    for (const [key, value] of Object.entries(filters)) {
      assert.equal(parsed.searchParams.get(key), value);
    }
    assert.equal([...parsed.searchParams].length, 5);
    return json([]);
  });
  await api.fetchBenchmarkRuns("", filters);
});

test("blank optional filters are omitted and unknown filter keys are not forwarded", async (t) => {
  t.mock.method(globalThis, "fetch", async (url) => {
    assert.equal(url, "/v1/benchmarks/runs?limit=20");
    return json([]);
  });
  await api.fetchBenchmarkRuns("", { provider: "", model: undefined, limit: 1000 });
});

test("run detail is fetched only on explicit call with encoded ID and original case order", async (t) => {
  const detail = { run_id: "run A/&?", case_results: [
    { case_index: 0, case_id: "z", evaluation: { score: 0 } },
    { case_index: 1, case_id: "a", evaluation: null },
  ] };
  const fetch = t.mock.method(globalThis, "fetch", async (url) => {
    assert.equal(url, `/v1/benchmarks/runs/${encodeURIComponent(detail.run_id)}`);
    return json(detail);
  });
  assert.equal(fetch.mock.callCount(), 0);
  assert.deepEqual(await api.fetchBenchmarkRun("", detail.run_id), detail);
  assert.equal(fetch.mock.callCount(), 1);
});

test("quality queries only explicit provider/model, never a clicked historical suite", async (t) => {
  const evidence = { quality_score: 1, confidence: 0, source_run_count: 2 };
  t.mock.method(globalThis, "fetch", async (url) => {
    const parsed = new URL(url, "http://localhost");
    assert.equal(parsed.pathname, "/v1/benchmarks/quality");
    assert.deepEqual([...parsed.searchParams], [["provider", "p&x"], ["model", "m/a+b"]]);
    return json(evidence);
  });
  assert.deepEqual(await api.fetchBenchmarkQuality("", {
    provider: "p&x", model: "m/a+b", suite_id: "historical",
  }), evidence);
});

for (const [status, code] of [
  [404, "benchmark_run_not_found"], [404, "no_quality_evidence"],
  [503, "quality_suite_not_configured"], [503, "benchmark_data_unavailable"],
  [422, "invalid_benchmark_target"],
]) {
  test(`preserves safe backend error code ${status}/${code}`, async (t) => {
    t.mock.method(globalThis, "fetch", async () => json({ detail: { code } }, status));
    await assert.rejects(api.fetchBenchmarkRuns(""), (error) => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      return true;
    });
  });
}

for (const status of [500, 503]) {
  test(`${status} unrecognized server errors never expose raw payloads`, async (t) => {
    const sensitive = "secret-token C:/private/database.db traceback";
    t.mock.method(globalThis, "fetch", async () => json({
      detail: { code: sensitive, message: sensitive },
    }, status));
    await assert.rejects(api.fetchBenchmarkRuns(""), (error) => {
      assert.equal(error.status, status);
      assert.equal(error.code, "request_failed");
      assert.ok(!`${error.message}${error.stack}${JSON.stringify(error)}`.includes(sensitive));
      return true;
    });
  });
}

test("a non-JSON error body is a safe generic HTTP error, not an empty list", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("private exception", { status: 503 }));
  await assert.rejects(api.fetchBenchmarkRuns(""), (error) => {
    assert.equal(error.status, 503);
    assert.equal(error.code, "request_failed");
    return true;
  });
});

test("malformed successful JSON becomes a safe parsing error", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("private error, not JSON"));
  await assert.rejects(api.fetchBenchmarkRuns(""), (error) => {
    assert.equal(error.code, "invalid_response");
    assert.doesNotMatch(error.message, /private/);
    return true;
  });
});

test("network failures are sanitized rather than displayed or converted to empty data", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("credential /private/path"); });
  await assert.rejects(api.fetchBenchmarkRuns(""), (error) => {
    assert.equal(error.status, null);
    assert.equal(error.code, "request_failed");
    assert.doesNotMatch(error.message, /credential|private/);
    return true;
  });
});

test("a failing benchmark request does not poison successful metrics requests", async (t) => {
  t.mock.method(globalThis, "fetch", async (url) => url.includes("benchmarks")
    ? json({ detail: { code: "benchmark_data_unavailable" } }, 503)
    : json({ request_count: 0 }));
  const [runs, summary] = await Promise.allSettled([
    api.fetchBenchmarkRuns(""), api.fetchSummary(""),
  ]);
  assert.equal(runs.status, "rejected");
  assert.equal(summary.status, "fulfilled");
  assert.deepEqual(summary.value, { request_count: 0 });
});

test("already-cancelled requests never reach fetch", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => json([]));
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(api.fetchBenchmarkRuns("", {}, controller.signal), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 0);
});

test("external cancellation aborts transport without exposing the cancellation reason", async (t) => {
  let transport;
  t.mock.method(globalThis, "fetch", (_url, options) => {
    transport = options.signal;
    return new Promise(() => {});
  });
  const controller = new AbortController();
  const request = api.fetchBenchmarkRuns("", {}, controller.signal);
  controller.abort(new Error("private reason"));
  await assert.rejects(request, (error) => {
    assert.equal(error.name, "AbortError");
    assert.doesNotMatch(error.message, /private/);
    return true;
  });
  assert.equal(transport.aborted, true);
});

for (const hangingBody of [false, true]) {
  test(`10-second deadline terminates hanging ${hangingBody ? "body" : "fetch"}`, async (t) => {
    let deadline;
    let transport;
    const token = {};
    t.mock.method(globalThis, "setTimeout", (callback, milliseconds) => {
      assert.equal(milliseconds, 10_000);
      deadline = callback;
      return token;
    });
    const clear = t.mock.method(globalThis, "clearTimeout", (value) => assert.equal(value, token));
    t.mock.method(globalThis, "fetch", (_url, options) => {
      transport = options.signal;
      return hangingBody
        ? Promise.resolve({ ok: true, status: 200, json: () => new Promise(() => {}) })
        : new Promise(() => {});
    });
    const request = api.fetchBenchmarkRuns("");
    await Promise.resolve();
    deadline();
    await assert.rejects(request, (error) => error.code === "request_timeout");
    assert.equal(transport.aborted, true);
    assert.equal(clear.mock.callCount(), 1);
  });
}

test("LatestRequest rejects stale completions even when a transport ignores abort", () => {
  const slot = new LatestRequest();
  const a = slot.begin();
  const b = slot.begin();
  let displayed;
  let loading = true;
  if (b.isCurrent()) { displayed = "B"; loading = false; }
  if (a.isCurrent()) { displayed = "A"; loading = false; }
  assert.equal(displayed, "B");
  assert.equal(loading, false);
  assert.equal(a.signal.aborted, true);
  assert.equal(b.signal.aborted, false);
});

test("old completion cannot clear a new request's loading state; cleanup invalidates it", () => {
  const slot = new LatestRequest();
  const a = slot.begin();
  const b = slot.begin();
  let loading = true;
  if (a.isCurrent()) loading = false;
  assert.equal(loading, true);
  slot.cancel();
  assert.equal(b.signal.aborted, true);
  assert.equal(b.isCurrent(), false);
  const c = slot.begin();
  assert.equal(c.isCurrent(), true);
  slot.cancel();
});

test("independent request slots cannot cancel each other", () => {
  const runs = new LatestRequest();
  const quality = new LatestRequest();
  const list = runs.begin();
  const target = quality.begin();
  quality.begin();
  assert.equal(list.isCurrent(), true);
  assert.equal(target.isCurrent(), false);
  runs.cancel();
  quality.cancel();
});

test("numeric formatters preserve real zeros and distinguish absent values", () => {
  for (const missing of [null, undefined]) {
    assert.equal(api.formatScore(missing), "—");
    assert.equal(api.formatPercent(missing), "—");
    assert.equal(api.formatLatency(missing), "—");
    assert.equal(api.formatCost(missing), "—");
  }
  assert.equal(api.formatScore(0), "0.000");
  assert.equal(api.formatScore(1), "1.000");
  assert.equal(api.formatPercent(0), "0%");
  assert.equal(api.formatLatency(0), "0 ms");
  assert.equal(api.formatCost("0"), "$0");
});

test("timestamps are visibly UTC, preserve instant across offsets, and handle missing dates", () => {
  const first = api.formatDateTime("2026-09-21T08:00:00+08:00");
  assert.equal(first, api.formatDateTime("2026-09-21T00:00:00Z"));
  assert.match(first, /UTC/);
  assert.match(first, /2026/);
  for (const missing of [null, undefined, "not a date"]) {
    assert.equal(api.formatDateTime(missing), "—");
  }
});

test("existing gateway helpers keep their endpoint paths and GET-only behavior", async (t) => {
  const paths = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(options.method, "GET");
    paths.push(url);
    return json({});
  });
  await Promise.all([
    api.fetchHealth(""), api.fetchProviderHealth(""), api.fetchSummary(""),
    api.fetchProviderMetrics(""), api.fetchRoutingDecisions(""), api.fetchRecentFailures(""),
  ]);
  assert.deepEqual(paths, [
    "/health", "/v1/health/providers", "/v1/metrics/summary?hours=24",
    "/v1/metrics/providers", "/v1/metrics/routing-decisions?limit=10",
    "/v1/metrics/failures?limit=10",
  ]);
});
