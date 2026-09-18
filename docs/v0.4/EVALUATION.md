# Deterministic Evaluation Design

V04-001 defines configuration only. Execution of all five evaluators belongs to V04-002.
Every spec pins `evaluator_version` to `"1"` (default). Semantics changes require a new version.
Evaluators accept model response text plus a validated spec and return a typed CaseEvaluation.
All first-version scores are binary 0.0/1.0, finite and in [0,1]. There is no LLM judge.

| Kind | Expected/configuration | Planned deterministic semantics | Edge cases |
| --- | --- | --- | --- |
| exact_match | expected_text: string | Byte-for-character Unicode string equality; no trim | Empty expected text allowed; whitespace/case significant |
| normalized_exact_match | expected_text: string | NFKC, casefold, collapse Unicode whitespace, trim, then equality | Same pipeline for both strings; empty normalized text allowed; no punctuation stripping |
| contains | expected_text: nonempty string | Case-sensitive literal substring; no regex/normalization | Empty needle rejected to prevent vacuous passes |
| numeric_tolerance | expected_number: finite number; tolerance: finite >= 0, default 0 | Parse entire trimmed response as one finite JSON number; abs(actual-expected) <= tolerance | No units, prose, code fences, booleans, NaN/Infinity; exact inclusive boundary |
| json_equal | expected_json: JSON document string | Parse entire response; compare JSON trees | Object order ignored, arrays ordered, no duplicate keys, bool differs from number, numeric 1 equals 1.0, no fence stripping |

Malformed responses produce score 0 with a structured reason, not exceptions that silently skip
cases. Invalid definitions are rejected before execution. Provider failure produces a failed case
result; internal evaluator failure is a distinct run error, not evidence of low model quality.

## Planned aggregation and confidence (V04-005)

Scope: provider/model, suite_id/version, category and versioned evaluator configuration. Use the
latest compatible complete run; do not mix changed suites, evaluator versions or generation settings.
The full suite aggregate may be displayed but category/profile selection must remain explicit.

For a category with declared cases i, positive weights w_i and result scores s_i:
`quality = sum(w_i * s_i) / sum(w_i)` on a complete run. Provider failures count as zero; they
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
