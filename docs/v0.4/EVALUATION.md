# Deterministic Evaluation Design

V04-001 defines configuration; V04-002 implements all five pure evaluators through
`evaluate_case(spec, actual_output) -> CaseEvaluation` in `benchmarks/evaluators.py`.
Every spec pins `evaluator_version` to `"1"` (default). Semantics changes require a new version.
Evaluators accept model response text plus a validated spec and return a typed CaseEvaluation.
All first-version scores are binary 0.0/1.0, finite and in [0,1]. There is no LLM judge.

| Kind | Expected/configuration | Implemented deterministic semantics | Edge cases |
| --- | --- | --- | --- |
| exact_match | expected_text: string | Byte-for-character Unicode string equality; no trim | Empty expected text allowed; whitespace/case significant |
| normalized_exact_match | expected_text: string | NFKC, casefold, collapse Unicode whitespace, trim, then equality | Same pipeline for both strings; empty normalized text allowed; no punctuation stripping |
| contains | expected_text: nonempty string | Case-sensitive literal substring; no regex/normalization | Empty needle rejected to prevent vacuous passes |
| numeric_tolerance | expected_number: finite Decimal; tolerance: finite Decimal >= 0, default 0 | Parse entire trimmed response as one finite JSON number; abs(actual-expected) <= tolerance | No units, prose, code fences, booleans, NaN/Infinity; exact inclusive boundary |
| json_equal | expected_json: JSON document string | Parse entire response; compare JSON trees | Object order ignored, arrays ordered, no duplicate keys, bool differs from number, numeric 1 equals 1.0, no fence stripping |

Malformed responses produce score 0 with a structured reason, not exceptions that silently skip
cases. Invalid definitions are rejected before execution. Provider failure produces a failed case
result; internal evaluator failure is a distinct run error, not evidence of low model quality.

## V04-002 result and numeric precision

CaseEvaluation is frozen and serializable: evaluator_kind, evaluator_version, score, passed, reason.
Stable reasons: match, mismatch, invalid_number, outside_tolerance, invalid_json. Passing requires
score 1 and reason match; invalid answers require score 0. The schema retains a [0,1] score field
for future partial-credit designs, but every current evaluator returns only 0 or 1. There are no raw
expected/actual texts, exception messages or unbounded details in the result.

Numeric definition fields are now Decimal, not float. This is the minimum V04-002 correction to
V04-001 required for lossless tolerance boundaries. Dataset numeric tokens load directly as Decimal;
int/Decimal Python inputs are exact, while existing finite Python floats convert using Decimal(str(x))
(precision already lost before construction cannot be restored). Dataset/Python numeric strings and
booleans remain rejected. Pydantic model JSON round-trips use its standard decimal-string encoding;
that serialization format is distinct from the numeric-token dataset format.

Numeric answer parsing uses the full JSON number grammar after trimming outer whitespace, with
scientific notation supported. Leading plus, leading zeroes, Unicode digits, underscores, units,
code fences and prose are rejected. Decimal comparison uses a private precision/rounding context,
independent of the caller's Decimal context. Directed subtraction plus the inexact flag compares the
inclusive boundary correctly without allocating digits across a huge exponent gap. Unrepresentable
Decimal answer numbers return invalid_number, not a run exception.

JSON numbers compare exactly using integers/Decimal, including long fractional literals; booleans
are a distinct type. Duplicate keys and nonstandard constants are rejected. The existing JSON parser
still rejects fractional/exponent values that overflow its V04-001 finite-number range; parser
resource/recursion failures yield invalid_json for answers. JSON string values are not normalized.
No database, filesystem, environment, time, randomness, network, provider or LLM access occurs during
evaluation. Invalid spec kind/version or non-text caller input fails explicitly as a programming error.

## Planned aggregation and confidence (V04-005)

Scope: provider/model, suite_id/version, category and versioned evaluator configuration. Use the
latest compatible complete run; do not mix changed suites, evaluator versions or generation settings.
The full suite aggregate may be displayed but category/profile selection must remain explicit.

For a category with declared cases i, positive weights w_i and result scores s_i:
`quality = sum(w_i * s_i) / sum(w_i)` on a complete run. Proposed aggregation penalties count
provider failures as zero; V04-003 execution records still have evaluation=null, not score=0. They
are not omitted from the denominator. Evaluator/system failure makes the run ineligible for routing.
Partial-run progress may be displayed, but must not be labeled a complete quality measurement.

Let n be distinct successfully evaluated case IDs in that category (not repeat executions),
coverage be their declared weight / total declared category weight, and completeness be terminal
case results / declared case count. Clamp all ratios to [0,1]. Proposed versioned policy:
`ramp(n) = 0 if n < 5 else min(1, (n - 5) / 45)`;
`confidence = ramp(n) * coverage * completeness`.
Only complete, compatible runs without system/evaluator errors are eligible; otherwise confidence
is 0 and measured quality is unavailable for routing. Provider failures reduce coverage/confidence
as well as the score. Repeating one case cannot inflate n. A tiny smoke suite always has zero routing
confidence and is not sufficient evidence to override static quality.

No eligible benchmark => measured quality null, confidence 0, static quality unchanged. This is a
conservative evidence-weight heuristic, not a statistical confidence interval or scientific proof.
Thresholds and policy version will be explicit inputs in V04-005/006, not baked into V04-001 models.

Provider generation is not guaranteed reproducible byte-for-byte, even with fixed sampling settings.
Record suite fingerprint, evaluator version, provider/model identity, settings, time and errors so
the procedure can be replayed and differences audited. Any future LLM judge needs a separate design
covering cost, bias, judge version and reproducibility; it is outside the initial implementation.
