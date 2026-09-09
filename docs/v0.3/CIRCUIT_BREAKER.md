# Circuit Breaker Specification

## States

- `CLOSED`: normal traffic is eligible.
- `OPEN`: normal traffic is blocked until the cooldown deadline.
- `HALF_OPEN`: a bounded recovery probe is eligible.

State is maintained independently for every `(provider, model)` pair.

## Counted failures

The following normalized error types count toward the circuit:

- `timeout`
- `connection_error`
- `rate_limit`
- `provider_error`
- `invalid_response`
- `unknown_error`

`unknown_error` counts conservatively. Although its cause is not classified, repeated unknown
failures mean the current provider path is not serving usable responses.

`authentication_error` does not count. A cooldown does not repair missing or invalid
credentials, so authentication problems should be surfaced through configuration and
operational diagnostics rather than provider-health flapping.

## Transition table

| Current state | Event | Next state | Effect |
| --- | --- | --- | --- |
| `CLOSED` | success | `CLOSED` | reset consecutive failures to zero |
| `CLOSED` | counted failure below threshold | `CLOSED` | increment consecutive failures |
| `CLOSED` | counted failure reaches threshold | `OPEN` | set open time and cooldown deadline |
| `CLOSED` | ignored failure | `CLOSED` | no health counters change |
| `OPEN` | time before cooldown | `OPEN` | remain ineligible |
| `OPEN` | time at/after cooldown | `HALF_OPEN` | become eligible for a bounded probe |
| `HALF_OPEN` | success | `CLOSED` | reset consecutive failures and clear cooldown |
| `HALF_OPEN` | counted failure | `OPEN` | increment failures and restart cooldown |
| `HALF_OPEN` | ignored failure | `HALF_OPEN` | no health counters change |

An outcome must not be recorded directly against `OPEN` before its cooldown ends because no
provider call is eligible in that state.

## Boundary semantics

- Cooldown expiration uses `now >= cooldown_until`.
- A zero cooldown opens the circuit for the failure transition, then makes it eligible for
  half-open evaluation at the same timestamp.
- Threshold means the number of consecutive counted failures required to open the circuit.
- Success resets the consecutive count but retains last success/failure timestamps for
  observability.
- Time never comes from `sleep`; callers inject `now` or a clock result.
- Inputs earlier than `updated_at` are rejected to prevent time-reversing transitions.

## Probe semantics

`HALF_OPEN` represents eligibility, not an unlimited traffic state. A later integration task
must coordinate at most one in-flight recovery probe per provider/model pair in the supported
single-process deployment. V03-001 deliberately does not implement locks, leases, or Router
filtering.

## Determinism

Given the same snapshot, event, threshold, cooldown, and timestamp, a transition returns the
same serialized snapshot. Re-evaluating an unchanged state at the same time is idempotent.

