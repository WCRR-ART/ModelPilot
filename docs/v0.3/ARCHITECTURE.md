# ModelPilot V0.3 Architecture

## Read-only health observability (V03-007)

`GET /v1/health/providers` returns a sorted list of configured provider/model candidates,
combined with persisted health snapshots. It is separate from service liveness at `/health`.
Missing records use `CLOSED`, `health_unknown`, zero consecutive failures and null timestamps.
Disabled providers and stale records outside the configured registry are excluded.

The API reuses `health_eligibility` at one clock instant: an expired OPEN snapshot becomes an
effective HALF_OPEN view without updating SQLite. Store errors return a sanitized 503 and
warning, not fabricated healthy data. Only provider/model identity and health metadata are exposed.
There are no health mutation, reset, or manual probe endpoints. Schema version remains 3.

`probe_in_flight` is a lock-protected read of the existing process-local coordinator, never
an acquisition or release. It is not persisted and cannot represent other worker processes.
Snapshots can become stale immediately after reading; they are not admission guarantees.

The existing Dashboard shows Healthy (CLOSED), Open (OPEN), and Recovering (HALF_OPEN), with
missing records explicitly labeled No health record. It displays consecutive failures,
absolute UTC cooldown deadlines, local-time last success/failure, and Ready to probe or Probe
in progress. Null timestamps display an em dash. The existing manual Refresh reloads health;
there is no polling or write control. Loading, empty and error states are isolated from other
metrics sections. V0.3 remains under development, not a published stable release.

## Design principle

Provider health is a separate domain from performance scoring. Metrics describe how a
provider has behaved; the circuit breaker decides whether a provider/model pair is currently
eligible to receive traffic. A high routing score never overrides an open circuit.

## Components

```text
normalized ProviderOutcome
          |
          v
+----------------------+       +----------------------+
| Health state machine |------>| ProviderHealthStore  |
| pure transitions     |       | SQLite (later task)  |
+----------+-----------+       +----------+-----------+
           |                              |
           | immutable health snapshot    |
           v                              v
+-----------------------------------------------------+
| Router eligibility filter                          |
| CLOSED: eligible                                   |
| OPEN: excluded until cooldown                      |
| HALF_OPEN: bounded recovery probe only             |
+--------------------------+--------------------------+
                           |
                           v
                  existing deterministic scoring
                           |
                           v
                  provider attempt + fallback
```

## Domain boundary

`modelpilot.health` owns:

- `CircuitState`
- `ProviderHealth`
- circuit-failure classification
- time-driven and outcome-driven transitions

The domain receives a failure threshold, cooldown duration, and timezone-aware `now` value.
It does not read environment variables, call a database, inspect FastAPI requests, or invoke
providers.

## Identity and snapshots

Health is keyed by `(provider, model)`, matching routing candidates and provider metrics.
Consumers operate on immutable snapshots. The timestamp used for a routing decision is read
once, so every candidate is evaluated against the same instant.

## Request flow after integration

1. Capture one timezone-aware decision timestamp.
2. Load health snapshots for configured candidates.
3. Advance expired `OPEN` snapshots to `HALF_OPEN` deterministically.
4. Exclude `OPEN` candidates and enforce the half-open probe limit.
5. Apply the existing V0.2 scoring to eligible candidates without changing its formula.
6. Record each normalized provider outcome in metrics as today.
7. Apply the corresponding health transition and persist the new snapshot.
8. Continue existing fallback behavior using the remaining eligible candidates.

V03-001 implements steps 3 and 7 only as pure domain operations. Persistence, routing, and
probe coordination are separate tasks.

## Failure isolation

- Health-store failures are logged with bounded metadata and never expose secrets.
- A successful provider response remains successful if health persistence fails.
- Authentication failures do not open a circuit because cooldown cannot repair credentials.
- Repeated unknown failures count conservatively because the provider path is not usable.
- Multi-process atomic probe leasing is outside the single-host V0.3 deployment boundary.

## Configuration boundary

Threshold and cooldown values belong to application configuration and are passed into the
state machine. Domain defaults are intentionally absent. Validation rejects thresholds below
one and negative cooldowns; a zero cooldown is valid and immediately permits half-open
evaluation.
