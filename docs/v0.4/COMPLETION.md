# V0.4 Local Release Preparation

Date: 2026-09-21. Release target: **v0.4.0, unpublished**. Published stable release: v0.3.0.
This records local evidence, not a GitHub release or remote CI result.

## Current release gates

| Gate | Current evidence |
| --- | --- |
| V04-008 browser interaction and desktop/narrow layout | PASS — 14 real browser scenarios; see DASHBOARD.md |
| V0.4 backend end-to-end and compatibility | PASS — 1003 tests, including new release-hardening and harness tests |
| Frontend helper/static rendering | PASS — 50 tests; not a substitute for browser testing |
| Required lint/typecheck/build/install/audit | PASS, including acceptance-script Ruff |
| Final version update and related regression | PASS — 0.4.0 metadata, live health and repeated full checks |
| Final sensitive-data, artifacts and Git review | PASS — source-only changes; no credentials, DBs or build/evidence artifacts |
| Local release ready | YES — all required local gates passed; not published |
| Remote CI for this preparation commit | PENDING — not pushed |
| Real cloud Provider calls | NOT_RUN — all acceptance uses fake providers |

The preceding V04-008 baseline was PARTIAL: 990 backend tests, 50 frontend tests and successful
static checks did not establish browser acceptance. It is historical evidence, not this task's final
result. No missing browser gate is waived because of an environment failure.

## Commands and observed results

Use the existing backend environment and installed frontend dependencies. From the repository:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff check ../scripts --config pyproject.toml
Set-Location ../frontend
npm ci
npm test
npm run lint
npm run typecheck
npm run build
npm audit
```

Observed in this task, with the full tests and required code checks repeated after the gated version change:

- Backend pytest: **1003 passed**, with two existing Starlette/httpx and AnyIO deprecation warnings.
- New release coverage: 11 cases in `test_v04_release_hardening.py` and 2 cases in
  `test_dashboard_acceptance_harness.py`; the preceding 990 tests continue to pass.
- Backend Ruff and acceptance-script Ruff: PASS.
- Frontend `npm test`: **50 passed**, helper and React static-render tests.
- Frontend `npm ci`, lint, typecheck and production build: PASS.
- `npm audit`: **0 vulnerabilities** after an approved retry of a transient endpoint failure;
  not a perpetual security guarantee.
- Browser suite: **14 scenarios passed**, Chrome 153.0.8010.48, sandbox enabled, desktop 1440×1000
  and narrow 390×844. Exact command, screenshots, trace and network/console evidence are in
  [DASHBOARD.md](DASHBOARD.md) and the ignored local artifact directory.

The gated update changed backend/package metadata, the frontend version label and health fixture to
0.4.0. The exact version assertions in `test_api.py` and `test_v03_end_to_end.py` were updated, retaining
all cross-file consistency assertions. The first post-update run exposed the latter's old 0.3.0 literal;
after correcting it, the full suite passed **1003 tests in 31.87 seconds**. No test was skipped or removed.
Frontend 50 tests, lint, typecheck and production build passed again. Generated `next-env.d.ts` path
changes were restored and typecheck repeated. The fresh 0.4.0 isolated services passed all 14 browser
scenarios again; actual `GET /health` returned `{"status":"ok","version":"0.4.0"}`.
Ruff's explicit first-party module setting keeps script import checks consistent across working directories.

## End-to-end evidence

`backend/tests/test_v04_release_hardening.py` connects the actual suite loader, CLI entry point,
runner, fake provider, evaluator, SQLite store, HTTP inspection, resolver, production Router,
GatewayService and persisted explanation. Network sends are forbidden during these tests.

| Scenario | Evidence |
| --- | --- |
| Explicit CLI run and save | Required target, ordered messages/cases, saved summary and reopened SQLite records agree |
| Store → list/detail API | Run ID, suite fingerprint, ordered case indexes and serialized results match |
| Wrong answer vs execution failure | Completed score 0 remains evidence; timeout/rate_limit have evaluation null |
| Authentication fail-fast | Partial run saved, exit 3, no invented subsequent cases |
| Latest-attempt semantics | New failure removes older successful evidence; unattempted cases can retain their own historical evidence |
| Independent sample count | Repeated runs do not inflate unique-case count or confidence |
| Confidence formula | Existing `clamp((n-5)/45,0,1) × weighted_evaluation_coverage × execution_completeness` is unchanged |
| Full-confidence quality routing | Synthetic 50 independent cases alter quality input and quality-only preferred ordering |
| Smoke routing | Synthetic 3-case suite has confidence 0 and retains static quality |
| Health precedence | High-quality OPEN candidate excluded; HALF_OPEN GET does not claim a probe or mutate state |
| Production isolation | CLI/benchmark GET leave attempts/pricing/health/decisions unchanged; later ordinary fake chats create only their own records |
| Explanation provenance | Stored selected candidate retains static/measured values, confidence and source Run IDs |
| Static/configuration fallback | Existing V04-006 regression covers missing/zero/null evidence, runtime failures, and explicit invalid-suite startup errors |

Existing suite/category identity, independent category confidence, explicit-model behavior, core
OpenAI-compatible response, metrics/health API, CLI exit/persistence failures and read-only contracts
remain covered by the full regression. No quality formula, production routing algorithm or cloud
Provider behavior is redefined by release preparation.

The separately executable `scripts/dashboard_acceptance.py` uses the real app/store/runner and a
60-case original synthetic fixture. Its fake providers contain no HTTP client. It saves three
benchmark runs, then performs one **ordinary fake chat** to populate production Dashboard evidence.
That chat's operational records are expected and are not benchmark contamination. Two harness tests
verify the fake-run results, quality and exactly one ordinary chat's explanation/log.
The separate real-browser frontend → local backend → temporary SQLite path also passed, using
unmocked API requests in a fresh context. This is recorded in the browser suite, not inferred from
the two backend harness tests. All data remain synthetic; no real cloud Provider was called.

## Database compatibility and verified backup/restore

SQLite schema stays **4**. Existing temporary-database tests cover fresh initialization, v1/v2/v3
migrations, transactional rollback on migration failure, preservation of attempts/pricing/decisions/
health, atomic benchmark saving, duplicate-ID rejection, legacy V0.2/V0.3 explanations and reopen.
No user database was used to test an upgrade or rollback.

`scripts/backup-sqlite.py SOURCE_DATABASE NEW_DESTINATION` opens the source read-only, uses SQLite's
backup API (including committed WAL records), runs `PRAGMA integrity_check`, and prints the copied
schema version. It creates only a new destination; existing destinations or WAL/SHM sidecars are
rejected. It does not create parent directories, migrate, downgrade or overwrite the source.

Release-hardening tests execute the actual script as a subprocess and verify:

- Committed, uncheckpointed WAL records survive backup and restore; source contents stay unchanged.
- The reopened copy preserves both wrong-answer scores and execution-failure null evaluations.
- A schema-3 backup remains schema 3 after the original database is upgraded to schema 4; restoring
  that backup to a new path preserves the pre-upgrade data, without benchmark tables.
- Existing destination, same path, sidecar collision, missing source and invalid source are rejected
  without overwriting user data.

### Before upgrading

Stop **all** application processes and CLI writers that use the database. Identify the actual
`MODELPILOT_METRICS_DB` path; the example below assumes the normal `backend/data/modelpilot.db` path
and runs from the repository root. Substitute an explicit correct path if your configuration differs.
Keep backups outside source control and select a new destination name for each backup.

```powershell
New-Item -ItemType Directory -Path .\backups -Force
& .\backend\.venv\Scripts\python.exe .\scripts\backup-sqlite.py .\backend\data\modelpilot.db .\backups\modelpilot-pre-v04.sqlite3
```

Proceed only when exit code is 0 and output reports `integrity_check=ok` and the expected pre-upgrade
schema (for released v0.3.0, schema=3). The script refuses an existing backup; do not overwrite a
known-good backup to make the command succeed. Preserve the original backup offline as appropriate.
Simply copying a live `.db` file without its outstanding WAL is not this verified procedure.

### Roll back using a pre-upgrade backup

Stop writers again. Preserve the upgraded database; do not replace it in place. Create a separate
restored database from the verified **pre-upgrade** backup:

```powershell
& .\backend\.venv\Scripts\python.exe .\scripts\backup-sqlite.py .\backups\modelpilot-pre-v04.sqlite3 .\backend\data\modelpilot-rollback.sqlite3
$env:MODELPILOT_METRICS_DB = (Resolve-Path .\backend\data\modelpilot-rollback.sqlite3).Path
```

Check the exit code/integrity/schema output again, then start the compatible previous program from
its own environment against this explicitly selected restored path. Do not initialize the V0.4 store
on a schema-3 rollback copy before starting v0.3.0: doing so would upgrade it again. Clear or adjust
the process-local database setting when returning to the intended deployment.

Schema 4 has **no automatic down-migration**. Released v0.3.0 rejects schema 4. A backup taken after
upgrade still has schema 4 and cannot substitute for a compatible pre-upgrade backup. Never edit the
schema marker to force an older program to open an incompatible database.

## Bounded synthetic performance observation

One regression seeds **100 runs × 50 independent cases = 5,000 stored case results** for one exact
suite/target identity, then performs three resolver reads. Latest evidence remains 50 independent
evaluated cases, not 5,000. Recorded local run: Python 3.12.10, SQLite 3.49.1, Windows; elapsed
resolver reads **0.1958 s / 0.1970 s / 0.1337 s**. These are synthetic, machine/load-dependent
observations, not a throughput guarantee, threshold, cloud benchmark or improvement claim.

All matching history is read and revalidated on each lookup. No cache, TTL, retention pruning or
background infrastructure was added. Large histories can increase inference latency. Source Run
IDs and times remain the way to inspect stale evidence; snapshot `generated_at` is calculation time.

## CI and release boundary

CI configuration includes backend pytest/Ruff and Ruff for acceptance scripts, plus frontend
`npm ci`, `npm test`, lint, typecheck and production build. Required failures are not hidden with
`continue-on-error`. Browser acceptance is a repeatable **local release gate**; the Windows isolated
harness/browser suite is not currently wired into GitHub-hosted CI, so no browser CI claim is made.
Current preparation changes are unpushed; remote CI remains PENDING regardless of earlier release CI.

No push, tag creation, old-tag modification or GitHub Release belongs to this task. Version metadata
became 0.4.0 only after the prerequisite local gates passed and was revalidated afterward.
Draft release notes remain [docs/releases/v0.4.0.md](../releases/v0.4.0.md).

## Final audit record

- Browser and layout: PASS; see [DASHBOARD.md](DASHBOARD.md).
- Post-gate version and `/health`: PASS, 0.4.0; published stable version remains v0.3.0.
- Final checks: 1003 backend tests, 50 frontend tests, 14 browser scenarios; Ruff/lint/typecheck/build PASS.
- Source/secret/artifact audit: no populated .env, credentials, SQLite/WAL/SHM, screenshots, traces or build output included.
- Product changes are version labels only; Router, evaluators, aggregation, providers, health and schema are unchanged.
- Commit: `chore(release): prepare ModelPilot v0.4.0`; exact hash and observed Git status are reported in the task handoff.
- `LOCAL_RELEASE_READY`: **YES**. Remote CI and publication require a separately authorized next step.

Non-blocking environment observations: existing Python dependency deprecations; npm's ESLint support/
existing native-install-script notices; a transient audit endpoint failure recovered on retry. The
desktop sandbox helper was unavailable, so normal approved local commands and the installed Chrome
were used without disabling the browser sandbox. Forced tool cancellation can bypass launcher cleanup;
use its tested timed or interactive shutdown and follow the cleanup guidance in DASHBOARD.md.
