# ModelPilot V0.3 Data Model

## ProviderHealth

One record represents the circuit state of one provider/model pair.

| Field | Type | Nullable | Meaning |
| --- | --- | --- | --- |
| `provider` | non-empty string | no | normalized provider name |
| `model` | non-empty string | no | provider model identifier |
| `state` | `CLOSED` / `OPEN` / `HALF_OPEN` | no | current circuit state |
| `consecutive_failures` | non-negative integer | no | counted failures since last success |
| `opened_at` | aware datetime | yes | start of the current open cycle |
| `cooldown_until` | aware datetime | yes | inclusive half-open eligibility boundary |
| `last_failure_at` | aware datetime | yes | most recent counted circuit failure |
| `last_success_at` | aware datetime | yes | most recent successful outcome |
| `updated_at` | aware datetime | no | timestamp of the last state-changing event |

All datetimes are normalized to UTC. Naive datetimes are rejected.

## Invariants

- `consecutive_failures` is never negative.
- `CLOSED` has no active `opened_at` or `cooldown_until`.
- `OPEN` and `HALF_OPEN` require both `opened_at` and `cooldown_until`.
- `opened_at <= cooldown_until`.
- Recorded success/failure/open timestamps cannot be later than `updated_at`.
- Threshold and cooldown are transition inputs, not persisted model constants.

## Persistence (V03-002)

Schema version 3 adds `provider_health` in the existing ModelPilot SQLite database, with
`PRIMARY KEY (provider, model)`. The table stores exactly the nine fields above: canonical
uppercase state strings, integer failure counts, and UTC ISO-8601 timestamps with microsecond
precision. Optional timestamps remain SQL NULL. Threshold and cooldown duration are not stored.

`ProviderHealthStore` is an independent protocol exposing `get_health`, `upsert_health`, and
`list_health`. The existing `SQLiteMetricsStore` implements both store protocols, reusing its
database path, connection lifecycle, WAL, timeout, and transaction conventions. The
`MetricsStore` protocol itself is unchanged.

Fresh databases initialize at v3; existing v1 and v2 databases migrate transactionally to v3
without deleting attempts, pricing, or routing decisions. Failed migrations roll back.
Unsupported newer schema versions fail explicitly.

Upserts atomically replace the snapshot for a key; no history row is added. The last submitted
snapshot wins (there is no concurrency admission or stale-write arbitration in V03-002).
Missing keys return None. Lists sort by provider and model. Reads restore the exact stored
state, including expired OPEN circuits, without evaluating the clock or advancing transitions.
Malformed rows raise `ProviderHealthStoreDataError`; SQLite errors propagate to the caller.

## V03-006 routing health evidence

New automatic explanations use `routing_version = "v0.3"`; the gateway release/health version
stays 0.2.0. `RoutingSignal.health` reuses the typed `HealthEligibility` object and its stable
reason values. Fields are state (nullable), eligible, reason, probe, cooldown_until (nullable
UTC datetime), and consecutive_failures (nullable). Missing records have CLOSED/health_unknown;
store failures have null state/health_store_unavailable, never a fabricated healthy state.

`RoutingExplanation.candidates` contains the final ranked eligible candidates. Separate
`excluded_candidates` entries contain only provider, model, and health: no rank or final_score.
At execution time, busy probes move to exclusions; remaining ranks are contiguous and selected
is the first remaining candidate. Scoring component values are not recomputed or changed.
Selected and served remain distinct when an actual attempt fails before successful fallback.

Health evidence describes admission for this request, not the post-outcome circuit state.
An acquired probe keeps HALF_OPEN/half_open_probe_acquired/probe=true even if its outcome
subsequently closes or reopens the circuit. Uncalled probes retain probe=false and eligibility
evidence. Busy probes are ineligible with half_open_probe_in_flight/probe=false.

All-excluded explanations have empty candidates and null selected fields. They are retained
on the internal `NoProviderAvailable.routing` exception, while the HTTP 503 shape is unchanged.
They are not inserted into routing_decisions, whose selected columns remain non-nullable.
Other final explanations persist in the existing explanation_json; schema remains 3.
Old v0.2 JSON reads with health=None and excluded_candidates=(). No history is backfilled.
Explicit model requests still produce no automatic explanation.

## Sensitive-data boundary

The model contains no prompt, completion, raw error body, API key, authorization value, user
identifier, or request payload.
