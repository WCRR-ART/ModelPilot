# V0.3 Local Completion Checklist

Status: local release preparation and the subsequent publication workflow are complete.
Published stable version: v0.3.0, commit `3cfc7ae21fe74a9805fe6b6c49178d5ebaa4d6d9`.

## Task evidence

| Task | Implementation | Validation evidence (backend/tests) | Result |
| --- | --- | --- | --- |
| V03-001 | health/models.py | test_provider_health.py: 26 | PASS |
| V03-002 | health/store.py, metrics/sqlite_store.py | test_sqlite_provider_health_store.py: 20 | PASS |
| V03-003 | health/manager.py, service.py, config.py | test_provider_health_updates.py: 31 | PASS |
| V03-004 | health/eligibility.py, router.py | test_health_aware_routing.py: 29 | PASS |
| V03-005 | health/probes.py, service.py admission | test_half_open_probes.py: 27 | PASS |
| V03-006 | metrics/models.py health explanations | test_health_explanations.py: 16 | PASS |
| V03-007 | health/api.py, Dashboard and API helper | test_health_api.py: 16; frontend checks | PASS |
| V03-008 | release tests, bilingual README, version metadata, release draft | test_v03_end_to_end.py: 6; all gates below | PASS |

Paths in the implementation column are relative to backend/src/modelpilot unless stated otherwise.
V0.2 regression coverage: 228 tests; total: **399 passed**.

## End-to-end release gates

| Scenario | Evidence | Result |
| --- | --- | --- |
| Threshold 3, health API and persisted timestamps | test_v03_end_to_end: HTTP timeout sequence 1/2/3 | PASS |
| OPEN isolation, unscored exclusions, healthy fallback | release HTTP restart test; test_health_aware_routing | PASS |
| SQLite reopen retains OPEN and cooldown | release HTTP restart test; SQLite health suite | PASS |
| Before / exactly at / after cooldown | release HTTP test; eligibility and Health API boundary tests | PASS |
| Successful recovery, reset, next ordinary request | release HTTP recovery test | PASS |
| Failed recovery, new cooldown, next request skips | release HTTP recovery test | PASS |
| 20 concurrent requests, one probe, maximum active = 1 | release concurrent ASGI HTTP test; V03-005 concurrency tests | PASS |
| Busy probe excluded immediately, no score/rank | release concurrent HTTP test; health explanation suite | PASS |
| Lease release on success, every normalized failure, unexpected exception, cancellation | test_half_open_probes | PASS |
| Metrics/health persistence failure independence and lease release | release fault-injection tests; health update suite | PASS |
| Authentication failure recorded but not counted | provider health update suite; normalized outcome and lease tests | PASS |
| Provider + model isolation | routing, SQLite and independent lease-key tests | PASS |
| Read failure: inference fail-open, sanitized warning/evidence | health-aware routing and explanation tests | PASS |
| All OPEN: 503, no provider calls | health-aware routing suite | PASS |
| Explanation, selected versus served, explicit model compatibility | health explanations and V0.2 E2E suites | PASS |

Excluded candidates intentionally omit rank and final_score; they do not invent numeric values.
Health admission evidence describes the decision time, not the resulting post-call health state.

## API and database gates

- Service `/health` reports 0.3.0; existing chat/metrics API regression tests pass.
- Provider Health GET covers configured missing records, disabled providers, all effective states,
  probe visibility, stable sorting, UTC/null timestamps, sanitized 503 and no write/lease side effects.
- Fresh schema 3; v1 -> v2 -> v3 and v2 -> v3 migrations preserve attempts, pricing and decisions.
- Health state survives reopen; WAL, UTC, enum round-trip and repeat migrations are tested.
- Old V0.2 explanation JSON without health/excluded_candidates is readable; no schema v4.

## Dashboard and build gates

- Existing metrics and new Provider Health share manual Refresh through Promise.allSettled.
- Static review confirms Healthy/Open/Recovering, consecutive failures, absolute UTC cooldown,
  probe readiness/progress, last success/failure, and null em dashes.
- Loading/empty/error/unavailable handling is separate per section. No placeholder metrics/health,
  polling, or write controls were added. Existing V0.2 metrics cards and lists remain intact.
- npm ci, ESLint, TypeScript and Next.js production build pass; npm audit reports 0 vulnerabilities.
- CI already runs pytest, Ruff, npm ci, lint, typecheck and build; no deployment or CI change needed.
- Browser interaction is not claimed by the static/build checks; remote CI passed after push.
- Production Dashboard HTTP smoke check returned 200 with V0.3.0, Provider Health and existing
  metrics/routing/failure sections present; the temporary local server was stopped afterward.

## Security and operational limits

- Tracked and proposed files scanned for credential signatures, private keys, sensitive filenames,
  local absolute paths and auth/prompt/completion terms. No real credentials or database files found.
- Provider Authorization construction uses configured environment credentials; fake test secrets
  exercise sanitization. Synthetic prompts/completions exist only as test/API examples, not records.
- Metrics/health schemas and read APIs contain no prompt, completion or credential fields.
- .env.example keys remain empty; threshold=3, cooldown=60 and database path match Settings/README.
- README documents single-process leases, no active checking/control APIs, rollback, and limits.
- Existing Starlette/httpx and AnyIO deprecation warnings remain non-blocking. npm reports the
  existing ESLint support warning and an unapproved unrs-resolver install script; all build checks
  pass without approving scripts or upgrading dependencies.

## Publication completed

The separately authorized release workflow pushed main, verified CI on the exact commit, pushed
annotated tag v0.3.0 and published the non-draft, non-prerelease
[GitHub Release](https://github.com/WCRR-ART/ModelPilot/releases/tag/v0.3.0).
[Main CI](https://github.com/WCRR-ART/ModelPilot/actions/runs/35305109741) and
[tag CI](https://github.com/WCRR-ART/ModelPilot/actions/runs/35305170289) passed.
Publication did not change source code or the v0.1.0/v0.2.0 tags.
