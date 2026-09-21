# ModelPilot V0.4 — Benchmark-Driven Quality Routing

Status: staged implementation; V04-001 definitions/loading, V04-002 pure evaluators, V04-003
isolated runner, V04-004 independent benchmark persistence, V04-005 pure quality aggregation and
V04-006 optional overall quality routing and V04-007 read-only API/explicit CLI are
implemented. Stable application version remains 0.3.0; development SQLite schema is 4.
Quality routing requires an explicit suite path and defaults to disabled; no benchmark Dashboard yet.

## Why and user value

Operational cost, latency and reliability already have measured signals; quality still uses a
configured baseline. Dedicated, versioned benchmarks will provide evidence about specific tasks,
not a universal model leaderboard. Users should be able to trace a quality decision to a suite,
version, category, evaluator and complete run rather than an arbitrary score.

## Required scope

- Immutable benchmark definitions and explicit local UTF-8 JSON loading.
- Five deterministic evaluators, introduced in V04-002, not the definition task.
- Explicitly invoked runner using existing providers, separate from production request handling.
- Durable benchmark run/results storage, separate from production attempts and health updates.
- Quality snapshots scoped by provider/model/suite/version/category/evaluator configuration.
- Confidence-aware blending with static quality; missing/unreliable results retain static quality.
- Read-only benchmark inspection, explicit CLI execution, and a minimal Dashboard section.
- Compatibility and privacy tests, documentation and final release gates.

## Non-goals

No new providers (including Claude, Qwen, Ollama or vLLM), streaming, authentication, billing/payment,
Redis, PostgreSQL, Kubernetes, distributed circuit breaking, default LLM judges, web scraping,
automatic dataset downloads, ML Router training, user prompts as fixtures, or realtime pricing sync.
No translation/summarization quality claims and no generated-code execution sandbox in this version.
The smoke suite is original demonstration data, not scientific or industry benchmark evidence.

## V04-001 boundary

Implement only BenchmarkMessage, EvaluatorSpec variants, BenchmarkCase, BenchmarkSuite and local
dataset loading plus a 2–5 case smoke fixture. No provider calls, evaluator execution, runner, SQL,
API, Dashboard or Router changes. No benchmark results or measured scores are invented.

## Definition of Done for V0.4 (future tasks)

Every score is traceable to real benchmark outcomes and an immutable definition. Identical inputs
produce identical evaluator/aggregation results. Model generation itself may vary; record run
settings and evaluator version rather than promising identical provider output. Tests prove
small samples cannot dominate quality routing, missing data uses static quality, production
privacy and metrics stay isolated, health still gates candidates, and all V0.3 regression checks pass.

V04-001 is done when definition validation, immutable ordered loading, malformed-input rejection,
and all existing backend/frontend checks pass without changing the production path.
