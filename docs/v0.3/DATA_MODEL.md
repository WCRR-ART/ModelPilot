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

## Sensitive-data boundary

The model contains no prompt, completion, raw error body, API key, authorization value, user
identifier, or request payload.
