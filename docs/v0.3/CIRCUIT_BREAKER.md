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

`HALF_OPEN` represents eligibility, not an unlimited traffic state. V03-005 coordinates at most
one in-flight recovery probe per provider/model pair in the supported single-process deployment.
The V03-001 domain itself remains free of locks, leases, and Router filtering.

## Determinism

Given the same snapshot, event, threshold, cooldown, and timestamp, a transition returns the
same serialized snapshot. Re-evaluating an unchanged state at the same time is idempotent.

## V03-004 routing eligibility

Automatic ranking reads each configured provider/model health at most once per ranking,
using one injected evaluation timestamp. CLOSED and missing records are eligible. OPEN is
excluded before scoring until the inclusive cooldown boundary; expired OPEN and HALF_OPEN
are eligible without a score adjustment. Eligibility evaluation does not write state.
Completed outcomes continue to drive persisted transitions through the existing manager.

Health read or validation failure logs a warning without exception content and fails open.
Structured internal evidence includes eligibility, effective state, and a stable reason;
excluded candidates have no fabricated score. V03-006 exposes this evidence in HTTP explanations.
Per-key reads share a timestamp but are not an atomic cross-key database snapshot.

Explicit model requests retain their existing exact model matching and fallback semantics;
health filtering applies only to automatic routing. No new provider/model syntax is added.
When all automatic candidates are OPEN before cooldown, the existing no-provider path returns
503; no blocked candidate is reinstated. Duplicate provider/model candidates are attempted
at most once.

## V03-005 single-process probe coordination

The composition root creates one `HalfOpenProbeCoordinator` per application gateway and
injects it into the shared service. A short standard-library mutex protects a map of leases
keyed by `(provider, model)`. The mutex is never held during provider IO or persistence.
Requests do not wait for another probe's response: a busy candidate is skipped immediately.

Ranking remains side-effect free. Immediately before an automatic provider call, the service
re-reads that candidate's health with an injected current time. This execution check is separate
from the request's ranking snapshot and prevents stale rankings from bypassing a new cooldown.
Only effective HALF_OPEN candidates claim a lease. Claims produce structured reasons
`half_open_probe_acquired` or `half_open_probe_in_flight`; no scoring bonus or penalty is added.

The lease surrounds the actual call and attempt/health updates, and is released in `finally`,
including cancellation and unexpected exceptions. Unused candidates never reserve leases.
An owner identity check makes repeated or stale releases harmless. If every candidate is
skipped without any provider attempt, the existing 503 unavailable path is used.

Probe success/failure uses the existing health manager. Authentication errors retain the
existing non-counted behavior (HALF_OPEN may remain), while the lease is still released.
Health persistence failure still returns a successful inference and releases the lease; a
later request may probe again if the persisted state has not advanced. Health read failures
retain fail-open behavior, so protection cannot be guaranteed while the health store is unavailable.

Explicit model requests bypass automatic probe coordination. Leases are process-local only,
not shared across workers or replicas, and disappear on process exit. No lease data is stored
in SQLite and the schema remains version 3.

## V03-006 explanations

The existing modelpilot.routing extension now includes health evidence on ranked candidates
and an excluded_candidates list without invented scores or ranks. Execution admission updates
the final explanation before persistence/response: acquired probes are marked as executed,
busy probes move to exclusions, and selected is the first remaining ranked candidate.
Successful fallback still records the original eligible selection separately from served_by.
Health-read failure remains fail-open, represented by a null state and
health_store_unavailable. Cooldown timestamps and failure counts reflect the actual snapshot
used for eligibility, not the resulting health state after the provider completes.

New explanation version is v0.3; SQLite remains schema 3 and old v0.2 decisions remain readable.
All-unavailable evidence is retained internally on the existing unavailable exception without
changing its HTTP response. See DATA_MODEL.md for the exact compatibility rules.
