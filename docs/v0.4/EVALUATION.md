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

## Aggregation and confidence (V04-005, revised contract)

This supersedes the proposed failure-as-zero penalty and latest-complete-run policy.
Quality is not reliability. Execution failures never enter the quality numerator or denominator.
Completed wrong answers, including score=0, are valid evidence and cannot be filtered out.

aggregate_quality(suite, runs, target=..., generated_at=...) is pure domain calculation.
Every run must match provider/model, suite ID/version/fingerprint, run configuration, case identities,
categories and evaluator kind/version. The fingerprint is checked against the supplied definition.
Invalid historical runs are rejected even when newer runs would supersede them. Partial results must
be an executed prefix of the definition, consistent with Runner's authentication fail-fast behavior.

For each case, the latest actual attempt by (run.finished_at, run_id) wins. A latest failure removes
old successful evidence. Cases absent from a newer partial run retain their own latest historical
attempt. Identical duplicate run IDs are deduplicated; conflicting same-ID records are rejected.
Only runs contributing selected attempts appear in lexically sorted source_run_ids. Repetition never
multiplies independent case evidence. No age expiry is applied in this task.

For the suite and independently for each real category:

- quality_score = sum(evaluated weight * score) / sum(evaluated weight); null without evaluations.
- coverage = observed unique cases / total expected cases; observed includes execution failures.
- execution_completeness = evaluated unique cases / total expected cases; wrong answers count.
- weighted_evaluation_coverage = evaluated weight / total expected weight.
- ramp(n) = clamp((n - 5) / 45, 0, 1), using unique evaluated cases in this scope.
- confidence = ramp(n) * weighted_evaluation_coverage * execution_completeness.

The fixed versioned policy is latest_attempt_v1: n<=5 gives zero sample confidence, 6..49 grows
linearly, n>=50 gives sample confidence 1. This preserves the planned 5/50 rule, not the suggested 20.
A fully wrong 50-case run has quality 0 and confidence 1. A perfect 3-case smoke has quality 1
and confidence 0. This is a conservative evidence-weight heuristic, not a statistical interval.
Partial scores in [0,1] are supported. Overall quality weights cases directly, not category means.

Categories appear in first-occurrence order in the definition; absent categories are not invented.
latest_run_id, latest_run_coverage (attempted/total), and latest_run_completeness (evaluated/total)
separately expose the latest run. Historical evidence must not imply the latest execution was complete.
These diagnostics do not add an undocumented factor to confidence.

Empty input returns null quality, zero counts/ratios/confidence, no sources/latest run/config,
and the definition's real categories for the explicit target. generated_at is caller-supplied aware
time normalized to UTC. Input run order does not affect output. All inputs are revalidated.

The aggregator itself performs no Store access, Provider calls or snapshot persistence; schema stays 4.

## Routing consumption (V04-006)

Optional routing consumes only overall quality_score and the already-computed confidence:
`quality = static_quality * (1 - confidence) + benchmark_quality * confidence`.
Missing snapshots, null quality and zero confidence retain static quality. Confidence 1 uses measured
quality exactly; partial confidence blends. Do not apply coverage/completeness again or recalculate
confidence in Router. Category evidence is not used for task classification or category routing.
The original final weighted formula and other three dimensions are unchanged. Health eligibility
comes first; benchmark quality cannot reopen or bypass a circuit. See ARCHITECTURE.md for wiring.
