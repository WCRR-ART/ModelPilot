# V0.4 Architecture

```text
explicit local Benchmark Definition (suite_id, version)
  -> explicitly invoked Benchmark Runner (V04-003)
  -> existing Provider adapter -> Model Response
  -> versioned deterministic Evaluator -> CaseEvaluation
  -> BenchmarkCaseResult -> BenchmarkRun
  -> separate benchmark store (V04-004) -> pure scoped QualitySnapshot (V04-005)
  -> optional BenchmarkQualityResolver -> confidence-blended Router quality (V04-006)
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
V04-004 adds explicit persistence after execution; V04-006 optionally reads that evidence for quality.
SQLite schema remains 4; quality routing is disabled unless explicitly configured.

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

## Optional quality integration (V04-006)

V04-005 computes snapshots from explicit definitions and supplied compatible runs, without Store
access, Provider calls or production side effects. Latest attempt per case wins; execution failure
removes stale scores rather than becoming quality zero. Coverage, evaluated completeness, weighted
coverage, confidence and latest-run diagnostics stay distinct. The approved formulas in EVALUATION.md
replace the earlier proposed failure penalty/latest-complete-run policy. Snapshots are not persisted.

Health eligibility remains before scoring. Existing quality/cost/latency/reliability preference weights
and deterministic ordering remain intact. Only the quality component gains a measured input in
V04-006: `quality = (1 - confidence) * static_quality + confidence * measured_quality`.
Use a single configured suite ID/version/fingerprint and overall quality across candidates; never
mix incompatible suites or infer task categories. A candidate lacking compatible evidence
uses static quality. Other measured dimensions retain their V0.3 algorithms.

MODELPILOT_QUALITY_SUITE_PATH is optional and blank by default. Composition loads the explicit local
definition once at startup with the existing strict loader; invalid paths/data fail startup. No smoke
suite default, scanning or automatic execution exists. Restart to change the loaded definition.
Router depends on QualitySignalSource, not SQL/files/aggregation. BenchmarkQualityResolver selects
exact provider/model/suite identity histories through BenchmarkStore.list_matching_runs and calls
the pure aggregator. Reads use one SQLite snapshot and include all matching history, not a globally
truncated recent-run list, so older evidence survives partial runs. No cache/TTL is introduced;
large matching histories can increase per-request read/aggregation cost.

Health gating precedes one quality lookup per eligible candidate per ranking evaluation. The returned
snapshot is frozen and validated, then reused for score and explanation. OPEN candidates and explicit
model requests never query quality. HALF_OPEN uses the same blend; probe coordination is unchanged.
Runtime read/aggregation/identity/validation errors produce a sanitized warning and static quality,
not inference failure. Incompatible run configurations are rejected rather than silently mixed.
No benchmark execution, production metrics writes, snapshot persistence or new schema is involved.

Definitions, run provenance, evaluator versions and normalization are explicit. Runs pin a suite
content fingerprint and provider/model/settings so accidental reuse of a changed version can be
detected in the later registry/store. V04-001 has no registry or global version uniqueness enforcement.

## Read API and explicit CLI (V04-007)

HTTP exposes only GET /v1/benchmarks/runs, /runs/{run_id}, and /quality. Routes depend on the
benchmark store and the configured production BenchmarkQualityResolver, not Runner or Provider.
Store construction/schema initialization happens at application startup, never in GET handlers.
Run-list filters are applied in SQL before the bounded limit; details retain ordered case results.
Quality reads reuse the overall/category aggregation contract without a second scoring formula.
No suite is 503 quality_suite_not_configured; no matching history is 404 no_quality_evidence.
Read/aggregation failures are explicit sanitized 503 errors, unlike Router's static fallback.

The argparse CLI calls loader -> explicit target/config validation -> shared create_provider factory
-> isolated BenchmarkRunner -> BenchmarkStore.save_run. It bypasses GatewayService, Router, health
and probe coordination. Missing credentials fail before execution. Store initialization is checked
before spending provider calls. Ordinary failed cases still form a valid saved run; authentication
failure saves the partial/terminal run but exits nonzero. No scheduler or HTTP execution is added.
Production and CLI share adapter construction unchanged. Schema remains 4. Saving adds only benchmark
history; the next resolver read sees it naturally, without modifying production routing state.

Application composition reuses the loaded suite and store. For injected gateways, their existing
quality resolver is authoritative; no implicit settings file load occurs. An explicit API store
override also supplies the API resolver's evidence, leaving the injected Router untouched.
See BENCHMARKS.md for commands, bounds, response/error contracts and exit codes.

## Privacy and trust

Only user-selected local files are read. JSON is data: no import, eval, exec, network or code execution.
Original fixture prompts are safe to version; production prompts must never be imported automatically.
Later result retention should default to scores and sanitized error/provenance metadata, not raw
responses or credentials. The CLI must not silently download or execute third-party datasets.
