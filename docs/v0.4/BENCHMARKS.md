# Benchmark Definitions and Dataset Format

V04-001 supports local UTF-8 JSON. Call `load_benchmark_suite(path)` with one explicit path; there
is no directory discovery, remote fetch, registry, dynamic import or dataset execution.
The file is one suite object, not JSONL. UTF-8 BOM is rejected by strict JSON parsing.

## Identity and ordering

- Suite identity: `(suite_id, version)`; case identity: `(suite_id, version, case_id)`.
- Identity tokens start with an ASCII alphanumeric and contain only alphanumerics, `.`, `_`, `-`.
  Version is an opaque nonempty token, not implicitly coerced to a number or required to be SemVer.
- Changing a case, message, evaluator configuration, expected value, category, order or weight
  requires a new suite version. V04-001 cannot detect cross-file identity/content collisions.
- Cases and messages preserve file order using tuples. Duplicate case IDs are rejected, not merged.
- Unknown fields are rejected at every model level; unknown categories/evaluators/roles also fail.
- Duplicate JSON object keys, nonstandard NaN/Infinity, malformed JSON and empty suites fail explicitly.

## Fields

Suite: required `suite_id`, `version`, `name`, `cases`; `description` defaults to an empty string.
Name must contain non-whitespace text. A suite has at least one case.

Case: required `case_id`, `category`, nonempty `messages`, `evaluator`; `weight` defaults to 1.0
and must be a finite number > 0, not a boolean or numeric string.
Categories: coding, reasoning, structured_output, math, instruction_following. These are labels,
not a promise that a small fixture measures all skills. Coding cases use deterministic output checks,
not execution of generated code.

Message: required role (system/user/assistant) and string content containing non-whitespace text.
Message content is preserved verbatim. All definition models reject mutation; nested sequences are
tuples. Pydantic unchecked construction/copy escape hatches are not supported loading paths.

Evaluator configuration is a discriminated union on `kind`; see EVALUATION.md. Expected text is a
strict string, expected number a finite Decimal loaded from a JSON numeric token. Model JSON
serialization uses decimal strings for precision-preserving round trips; dataset numeric strings
remain invalid. For json_equal, `expected_json` is a string
containing a complete valid JSON document, e.g. `"{\"ok\":true}"`. This deliberately avoids a mutable
dict/list buried inside an otherwise frozen definition. It can represent any JSON value, including
JSON null, and is validated but not evaluated against model output in V04-001.

## Example

```json
{
  "suite_id": "modelpilot-smoke",
  "version": "1",
  "name": "Original loader smoke fixture",
  "description": "Not a scientific benchmark or leaderboard.",
  "cases": [{
    "case_id": "echo",
    "category": "instruction_following",
    "messages": [{"role": "user", "content": "Reply with exactly PINE."}],
    "evaluator": {"kind": "exact_match", "expected_text": "PINE"},
    "weight": 1
  }]
}
```

`benchmarks/suites/smoke-v1.json` is original synthetic content for loader/schema and future runner
smoke checks only. It contains no user traffic and no measured quality scores. Larger or external
datasets require deliberate inclusion and provenance/license review, never automatic downloading.

## Explicit execution (V04-003)

`await BenchmarkRunner().run(suite, BenchmarkTarget(provider=..., model=...), provider, config)`
returns an in-memory BenchmarkRun. Supply an existing abstract Provider implementation explicitly;
the runner never discovers credentials or models from the environment. Provider name and every
outcome's provider/model must match the target; identity mismatch is a programming error. `auto`
is rejected. Calls execute exactly once per case in file order, without parallelism, retry or fallback.
Messages are copied verbatim with no extra system prompt or evaluator hints.

BenchmarkRunConfig defaults: max_cases=100, case_timeout_seconds=30, temperature=0, max_tokens=1024.
All limits must be positive and finite; temperature is finite in [0,2]. Oversized suites are rejected
before any call, never truncated. Generation settings are recorded with the run. The existing three
adapters accept these request fields, but individual models may reject a setting; this is an execution
failure, not an invitation to silently change settings. Temperature zero does not guarantee identical
provider output. The token cap bounds requested output, not input size or monetary cost.

Each provider await has an asyncio deadline independent of its HTTP timeout. Deadline expiry cancels
the cooperative async call and records timeout, then continues. Adapters must not block the event
loop or suppress cancellation; this is not a subprocess kill/resource sandbox. External cancellation
propagates, with no partial checkpoint. Unexpected adapter/evaluator invariant exceptions propagate.

Successful outcomes use only normalized `choices[0].message.content` text. Missing/malformed/nontext
content is invalid_response and execution_status=provider_failed. A wrong text answer remains
completed with an evaluator score of zero. Every execution failure has evaluation=null, never a
fabricated quality score. Ordinary normalized failures (including rate_limit and unknown_error)
continue to the next case. Authentication errors stop immediately, preserving prior results and the
failed case; terminated_early is true only if cases remain unattempted. No sleep/retry is performed.

The runner does not read ProviderHealth: explicit benchmarks are allowed against OPEN circuits.
It neither changes health/probe state nor writes production attempts, routing decisions or metrics.
Only outcome token usage (including nulls), latency and standardized error enum are retained. No
raw response, actual output, headers, error message, credentials, duplicate prompts or expected
answers are captured. Raw output is transiently evaluated; there is no unlimited output retention.
The runner performs no cost estimation, persistence, quality aggregation, HTTP or Dashboard work.

Run identity is a UUID by default; clock/id_factory can be injected. Times are aware UTC. A SHA-256
fingerprint of the validated suite's UTF-8 model JSON pins content, order and evaluator configuration
(not the original file's formatting). The definition must remain available separately for later replay.

## Explicit saving (V04-004)

SQLiteBenchmarkStore accepts the same database path as SQLiteMetricsStore. For programmatic use:

```python
from modelpilot.benchmarks import SQLiteBenchmarkStore
from modelpilot.config import Settings

settings = Settings.from_env()
settings.metrics_db_path.parent.mkdir(parents=True, exist_ok=True)
store = SQLiteBenchmarkStore(settings.metrics_db_path)
# run = await runner.run(suite, target, provider)
store.save_run(run)
restored = store.get_run(run.run_id)
recent = store.list_runs(limit=20)
```

Execution and saving are separate; failure to save raises an exception. Historical run IDs cannot be
overwritten. The same file is upgraded to schema 4, with separate benchmark tables; no production
attempt/health/routing/metrics writes are performed. Back up before migration; see ARCHITECTURE.md
for rollback limitations. Definitions and raw answers are not stored. No benchmark API/CLI, quality
aggregation or routing integration is introduced in this task.

## Quality aggregation (V04-005)

aggregate_quality takes the definition, compatible runs, explicit target and an aware generated_at.
It invokes no Store or Provider. Quality uses evaluated answers including zeros; execution failures
lower completeness/confidence instead. Latest attempt per case wins, even if it failed. Missing
attempts in a newer partial run retain historical evidence; latest-run diagnostics remain explicit.
See EVALUATION.md for the authoritative latest_attempt_v1 formulas, 5/50 ramp and category rules.
Repeated runs never multiply unique case evidence. No quality routing is enabled.
