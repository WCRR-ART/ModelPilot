/* eslint-disable @typescript-eslint/no-require-imports */
// Explicit, isolated browser acceptance server. No production DB or Provider access.
const http = require("node:http");
const { fixtures, qualityFor, runSummaries, runs } = require("./fixtures.cjs");

const port = Number(process.env.PORT ?? 8765);
const requests = [];
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${port}`);
  response.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:3100");
  response.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  const send = (status, body) => {
    if (response.destroyed) return;
    response.writeHead(status);
    response.end(JSON.stringify(body));
  };
  const error = (status, code) => send(status, { detail: { code } });
  if (request.method === "OPTIONS") return send(204, null);
  if (url.pathname === "/__test/requests" && request.method === "GET") {
    return send(200, requests);
  }
  if (url.pathname === "/__test/reset" && request.method === "GET") {
    requests.length = 0;
    return send(200, { reset: true });
  }
  requests.push({ method: request.method, path: url.pathname, query: url.search });
  if (request.method !== "GET") return error(405, "method_not_allowed");
  if (Object.hasOwn(fixtures, url.pathname)) return send(200, fixtures[url.pathname]);
  if (url.pathname === "/v1/benchmarks/runs") {
    const provider = url.searchParams.get("provider");
    if (provider === "error") return error(503, "benchmark_data_unavailable");
    if (provider === "network-error") return request.socket.destroy();
    const limit = Number(url.searchParams.get("limit") ?? 20);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      return error(422, "invalid_limit");
    }
    const filtered = runSummaries.filter((run) =>
      ["provider", "model", "suite_id", "suite_version"].every((key) =>
        !url.searchParams.has(key) || run[key] === url.searchParams.get(key),
      ),
    );
    return send(200, filtered.slice(0, limit));
  }
  if (url.pathname.startsWith("/v1/benchmarks/runs/")) {
    const runId = decodeURIComponent(url.pathname.slice("/v1/benchmarks/runs/".length));
    const run = runs.find((value) => value.run_id === runId);
    return run ? send(200, run) : error(404, "benchmark_run_not_found");
  }
  if (url.pathname === "/v1/benchmarks/quality") {
    const provider = url.searchParams.get("provider");
    const model = url.searchParams.get("model");
    if (!provider || !model || provider === "auto" || model === "auto") {
      return error(422, "invalid_benchmark_target");
    }
    if (model === "no-evidence") return error(404, "no_quality_evidence");
    if (model === "not-configured") return error(503, "quality_suite_not_configured");
    if (model === "error" || model === "server-error") {
      return send(model === "error" ? 503 : 500, {
        detail: { code: "benchmark_data_unavailable" },
        debug: "SECRET_TEST_MARKER raw traceback /private/database.sqlite",
      });
    }
    if (model === "network-error") return request.socket.destroy();
    if (model === "slow-a" || model === "timeout") {
      await new Promise((resolve) => setTimeout(resolve, model === "timeout" ? 20000 : 1500));
    }
    return send(200, qualityFor(provider, model));
  }
  return error(404, "not_found");
});

server.listen(port, "127.0.0.1", () => {
  console.log(`TEST DATA ONLY fixture API: http://127.0.0.1:${port}`);
});
