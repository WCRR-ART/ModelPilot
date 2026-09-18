# V0.4 Implementation Tasks

Each task is a focused independently validated commit. V04-001 through V04-006 are implemented;
V04-007 and later task descriptions remain plans, not completed features.

| Task | Name | Goal and acceptance gate |
| --- | --- | --- |
| V04-001 | Benchmark Domain Models & Dataset Loader | Frozen typed definitions, strict local JSON loading, original smoke fixture; no execution/store/routing; malformed/duplicate/immutable/order tests and V0.3 regression |
| V04-002 | Deterministic Evaluators | Implement the five specified pure evaluator functions and typed CaseEvaluation; boundary, normalization, malformed response and deterministic-score tests; no provider calls |
| V04-003 | Benchmark Runner | Explicit bounded runner over dedicated cases with existing adapters, pinned definition and generation settings; fake-provider failure/cancellation tests, no production metrics/health pollution |
| V04-004 | Benchmark Result Persistence | Separate benchmark store and versioned SQLite migration preserving all existing data; run/result provenance, identity collision and reopen tests |
| V04-005 | Quality Metrics Aggregation | Scoped weighted quality, unique-case coverage/completeness/confidence, no survivor bias or repeated-case inflation; complete/partial/error/missing-data tests |
| V04-006 | Confidence-Blended Quality Routing | Optional explicit suite identity and overall quality blending; static fallback, health precedence, unchanged other dimensions and deterministic ranking tests |
| V04-007 | Benchmark Read API / CLI | Read-only inspection and explicit opt-in bounded CLI execution; no HTTP run/control endpoint, no automatic downloads, no secret/raw-output leakage |
| V04-008 | Benchmark Dashboard | Minimal real-data benchmark/quality view with provenance and missing/error states; no invented scores or broad UI redesign |
| V04-009 | End-to-End Hardening & Release | Fake-provider full chain, schema compatibility, privacy audit, bilingual docs, version metadata and release-prep validation; publication requires separate authorization |

Dependencies follow the numeric order, with V04-007 consuming the runner/store/aggregation and
V04-008 consuming the read API. Every task runs pytest/Ruff and the required frontend checks.
No task may silently expand into new providers, distributed circuit breaking, streaming, auth,
billing, LLM judging, benchmark downloads or ML training.

V04-001 implementation files: backend/src/modelpilot/benchmarks/{models,loader,__init__}.py and
benchmarks/suites/smoke-v1.json. Validation covers required/nonblank fields, strict expected values,
roles/categories/kinds, numeric boundaries, unknown fields, JSON errors, unique IDs, UTF-8,
immutable nested data, stable ordering, data-only loading and unchanged production behavior.
