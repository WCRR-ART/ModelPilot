# V0.2 Implementation Tasks

Each task is intended to fit one focused pull request or commit. File paths are forecasts, not authorization to change unrelated code. Tests should be written with the behavior they protect.

## V02-001 — Define metrics domain contracts

- **Goal:** Introduce typed, provider-neutral domain models for usage availability, attempt records, provider metrics snapshots, routing explanations, and pricing metadata, plus a narrow metrics-store interface.
- **Files likely affected:** `backend/src/modelpilot/schemas.py`, new `backend/src/modelpilot/metrics/models.py`, new `backend/src/modelpilot/metrics/store.py`, tests under `backend/tests/`.
- **Tests required:** Pydantic validation for nullable usage, non-negative tokens/latency/prices, cost status invariants, explanation score bounds, and protocol/fake-store behavior.
- **Dependency:** None.
- **Acceptance criteria:** No persistence or routing behavior changes; missing usage is represented as unavailable rather than zero; models contain no prompt/response/credential fields; existing tests pass.

## V02-002 — Add minimal SQLite schema and store

- **Goal:** Persist `RequestRecord`, `RoutingDecision`, and versioned `ModelPricing` using standard-library SQLite.
- **Files likely affected:** new `backend/src/modelpilot/metrics/sqlite.py`, `backend/src/modelpilot/config.py`, `backend/src/modelpilot/main.py`, `.env.example`, backend tests.
- **Tests required:** empty-database initialization, schema-version validation, insert/read round trips, restart persistence using a temporary file, uniqueness/constraint failures, unknown-newer-schema failure.
- **Dependency:** V02-001.
- **Acceptance criteria:** WAL and busy timeout are configured; SQL is parameterized; transactions are short; database path is configurable; no ORM, migration framework, PostgreSQL, or Redis is introduced.

## V02-003 — Normalize provider usage outcomes

- **Goal:** Make the provider boundary expose real nullable input/output/total usage consistently without changing the public OpenAI-compatible payload.
- **Files likely affected:** `backend/src/modelpilot/providers/base.py`, `openai_compatible.py`, `gemini.py`, provider tests.
- **Tests required:** OpenAI-compatible usage present/missing/malformed; Gemini usage present/missing; absence maps to unavailable/`None`; original response compatibility remains intact.
- **Dependency:** V02-001.
- **Acceptance criteria:** No token estimator is added; missing usage is never zero-filled; no new Provider is added.

## V02-004 — Record completed provider attempts

- **Goal:** Capture UTC start/end, monotonic latency, outcome, nullable usage, bounded error category, attempt order, and selected outcome for every attempted Provider.
- **Files likely affected:** `backend/src/modelpilot/service.py`, metrics store integration in `main.py`, logging/error mapping modules, service tests.
- **Tests required:** first-attempt success, failure then fallback success, all failures, usage unavailable, metrics-store write failure after provider success, no sensitive payload persistence.
- **Dependency:** V02-002, V02-003.
- **Acceptance criteria:** One durable record per completed attempt; fallback behavior is unchanged; a metrics write failure cannot convert a successful LLM response into failure; prompt/response bodies are not stored.

## V02-005 — Add configured pricing and cost estimates

- **Goal:** Version manually configured pricing metadata and calculate post-response estimated cost from real reported input/output tokens.
- **Files likely affected:** metrics models/store, new pricing service/configuration module, `.env.example` or a documented pricing configuration file, cost tests.
- **Tests required:** decimal calculation, input/output price distinction, effective-time selection, missing usage, missing pricing, zero usage, pricing version rollover, historical estimate stability.
- **Dependency:** V02-002, V02-003, V02-004.
- **Acceptance criteria:** Estimates use decimal arithmetic; unavailable inputs yield `NULL` with a reason; metadata source/effective time is retained; no live pricing scraper or billing claim is added.

## V02-006 — Compute recent provider metrics

- **Goal:** Produce deterministic `ProviderMetrics` from the newest 100 attempts within seven days.
- **Files likely affected:** new `backend/src/modelpilot/metrics/reader.py`, SQLite queries, metrics tests.
- **Tests required:** empty/one/even/odd windows, nearest-rank p50/p95, 100-record cap, seven-day cutoff, success/failure counts, unavailable usage/cost exclusion, UTC boundaries, deterministic tie ordering.
- **Dependency:** V02-002, V02-004, V02-005.
- **Acceptance criteria:** Average, rolling average, p50, p95, success rate, token totals, cost totals, unavailable counts, and sample counts follow `ROUTING_METRICS.md`; aggregates are derived rather than stored.

## V02-007 — Implement confidence-blended dynamic scoring

- **Goal:** Combine static quality/baselines with measured latency, reliability, and historical estimated cost while keeping ranking deterministic and explainable.
- **Files likely affected:** `backend/src/modelpilot/router.py`, metrics reader/model modules, router tests.
- **Tests required:** fewer than five samples, confidence ramp at 5/49/50 samples, missing per-metric data, bounded normalization, preference weights, candidate-order independence, stable provider-name tie-break, frozen snapshot behavior.
- **Dependency:** V02-006.
- **Acceptance criteria:** Formula exactly matches `ROUTING_METRICS.md`; no ML/AI Router; identical inputs produce identical ordering; explicit model behavior remains unchanged; ranking is computed once per request.

## V02-008 — Persist and return structured route explanations

- **Goal:** Store the automatic decision and include its structured evidence in the existing `modelpilot` response extension.
- **Files likely affected:** `backend/src/modelpilot/router.py`, `service.py`, `schemas.py`, metrics store, API/service tests.
- **Tests required:** selected candidate, all considered candidates, component sources, raw/effective scores, confidence, sample counts, weights, final score, static fallback explanations, fallback outcome versus initial selection.
- **Dependency:** V02-007.
- **Acceptance criteria:** Every successful `model="auto"` response has a versioned structured explanation; no natural-language-only reason; OpenAI-compatible fields remain unchanged; explicit requests are not forced into automatic decision records.

## V02-009 — Add read-only metrics API

- **Goal:** Serve the minimal accurate data needed by the Dashboard: summary, provider metrics, recent decisions, and recent failures.
- **Files likely affected:** `backend/src/modelpilot/main.py` or a small metrics API module, response schemas, metrics reader, API tests.
- **Tests required:** empty database, populated summary, UTC "today" boundary, pagination/limit bounds, nullable values, recent ordering, no prompt/error-body leakage, CORS behavior unchanged.
- **Dependency:** V02-006, V02-008.
- **Acceptance criteria:** Endpoints are read-only, bounded, typed, and backed by SQLite facts; zero counts and unavailable measurements are distinct; no admin write API or authentication system is introduced.

## V02-010 — Replace Dashboard placeholders with metrics

- **Goal:** Display requests today, average latency, success rate, estimated cost, requests by provider, routing decisions, and recent failures from the read-only API.
- **Files likely affected:** `frontend/app/page.tsx`, `frontend/app/globals.css`, small typed frontend API helpers/tests if introduced.
- **Tests required:** TypeScript typecheck, ESLint, production build; unit tests only if a frontend test runner is deliberately added in a separate justified decision.
- **Dependency:** V02-009.
- **Acceptance criteria:** No hard-coded operational metric values remain; loading/empty/error/unavailable states are explicit; no complex chart library or large UI redesign; health status continues to work.

## V02-011 — End-to-end hardening and documentation

- **Goal:** Validate the complete V0.2 slice, document configuration and caveats, and prepare release evidence.
- **Files likely affected:** backend integration tests, README files, `.env.example`, `docs/v0.2/`, `.github/workflows/ci.yml` only if an existing validation command is missing.
- **Tests required:** v0.1 regression suite, SQLite temporary-file integration flow, automatic route/fallback/explanation flow, pytest, Ruff, frontend lint/typecheck/build, npm audit.
- **Dependency:** V02-004 through V02-010.
- **Acceptance criteria:** Definition of Done in `SCOPE.md` is met; documented commands run unchanged in CI; no out-of-scope capability appears; pricing and metric limitations are prominent; repository security scan is clean.

## Recommended PR order

```text
V02-001
  -> V02-002
  -> V02-003
  -> V02-004
  -> V02-005
  -> V02-006
  -> V02-007
  -> V02-008
  -> V02-009
  -> V02-010
  -> V02-011
```

V02-002 and V02-003 may be developed independently after V02-001, but they should merge before attempt recording begins. Do not create GitHub Issues from this plan until explicitly requested.
