# V0.4 Architecture

```text
explicit local Benchmark Definition (suite_id, version)
  -> explicitly invoked Benchmark Runner (V04-003)
  -> existing Provider adapter -> Model Response
  -> versioned deterministic Evaluator -> CaseEvaluation
  -> BenchmarkCaseResult -> BenchmarkRun
  -> separate benchmark store (V04-004) -> future scoped QualitySnapshot
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
automatic selection, retry or fallback. The runner still returns an in-memory result without saving.
V04-004 adds explicit persistence after execution; quality integration remains a future task.
SQLite schema is now 4; production inference behavior is unchanged.

Production attempts describe live latency, reliability and estimated cost. Benchmark records describe
dedicated task evaluation. Separate storage tables/interfaces and source labels prevent the latter
from contaminating operational metrics.

## Benchmark persistence (V04-004)

BenchmarkStore is a separate Protocol, not an extension of MetricsStore. SQLiteBenchmarkStore and
SQLiteMetricsStore share SQLiteDatabase connection/migration infrastructure and the same configured
file (`Settings.metrics_db_path`, from MODELPILOT_METRICS_DB). No second database is introduced.
The extracted infrastructure preserves WAL, foreign_keys=ON, busy timeout, explicit migrations,
connection closure and UTC microsecond ISO datetime serialization. Decimal pricing stays unchanged;
benchmark scores/latency/configuration use finite REAL values and token counts use nullable INTEGERs.

Call `store.save_run(run)` explicitly after `await runner.run(...)`. A short BEGIN IMMEDIATE
transaction inserts the run plus every case or rolls everything back. Duplicate run IDs raise
DuplicateBenchmarkRunError, never overwrite. SQLite failures propagate; saving is not best-effort.
There are no update/delete interfaces or automatic saves. Reads reconstruct validated domain models;
invalid persisted data raises BenchmarkStoreDataError rather than repairing or inventing values.

Fresh databases initialize at schema 4; versions 1/2/3 migrate through the existing chain then add
two benchmark tables. Migration failure rolls back schema changes and version advancement together.
Existing attempts, pricing, routing decisions and health are preserved and never queried as benchmark
results. Production summary/provider metrics still query only production tables.

Before opening an existing database with this development version, stop writers and take a consistent
SQLite backup (including uncheckpointed WAL). There is no down-migration: the released v0.3.0 binary
rejects schema 4. Restore the pre-upgrade backup if returning to v0.3.0. Tests use temporary databases;
no user database is migrated as part of development validation.

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
