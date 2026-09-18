# ModelPilot V0.3 Implementation Tasks

Each task is one focused commit or pull request. Dependencies are explicit so circuit behavior
is testable before it affects production routing.

All eight local implementation tasks are complete. See [COMPLETION.md](COMPLETION.md) for
implementation/test evidence and release gates. Version v0.3.0 has now been officially published.

## V03-001 — Provider Health Domain Model & State Machine

- **Goal:** Define immutable provider/model health snapshots, failure classification, and
  deterministic `CLOSED`/`OPEN`/`HALF_OPEN` transitions.
- **Tests:** initial state, threshold boundaries, cooldown boundaries, success/failure probe
  outcomes, every normalized error class, configuration validation, aware time, serialization,
  and repeated deterministic transitions.
- **Acceptance:** no persistence, Router, API, Dashboard, or provider behavior changes.

## V03-002 — Persist Provider Health State

- **Goal:** Add versioned SQLite storage for the latest health snapshot of each provider/model
  pair while preserving V0.2 data.
- **Dependency:** V03-001.
- **Tests:** schema upgrade, insert/update/read, restart durability, UTC round trip, and safe
  handling of newer schema versions.
- **Acceptance:** standard-library SQLite only; short parameterized transactions; no Router
  integration.

## V03-003 — Update Health from Provider Outcomes

- **Goal:** Apply and persist one health transition after every completed normalized provider
  outcome without changing fallback behavior.
- **Dependency:** V03-002.
- **Tests:** success/failure updates, ignored authentication errors, all-fail fallback, health
  write failure isolation, and shared deterministic timestamps.
- **Acceptance:** inference success is never replaced by a health persistence failure.

## V03-004 — Filter Open Circuits During Routing

- **Goal:** Exclude open provider/model circuits before applying the existing V0.2 scoring.
- **Dependency:** V03-003.
- **Tests:** open exclusion, closed ranking unchanged, explicit-model behavior, all-open error
  compatibility, immutable cutoff time, and deterministic ordering.
- **Acceptance:** scoring formula remains unchanged; no hidden retries or new provider behavior.

## V03-005 — Coordinate Half-Open Recovery Probes

- **Goal:** Allow one bounded half-open probe per provider/model pair and prevent ordinary
  traffic from flooding a recovering provider.
- **Dependency:** V03-004.
- **Tests:** single probe admission, concurrent denial, success close, failure reopen, cooldown
  restart, and process-local recovery after completion.
- **Acceptance:** coordination matches the supported single-process/single-host boundary.

## V03-006 — Add Structured Health Explanations

- **Goal:** Extend automatic route explanations with structured eligibility and circuit-state
  evidence, including selected-versus-served fallback behavior.
- **Dependency:** V03-004, V03-005.
- **Tests:** closed/open/half-open candidates, excluded reasons, probe selection, serialization,
  and OpenAI-compatible response regression.
- **Acceptance:** explanations are structured and versioned; no natural-language-only reason.

## V03-007 — Add Read-Only Health API and Dashboard Status

- **Goal:** Expose bounded health snapshots and show provider circuit state in the existing
  Dashboard without adding control endpoints.
- **Dependency:** V03-006.
- **Tests:** empty/populated API, stable ordering, nullable timestamps, no sensitive fields,
  frontend lint/typecheck/build, and explicit loading/error states.
- **Acceptance:** read-only observability only; no manual trip/reset API or broad UI redesign.

## V03-008 — End-to-End Hardening and Release Documentation

- **Goal:** Validate failure-to-open-to-probe-to-recovery behavior, document configuration and
  operational limits, and prepare V0.3 release evidence.
- **Dependency:** V03-002 through V03-007.
- **Tests:** V0.2 regression, SQLite restart, fallback with open circuits, half-open recovery,
  pytest, Ruff, frontend lint/typecheck/build, and npm audit.
- **Acceptance:** V0.3 scope is complete, secure, deterministic, and documented without adding
  out-of-scope platform features.

## Recommended order

```text
V03-001 -> V03-002 -> V03-003 -> V03-004
         -> V03-005 -> V03-006 -> V03-007 -> V03-008
```
