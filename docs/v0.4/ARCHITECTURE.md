# V0.4 Architecture

```text
explicit local Benchmark Definition (suite_id, version)
  -> explicitly invoked Benchmark Runner (V04-003)
  -> existing Provider adapter -> Model Response
  -> versioned deterministic Evaluator -> CaseEvaluation
  -> BenchmarkCaseResult -> BenchmarkRun
  -> separate benchmark store -> scoped QualitySnapshot
  -> future confidence-blended quality input to Router
```

## Boundaries

The `modelpilot.benchmarks` definition/loading modules do not depend on FastAPI, Router, providers,
health or stores. The V04-003 runner depends only on the abstract Provider and its normalized outcome,
shared request schema, definitions, result models and pure evaluators.
Evaluation is a pure response-to-score operation; the runner owns I/O and outcome/error handling.
Runner calls are explicit, use dedicated cases and never intercept production traffic.
Benchmark calls must not automatically create production attempts or drive circuit transitions.
Runner configuration bounds case count, each call's deadline and generated token count. It does not
estimate or enforce a monetary budget; real calls can incur provider charges.

V04-003 calls `Provider.complete` directly, never GatewayService. It has no store, health, Router,
pricing, probe coordinator or API dependency. An explicitly selected target may be tested even when
its production circuit is OPEN; benchmark success/failure never changes that circuit. There is no
automatic selection, retry or fallback. Results remain in memory; downstream store/quality arrows
are future tasks. SQLite schema remains 3 and production behavior is unchanged.

Production attempts describe live latency, reliability and estimated cost. Benchmark records describe
dedicated task evaluation. Separate storage tables/interfaces and source labels prevent the latter
from contaminating operational metrics. Persistence/schema migration is deferred to V04-004.

## Later quality integration

Health eligibility remains before scoring. Existing quality/cost/latency/reliability preference weights
and deterministic ordering remain intact. Only the quality component gains a measured input in
V04-006: `quality = (1 - confidence) * static_quality + confidence * measured_quality`.
Use a single configured suite/version/category profile for comparisons across candidates; never
mix incompatible suites or infer categories with an LLM. A candidate lacking compatible evidence
uses static quality. Other measured dimensions retain their V0.3 algorithms.

Definitions, run provenance, evaluator versions and normalization are explicit. Runs pin a suite
content fingerprint and provider/model/settings so accidental reuse of a changed version can be
detected in the later registry/store. V04-001 has no registry or global version uniqueness enforcement.

## Privacy and trust

Only user-selected local files are read. JSON is data: no import, eval, exec, network or code execution.
Original fixture prompts are safe to version; production prompts must never be imported automatically.
Later result retention should default to scores and sanitized error/provenance metadata, not raw
responses or credentials. The CLI must not silently download or execute third-party datasets.
