# ModelPilot V0.3 Scope

## Theme

ModelPilot V0.3 adds provider health tracking and deterministic circuit breaking. When a
provider/model pair fails repeatedly, ModelPilot should temporarily stop sending it new
traffic, then allow a bounded recovery probe after a cooldown.

## Goals

- Track health independently for each `(provider, model)` pair.
- Represent `CLOSED`, `OPEN`, and `HALF_OPEN` as an explicit state machine.
- Count only normalized failure categories that can indicate provider unavailability.
- Keep thresholds and cooldowns configurable outside the domain model.
- Make every transition deterministic with an injected timezone-aware timestamp.
- Persist health state safely and exclude open circuits from automatic routing.
- Preserve explicit fallback behavior and the OpenAI-compatible API contract.
- Expose bounded health observability after routing integration is stable.

## V03-001 boundary

The first task implements only the immutable health domain model, failure classification,
and pure state transitions. It does not read or write SQLite, alter routing, add API routes,
or change the Dashboard.

## Non-goals

V0.3 does not add:

- new providers
- streaming
- authentication, authorization, billing, or multi-tenancy
- Redis, PostgreSQL, an ORM, or distributed consensus
- ML/AI routing or a benchmark engine
- automatic provider credential repair
- cross-host circuit coordination

## Compatibility requirements

- Existing OpenAI-compatible request and response fields remain unchanged.
- Existing confidence-blended scoring remains deterministic.
- Circuit eligibility filters automatic candidates without changing the scoring formula.
- Metrics or health persistence failures must not turn a successful inference into failure.
- No prompts, completions, API keys, authorization headers, or raw provider bodies are stored.

## Definition of Done

- Health behavior is specified by an explicit, tested state machine.
- State is durable across a local process restart.
- Routing avoids an open circuit and admits only bounded half-open recovery probes.
- Successful probes close the circuit; failed probes restart the cooldown.
- Health decisions are observable without exposing sensitive request data.
- V0.2 regression tests, Ruff, frontend lint, typecheck, build, and security checks pass.
