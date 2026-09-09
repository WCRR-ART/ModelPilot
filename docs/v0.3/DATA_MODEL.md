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

## Persistence forecast

A later task may persist these fields in SQLite with `(provider, model)` as the unique key.
V03-001 defines no table and performs no migration. Health history can continue to be derived
from existing attempt records; the health table stores only the latest operational snapshot.

## Sensitive-data boundary

The model contains no prompt, completion, raw error body, API key, authorization value, user
identifier, or request payload.

