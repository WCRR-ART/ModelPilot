# ModelPilot v0.2.0 Completion Checklist

Status: local release candidate complete on 2026-09-09. Remote CI, tag creation, and GitHub Release
publication must occur only after the release-preparation commit is pushed.

## Required capabilities

- [x] Provider-neutral attempt, pricing, metrics, and routing-explanation domain models
- [x] SQLite schema v2 with WAL, version checks, v1-to-v2 migration, and reopen persistence
- [x] Nullable Provider usage and bounded error normalization
- [x] One durable record for each completed success or failure attempt
- [x] Decimal cost estimates only when configured pricing and actual token usage are available
- [x] Recent provider metrics from at most 100 attempts per provider/model within seven days
- [x] Deterministic confidence-blended latency, reliability, and cost routing
- [x] Static-only quality signal and static fallback for missing measured dimensions
- [x] Persisted structured route explanations with selected and served routes kept separate
- [x] Bounded, read-only summary, provider, decision, and failure Metrics APIs
- [x] Dashboard backed by real metrics with loading, empty, error, and unavailable states
- [x] Existing explicit-model behavior and OpenAI-compatible response fields preserved

## End-to-end evidence

`backend/tests/test_v02_end_to_end.py` drives fake Providers through the HTTP API, Gateway service,
Router, cost estimation, SQLite persistence, routing decisions, and Metrics API. It covers:

- [x] Single-provider success with usage, latency, cost, decision, summary, provider metrics, and logs
- [x] One failure followed by success, with shared request ID and selected/served distinction
- [x] Two failures followed by success, with stable attempt order and readable failure metrics
- [x] All providers failing while every attempt and the unserved decision remain durable
- [x] Metrics write failure isolation and sanitized warning output
- [x] Cold start below five samples retaining static order
- [x] Cost-, latency-, and reliability-prioritized dynamic ranking from persisted history
- [x] Missing cost metrics retaining the static cost signal while other dimensions remain measured
- [x] Explicit-model compatibility without an automatic decision record

## Database and API evidence

- [x] Fresh schema creation, schema version 2, WAL mode, and reopen behavior
- [x] v1-to-v2 migration preserves attempts and pricing and creates routing decisions
- [x] `/health`, `/v1/chat/completions`, `/v1/logs`, and all four Metrics APIs are tested
- [x] Metrics query bounds, Decimal strings, UTC timestamps, and read-only methods are tested
- [x] Metrics responses exclude prompt, completion, credentials, and raw provider errors
- [x] No database file is tracked by Git

## Local validation evidence

- [x] Backend pytest: 228 passed
- [x] Backend Ruff: passed
- [x] Frontend clean install: `npm ci` passed
- [x] Frontend ESLint: passed
- [x] Frontend TypeScript: passed
- [x] Frontend production build: passed
- [x] npm audit: 0 vulnerabilities
- [x] Dashboard development route: HTTP 200 (validated for this release candidate)
- [x] CI workflow runs pytest, Ruff, npm ci, ESLint, TypeScript, and production build

## Release gates

- [x] Source and frontend package versions set to `0.2.0`
- [x] `/health` reports `0.2.0`
- [x] V0.2 limitations, privacy, metrics windows, cost caveat, and rollback behavior documented
- [ ] Push the release-preparation commit to `main`
- [ ] Wait for GitHub Actions on that commit to pass
- [ ] Create annotated tag `v0.2.0` at the verified commit
- [ ] Publish the non-draft, non-prerelease GitHub Release
