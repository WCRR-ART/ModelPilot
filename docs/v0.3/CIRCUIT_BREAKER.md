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

## V03-004 routing eligibility

Automatic routing reads each configured provider/model health at most once per request,
using one injected evaluation timestamp. CLOSED and missing records are eligible. OPEN is
excluded before scoring until the inclusive cooldown boundary; expired OPEN and HALF_OPEN
are eligible without a score adjustment. Eligibility evaluation does not write state.
Completed outcomes continue to drive persisted transitions through the existing manager.

Health read or validation failure logs a warning without exception content and fails open.
Structured internal evidence includes eligibility, effective state, and a stable reason;
excluded candidates have no fabricated score. HTTP explanations are unchanged in this task.
Per-key reads share a timestamp but are not an atomic cross-key database snapshot.

Explicit model requests retain their existing exact model matching and fallback semantics;
health filtering applies only to automatic routing. No new provider/model syntax is added.
When all automatic candidates are OPEN before cooldown, the existing no-provider path returns
503; no blocked candidate is reinstated. Duplicate provider/model candidates are attempted
at most once. HALF_OPEN concurrent admission remains deferred to V03-005.
