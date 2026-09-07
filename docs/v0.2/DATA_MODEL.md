# V0.2 Minimal Data Model

## Principles

- Persist facts from completed attempts and decisions; derive aggregates at query time.
- Use nullable fields for unavailable values instead of placeholder zeros.
- Keep prompt content, response content, credentials, and raw authorization/provider bodies out of storage.
- Use UTC ISO-8601 timestamps and monotonic elapsed milliseconds.
- Keep the schema suitable for one SQLite database and one ModelPilot deployment.

## Relationships

```text
RoutingDecision 1 ---- 0..N RequestRecord
       |                       |
       | selected provider     | provider + model + ended_at
       |                       v
       |                 ProviderMetrics (derived query model)
       |
       +-------------------- ModelPricing (matched by provider/model/time)
```

One incoming automatic request has one `RoutingDecision`. Each provider actually attempted has one `RequestRecord`, linked by `gateway_request_id`. Explicit-model requests have attempt records but do not require an automatic routing decision.

## RequestRecord

One row per completed provider attempt.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | integer | yes | SQLite primary key |
| `gateway_request_id` | text | yes | Existing generated request ID; not a credential |
| `attempt_index` | integer | yes | Zero-based order within fallback sequence |
| `provider` | text | yes | Stable provider key |
| `model` | text | yes | Provider model identifier |
| `started_at` | text | yes | UTC attempt start timestamp |
| `ended_at` | text | yes | UTC attempt end timestamp |
| `latency_ms` | real | yes | Monotonic elapsed milliseconds; non-negative |
| `success` | integer/boolean | yes | Provider attempt outcome |
| `error_category` | text | no | Bounded category such as timeout, transport, provider_status, invalid_response |
| `input_tokens` | integer | no | Real provider-reported input/prompt tokens |
| `output_tokens` | integer | no | Real provider-reported output/completion tokens |
| `total_tokens` | integer | no | Real provider-reported total, or validated input + output when both exist |
| `usage_status` | text | yes | `reported` or `unavailable` |
| `estimated_cost_usd` | decimal text | no | Estimate from real usage and matching configured pricing |
| `cost_status` | text | yes | `estimated`, `usage_unavailable`, or `pricing_unavailable` |
| `pricing_id` | integer | no | Pricing metadata used for the estimate |
| `selected` | integer/boolean | yes | Whether this attempt produced the returned response |
| `created_at` | text | yes | UTC persistence timestamp |

Constraints:

- unique `(gateway_request_id, attempt_index)`
- token counts are non-negative when present
- `latency_ms >= 0`
- `selected` implies `success`
- estimated cost requires `cost_status = estimated`, reported usage, and a pricing reference

Indexes:

- `(provider, model, ended_at DESC)` for rolling metrics
- `(gateway_request_id, attempt_index)` through the unique constraint
- no prompt or response indexes because that content is not stored

## ProviderMetrics

`ProviderMetrics` is a derived domain/read model, not a persisted aggregate table in V0.2. This prevents stale counters and avoids update-order problems.

| Field | Type | Meaning |
| --- | --- | --- |
| `provider` / `model` | text | Candidate identity |
| `as_of` | datetime | Snapshot cutoff |
| `window_attempt_limit` | integer | 100 by default |
| `window_max_age_days` | integer | 7 by default |
| `total_requests` | integer | Completed provider attempts in the window |
| `successful_requests` | integer | Successful attempts |
| `failed_requests` | integer | Failed attempts |
| `success_rate` | float/null | Observed successes / total |
| `average_latency_ms` | float/null | Mean raw latency |
| `rolling_latency_ms` | float/null | Mean routing-capped latency |
| `p50_latency_ms` / `p95_latency_ms` | float/null | Nearest-rank percentiles |
| `priced_request_count` | integer | Records with an estimated cost |
| `average_estimated_cost_usd` | decimal/null | Mean priced request estimate |
| `p50_estimated_cost_usd` | decimal/null | Median priced request estimate |
| `input_tokens` / `output_tokens` / `total_tokens` | integer/null | Sums of available reported values |
| `usage_unavailable_count` | integer | Attempts lacking usage |
| `estimated_cost_total_usd` | decimal/null | Sum of priced estimates |

Empty windows return zero counts and `NULL` rates/measurements. Query code must not replace `NULL` with a fabricated metric.

For Dashboard summary queries, gateway request count is `COUNT(DISTINCT gateway_request_id)` while per-provider request count is the number of actual attempt rows. This preserves both user-request volume and fallback load.

## RoutingDecision

One row per `model="auto"` ranking operation.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | integer | yes | SQLite primary key |
| `gateway_request_id` | text | yes | Unique link to attempts |
| `created_at` | text | yes | UTC decision time |
| `metrics_as_of` | text | yes | Cutoff shared by all candidate metrics |
| `strategy` | text | yes | Versioned algorithm key, initially `weighted_measured_v1` |
| `requested_model` | text | yes | `auto` for V0.2 decision rows |
| `selected_provider` | text | yes | Highest-ranked candidate before attempts |
| `selected_model` | text | yes | Highest-ranked model before attempts |
| `selected_final_score` | real | yes | Final weighted score |
| `weights_json` | text/JSON | yes | Normalized quality/cost/latency/reliability weights |
| `candidates_json` | text/JSON | yes | Ordered structured explanations for all eligible candidates |

`candidates_json` is intentionally used instead of a candidate-score child table. V0.2 needs replayable explanations and recent-decision display, not cross-decision analytical joins. Validate the JSON with Pydantic before persistence and version it through `strategy`.

The initially selected candidate can differ from the provider that ultimately returns a response after fallback. `RequestRecord.selected` preserves the actual outcome; the decision preserves the original ranking.

Index `created_at DESC` for the recent-decision endpoint. Do not store natural-language explanations.

## ModelPricing

Manually configured pricing metadata used for estimates.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | integer | yes | SQLite primary key |
| `provider` | text | yes | Provider key |
| `model` | text | yes | Exact model identifier |
| `currency` | text | yes | `USD` in V0.2 |
| `input_price_per_million` | decimal text | yes | Configured input-token price |
| `output_price_per_million` | decimal text | yes | Configured output-token price |
| `effective_from` | text | yes | UTC effective timestamp |
| `effective_to` | text | no | Exclusive UTC end, if superseded |
| `source` | text | yes | Human-auditable source label/URL, not a claim of live sync |
| `configured_at` | text | yes | UTC metadata write timestamp |

Constraints:

- prices are non-negative
- currency is explicit
- effective intervals for one provider/model must not overlap
- matching uses the attempt start time and exact provider/model

Pricing updates create a new row and close the previous interval; historical request estimates retain their `pricing_id` and calculated amount. V0.2 does not revise old estimates when metadata changes.

## Minimal SQLite schema ownership

- A `schema_version` table contains one integer version.
- Startup opens the configured database and applies known forward-only schema steps in a transaction.
- Unknown newer schema versions fail startup with a clear error.
- Tests use a temporary SQLite file for persistence behavior and an in-memory database where restart behavior is irrelevant.
- WAL mode and a bounded busy timeout are connection settings, not new services.

## Data access surface

The store interface should stay narrow:

- initialize/validate schema
- append one request record
- append one routing decision
- upsert/version configured pricing metadata
- read recent records for a provider/model snapshot
- read Dashboard summary/provider/decision/failure views

Do not introduce a generic repository framework or ORM in V0.2. Parameterized `sqlite3` queries and small mapping functions are sufficient.

## Retention and privacy

V0.2 bounds routing calculations but does not need an automated deletion service. Document that the SQLite file grows with request volume and provide a manual/explicit retention decision before production-scale use. Automated retention, archival, and distributed storage are later concerns.

Never persist:

- prompts, messages, completions, tool arguments, or raw payloads
- API keys, cookies, authorization headers, or provider credentials
- raw provider error bodies that may contain request content
- user identity, tenant identity, or billing-account data
