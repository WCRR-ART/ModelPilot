# ModelPilot V0.2 — Routing Intelligence Foundation

## Status

Implementation complete locally for the v0.2.0 release candidate. Remote CI, tagging, and release
publication remain separate post-push gates.

## Why V0.2 exists

ModelPilot v0.1.0 proves the gateway, provider boundary, deterministic `model="auto"` path, and ordered fallback. Its routing inputs are static baseline estimates, so they cannot reflect the behavior of a provider in the running installation.

V0.2 exists to turn real completed provider attempts into conservative routing signals. It is an observability-and-decision-quality release, not a provider-expansion or platform-infrastructure release.

## User value

- Operators can see actual latency, success rate, token usage, and estimated cost for configured providers.
- Automatic routing can adapt gradually to measured latency and reliability without becoming opaque or unstable.
- Every automatic decision has structured evidence showing the inputs, sources, confidence, weights, and final score.
- Metrics survive a process restart on a single ModelPilot instance.
- Missing usage and pricing data remain visibly unavailable instead of being invented.

## Required capabilities

1. Record one `RequestRecord` for every completed provider attempt, including provider, model, UTC start/end timestamps, monotonic duration, success, and a bounded error category.
2. Persist provider-reported input, output, and total token counts when present. Store `NULL` when usage is absent or invalid.
3. Maintain configured `ModelPricing` metadata and calculate estimated request cost only when both applicable pricing and required usage values exist.
4. Use a minimal SQLite metrics store with short transactions and query-time aggregates.
5. Calculate per-provider/model recent-window request count, successes, failures, average latency, p50 latency, p95 latency, success rate, and estimated cost.
6. Combine static quality with confidence-blended measured latency, reliability, and cost signals in a deterministic route score.
7. Return a structured route explanation for every successful `model="auto"` request without breaking the OpenAI-compatible response body.
8. Expose read-only metrics data required by the V0.2 dashboard: today's request count and estimated cost, average latency, success rate, requests by provider, recent routing decisions, and recent failures.
9. Update the dashboard to display accurate values and explicit unavailable/empty states using those read-only endpoints.

## Non-goals

V0.2 will not add:

- Claude, Qwen, Ollama, vLLM, or any other provider
- MCP, an AI router, ML-based routing, or neural routing
- benchmark automation, provider crawling, or live pricing scraping
- users, tenants, API-key management, authentication, billing, or payments
- PostgreSQL, Redis, Kubernetes, or a distributed gateway
- streaming, WebSocket, semantic cache, or prompt cache
- fake token counts, fake benchmark results, or inferred provider billing

## Compatibility constraints

- Existing OpenAI-compatible request fields and provider behavior remain valid.
- Existing explicit-model routing remains deterministic.
- Route metadata stays in the existing top-level `modelpilot` response extension.
- Provider credentials and prompt/message bodies are not written to the metrics database.
- A metrics write failure must be observable but must not turn a successful provider response into a failed chat completion.

## Acceptance criteria

- A successful and a failed provider attempt each produce the expected durable record.
- Provider timestamps use UTC for persistence; latency uses a monotonic clock for measurement.
- Rolling aggregates are calculated from the most recent 100 attempts per provider/model, limited to the previous seven days.
- Average, p50, and p95 calculations have documented, deterministic behavior for empty and small samples.
- Success rate includes every completed attempt in the window and is confidence-blended with the static prior for routing.
- Usage fields are nullable; unavailable usage never becomes zero.
- Estimated cost is nullable and is produced only from configured pricing metadata plus real provider-reported usage.
- Automatic ranking produces identical results for identical candidates, preferences, metrics snapshots, and pricing.
- Automatic responses include structured component scores, source labels, sample counts, confidence, weights, final score, and considered candidates.
- Cold-start providers remain routable using static baselines.
- Outliers and a single failure cannot immediately dominate routing.
- Dashboard summary, provider metrics, routing decisions, and recent failures are backed by persisted data rather than static UI constants.
- Tests cover migrations/schema creation, record writes, rolling aggregates, missing usage/pricing, scoring, tie-breaking, fallback, explanations, and read-only metrics endpoints.
- Existing v0.1.0 tests continue to pass.

## Definition of Done

- All required capabilities above are implemented in small reviewed changes.
- SQLite is the only new persistence dependency; no external service is required.
- The schema has a minimal versioning/migration mechanism and creates cleanly on an empty database.
- Pricing metadata identifies its configured source and effective date and is documented as an estimate.
- API schemas and examples distinguish measured, configured, unavailable, and estimated values.
- Backend tests and Ruff pass; frontend lint, typecheck, build, and npm audit pass; GitHub Actions pass.
- Documentation describes storage location, retention/window behavior, cost caveats, routing formula, and rollback expectations.
- No item from Non-goals is introduced incidentally.
