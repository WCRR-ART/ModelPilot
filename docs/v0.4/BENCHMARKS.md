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
strict string, expected number a finite strict number. For json_equal, `expected_json` is a string
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
