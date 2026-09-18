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
models are frozen, extra-forbid and JSON serializable. The runner itself generates no QualitySnapshot.

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

## SQLite persistence (implemented in V04-004)

Schema 4 adds exactly two tables to the existing application database:

| Table | Stored fields and keys |
| --- | --- |
| benchmark_runs | run_id primary key; suite_id, suite_version, suite_fingerprint; provider/model; started_at/finished_at; status; total_cases/completed_cases/execution_failed_cases; terminated_early; max_cases/case_timeout_seconds/temperature/max_tokens |
| benchmark_case_results | run_id foreign key; case_id, case_index, category; execution_status; provider/model; latency_ms; nullable input/output/total_tokens; error_type; nullable evaluation_score/evaluation_passed/evaluator_kind/evaluator_version/evaluation_reason |

Case primary key is (run_id, case_index), with UNIQUE(run_id, case_id); reads restore contiguous
zero-based case_index order, never alphabetical case_id order. The only added query index is
benchmark_runs(started_at DESC, run_id DESC), supporting bounded newest-first listing. No speculative
provider/suite analytics indexes are added. All values use SQL parameters; no pickle or arbitrary blob.

Completed wrong answers persist score=0/passed=0; execution failures persist all evaluation fields
as SQL NULL. Missing usage stays NULL. Booleans serialize as 0/1 and are strictly checked on restore.
UTC aware timestamps serialize with microsecond precision. Domain validation checks restored enums,
scores, counts, identities, time ordering and target consistency. Database constraints enforce keys,
foreign keys, nonnegative fields and evaluation/execution shape. Invalid/corrupt records fail explicitly.

The existing model has no separate termination_reason field: terminal authentication_error on the
last result plus terminated_early preserves the reason without redundant metadata. Generation config
and suite fingerprint round-trip verbatim; the store never recalculates the fingerprint. Different
fingerprints for the same suite identity remain distinguishable historical facts; no suite registry
or global definition-identity collision enforcement is introduced.

BenchmarkStore offers save_run, get_run (complete run or None), list_runs (complete runs; default 20,
integer limit 1..100, SQL LIMIT), and list_case_results (ordered tuple, empty for missing run).
V04-006 adds list_matching_runs(provider, model, suite_id, suite_version, suite_fingerprint):
all exactly matching runs ordered by finished_at/run_id, read in one consistent transaction.
It is intentionally not subject to the global recent-list limit.
Only selected runs are restored; reads use one consistent SQLite snapshot. Corrupt case ordering,
missing cases, invalid metadata and orphan results fail instead of being silently repaired.

No prompt, expected answer, full suite JSON, raw output, error message or credential is stored. Suite
definitions remain repository/user-managed files: retain their exact version to replay a historical
run. A fingerprint identifies content but does not recover the definition. Runs are immutable history;
repeats need new IDs. Production APIs and source version remain unchanged; snapshots are computed below.

## Computed quality snapshots (V04-005)

QualitySnapshot and CategoryQualitySnapshot are frozen, extra-forbid, serializable models.
Both expose nullable quality_score; coverage, execution_completeness, weighted_evaluation_coverage,
confidence; total_cases, observed_cases, evaluated_cases and execution_failed_cases. Ratios are finite
in [0,1]; counts refer to unique cases, not repeated executions. Category snapshots identify category.
The overall snapshot adds provider/model, suite_id/version/fingerprint, policy_version, run_config
(null without runs), source_run_count/source_run_ids, ordered categories and UTC generated_at.
latest_run_id, latest_run_coverage (attempted/total), latest_run_completeness (evaluated/total) expose
partial latest execution even when historical evidence fills gaps. These three fields are null without
runs. Source IDs include only contributing runs, including failed attempts. No snapshot is persisted.

## Routing quality evidence (V04-006)

RoutingSignal adds defaulted quality_source (static/blended/measured), nullable static_quality_score,
benchmark_quality_score, benchmark_confidence, benchmark_suite_id/version/fingerprint,
benchmark_generated_at, benchmark_latest_run_id/coverage/completeness, plus
benchmark_source_run_ids (empty tuple by default). Scores/confidence/ratios are finite [0,1];
timestamps are timezone-aware. quality_score is the actual blended component; sources.quality keeps
the legacy configured label for static quality and uses blended/measured for evidence consumption.

These fields serialize inside existing routing_decisions.explanation_json; old records without them
remain readable. No columns, migration, QualitySnapshot table or API endpoint is added. Explanations
retain provenance even for zero-confidence evidence. Explicit model requests remain unchanged.
