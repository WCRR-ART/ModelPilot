# V0.4 Data Model

## Implemented definition layer (V04-001)

All definitions use frozen Pydantic models with extra fields forbidden. Strings are strict; identity
tokens, nonblank labels, finite numeric fields and nonempty tuples are validated. No Any-based domain
contract. JSON expectations are validated immutable JSON-document strings, not mutable mappings.

| Model | Fields |
| --- | --- |
| BenchmarkMessage | role, content |
| EvaluatorSpec (discriminated union) | kind, evaluator_version, expected_text OR expected_number + tolerance OR expected_json |
| BenchmarkCase | case_id, category, messages, evaluator, weight |
| BenchmarkSuite | suite_id, version, name, description, cases; identity property returns (suite_id, version) |

ExactMatchSpec, NormalizedExactMatchSpec, ContainsSpec, NumericToleranceSpec and JsonEqualSpec
are the concrete evaluator variants. Definition JSON round-trips through model_dump_json and
model_validate_json; tuples serialize as arrays. The loader rejects duplicate JSON keys, which
ordinary Pydantic JSON parsing alone does not detect; use the loader for dataset files.

## Evaluation result (implemented in V04-002)

CaseEvaluation: frozen typed evaluator_kind/version, finite score [0,1], strict passed boolean and
stable reason. It contains no raw expected/actual output. NumericToleranceSpec now uses Decimal
expected_number/tolerance; the loader preserves numeric token precision. See EVALUATION.md for
the dataset versus model-serialization formats and inclusive Decimal comparison semantics.

## In-memory execution results (implemented in V04-003)

- BenchmarkTarget: explicit nonblank provider/model, no auto or surrounding whitespace.
- BenchmarkRunConfig: positive max_cases, case_timeout_seconds, max_tokens; temperature in [0,2].
- BenchmarkCaseResult: case_id/category, provider/model, completed or provider_failed, nonnegative
  latency_ms, nullable nonnegative token counts, normalized error_type, CaseEvaluation or null.
  Completed requires an evaluation and no error; failed requires an error and no evaluation.
- BenchmarkRun: run_id, suite_id/suite_version, SHA-256 suite_fingerprint, provider/model, config,
  UTC aware started_at/finished_at, completed or completed_with_failures, total_cases, completed_cases,
  execution_failed_cases, terminated_early and ordered immutable case_results.

completed_cases counts successful executions, including wrong answers, not all attempted cases.
Result counts/status/identity/time order are validated. Results have unique case IDs; a partial run
requires a terminal authentication error. Auth on the final case is complete with failures, not early.
Nested case results inherit run/suite identity from their containing run instead of duplicating it.
There are no raw outputs, prompts, secrets, error messages, database IDs or estimated costs.
Config plus suite fingerprint and per-evaluation version retain execution provenance. All result
models are frozen, extra-forbid and JSON serializable. No QualitySnapshot is generated.

## Result layer roadmap

| Model | Minimum planned data and invariants |
| --- | --- |
| CaseEvaluation (implemented) | evaluator kind/version, score [0,1], passed, structured reason; no natural-language-only result |
| BenchmarkCaseResult | run_id, suite/version/case/category, provider/model, evaluation or explicit failure, nullable usage/latency/cost; no production request payload |
| BenchmarkRun | run_id, suite identity/fingerprint, provider/model, generation settings, evaluator provenance, planned/terminal case counts, state, UTC aware started/finished timestamps |
| QualitySnapshot | provider/model/suite/version/category, provenance, quality [0,1] or null, distinct sample count, coverage/completeness/confidence [0,1], aggregation policy version |

Definition means what is tested; Run means one execution of that immutable definition against a
provider/model. Results never modify definitions. Repeats have separate run IDs; they do not create
new unique cases. Provider failure, invalid response, evaluator error and incomplete run remain
distinguishable. Missing aggregates use null, never invented zero measurements.

Persistence and schema are intentionally deferred. V04-001 uses no store, adds no tables and leaves
SQLite version 3 unchanged. Later migration design must preserve V0.2/V0.3 data and keep benchmark
records separate from production metrics and health. Runtime APIs and source version stay unchanged.
