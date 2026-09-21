# ModelPilot

Open-source intelligent LLM gateway with automatic model routing, ordered fallback, cost estimates,
and performance-aware provider selection.

[中文文档](README_CN.md)

## What is ModelPilot?

ModelPilot gives applications one OpenAI-compatible chat-completions endpoint while keeping
provider selection inside the gateway. It ranks configured models deterministically, attempts the
next ranked provider after a failure, and records local operational evidence so later decisions can
use measured latency, reliability, and estimated cost.

## Version status and features

Current stable version: **0.3.0**, officially published as
[ModelPilot v0.3.0](https://github.com/WCRR-ART/ModelPilot/releases/tag/v0.3.0).

This working branch targets **v0.4.0 — Benchmark-Driven Quality Routing**. It is not yet published.
The capabilities below describe this branch, not the older stable release. Local acceptance results
and remaining gates are tracked in [V0.4 completion](docs/v0.4/COMPLETION.md);
[v0.4.0 release notes](docs/releases/v0.4.0.md) remain a draft.

### Gateway

- FastAPI gateway with `GET /health` and OpenAI-compatible `POST /v1/chat/completions`
- OpenAI, Gemini, and DeepSeek adapters
- `model: "auto"` routing and deterministic ordered fallback

### Routing
- Real provider latency, success/failure outcomes, and nullable token usage
- Decimal cost estimates from configured pricing metadata and provider-reported token usage
- SQLite persistence for attempts, pricing metadata, and structured routing decisions
- Confidence-blended latency, reliability, and cost signals
- Optional confidence-blended benchmark quality, with static quality as the default/fallback
- Structured route explanations that distinguish the selected route from the provider that served it

### Observability
- Bounded, read-only Metrics API
- Next.js Dashboard backed by real local metrics
- Bounded in-memory request logs at `GET /v1/logs`

### Provider Health

- Persisted provider/model circuit state: CLOSED / OPEN / HALF_OPEN
- Configurable failure threshold and cooldown, automatic OPEN isolation before auto scoring
- Single-process, single-flight recovery probes and automatic recovery on success
- Structured health-aware route explanations with unscored excluded candidates
- Read-only Provider Health API and Dashboard circuit status

### Benchmarks (V0.4)

- Explicit local JSON suites and five versioned deterministic evaluators; no LLM judge
- Bounded, sequential CLI execution against one explicitly selected provider/model
- Atomic SQLite run/case persistence, read-only run and quality APIs, and a read-only Dashboard
- Unique-case quality evidence, suite identity checks and traceable routing explanations

## Tech stack

- Python, FastAPI, Pydantic, httpx, and standard-library SQLite
- Next.js, React, and TypeScript
- pytest, Ruff, ESLint, and GitHub Actions

## Architecture

```text
Client
  -> POST /v1/chat/completions
  -> circuit eligibility -> deterministic confidence-blended Router
  -> OpenAI | Gemini | DeepSeek
  -> ProviderOutcome
  -> AttemptRecord + estimated cost
  -> SQLite metrics + RoutingDecision + ProviderHealth
  -> OpenAI-compatible response + modelpilot explanation

SQLite metrics + health / process-local probe status
  -> read-only Metrics and Provider Health APIs
  -> Next.js Dashboard
```

Provider-specific HTTP translation stays under `backend/src/modelpilot/providers/`. A metrics write
failure is logged but cannot turn a successful provider response into a failed inference request.

## Quick start

Copy the environment template and add keys only for providers you want to enable. Never commit the
populated `.env` file.

```bash
cp .env.example .env
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m uvicorn modelpilot.main:app --host 127.0.0.1 --reload --env-file ../.env
```

In another terminal:

```bash
cd frontend
npm ci
export NEXT_PUBLIC_MODELPILOT_API_URL=http://localhost:8000
# PowerShell: $env:NEXT_PUBLIC_MODELPILOT_API_URL="http://localhost:8000"
npm run dev -- --hostname localhost
```

The API and Dashboard default to `http://localhost:8000` and `http://localhost:3000`. Set
`NEXT_PUBLIC_MODELPILOT_API_URL` when the API runs elsewhere. Next.js does not load the repository-root
`.env`: export this public URL as above or put only that setting in `frontend/.env.local`.
Without it the Dashboard uses same-origin API paths; configure a reverse proxy for that deployment.
The public URL is captured at frontend build time and must not contain credentials.
There is no authentication. Keep these services on loopback; do not expose the API or Dashboard
publicly. Use a single backend process for the recovery-probe single-flight guarantee.

## API example

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

Routing priorities are optional. Omitted values use the balanced default.

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "Summarize this."}],
  "modelpilot": {
    "preferences": {"quality": 0.4, "cost": 0.3, "latency": 0.2, "reliability": 0.1}
  }
}
```

## How routing works

```text
Cold start
  -> static baseline
  -> completed attempts collected
  -> recent measured metrics
  -> confidence blending
  -> deterministic dynamic ranking
```

The auto Router filters providers without API keys and OPEN circuits, reads one snapshot per provider/model, applies
the normalized request preferences, and ranks candidates once. Fallback follows that fixed order;
the request is not re-ranked between attempts. Eligibility is checked again immediately before a call,
and a HALF_OPEN candidate must acquire the process-local probe lease.

- **Quality:** static by default; optionally blended with compatible benchmark evidence as below.
- **Latency:** blended from the configured baseline and measured p50 latency when available.
- **Reliability:** blended from the configured baseline and smoothed measured success rate.
- **Cost:** blended from the configured baseline and p50 estimated request cost when priced samples exist.

Missing dimensions keep their static baseline. This is deterministic routing, not an ML router or a
claim that one model is universally better than another.

### Routing confidence

Confidence is calculated independently for measured attempt signals and priced cost samples:

- fewer than 5 samples: static signal only
- 5–49 samples: measured data is introduced gradually
- 50 or more samples: measured signal has full confidence

This ramp keeps a small number of requests or a single failure from immediately dominating routing.

### Route explanation

Successful automatic responses preserve the core OpenAI-compatible fields and add a `modelpilot`
extension. `selected_provider` is the Router's first choice; `served_provider` is the provider that
actually returned the response after fallback.

```json
{
  "modelpilot": {
    "request_id": "req_...",
    "served_by": {"provider": "gemini", "model": "gemini-2.0-flash"},
    "routing": {
      "routing_version": "v0.3",
      "selected_provider": "deepseek",
      "selected_model": "deepseek-chat",
      "served_provider": "gemini",
      "served_model": "gemini-2.0-flash",
      "selected": {
        "final_score": 0.88,
        "measured_sample_count": 12,
        "measured_confidence": 0.155556,
        "sources": {
          "quality": "configured",
          "latency": "blended",
          "reliability": "blended",
          "cost": "static_unavailable"
        }
      }
    }
  }
}
```

Explicit-model requests remain deterministic and include `served_by`, but do not create an automatic
routing explanation. They bypass automatic health filtering and do not silently switch providers.
The example above is an abbreviated structure, not measured benchmark data. V0.3 also includes
`health` evidence on candidates and `excluded_candidates`; excluded entries have no rank or score.
With a configured quality suite, routing version is `v0.4`; candidate explanations additionally expose
static/benchmark quality, confidence, suite identity, source run IDs and latest-run completeness.
Explicit-model requests do not look up benchmark quality.

## Circuit breaker

```text
CLOSED -> counted failures reach threshold -> OPEN
OPEN -> cooldown expires -> HALF_OPEN (eligible for a recovery probe)
HALF_OPEN -> probe success -> CLOSED
HALF_OPEN -> counted probe failure -> OPEN (new cooldown)
```

Defaults are **3 consecutive counted failures** and **60 seconds cooldown**, both configurable.
Counted errors: `timeout`, `connection_error`, `rate_limit`, `provider_error`, `invalid_response`,
and `unknown_error`. `authentication_error` remains observable in attempts but does not count
toward circuit failure or open a circuit. A successful call resets the failure count.

Auto routing skips OPEN circuits and returns the existing 503 unavailable response if none are
eligible. After cooldown, recovery is request-driven: one in-flight probe per provider/model;
other requests skip that candidate and use alternatives. No background checker runs.

**Probe coordination is single-process only.** Multiple workers or instances do not share leases;
this is not a distributed circuit breaker. Use one process for the single-flight guarantee.
SQLite preserves health across restarts; runtime probe leases are not persisted.
Health-store read failures are fail-open for inference with sanitized warnings. Attempt and health
writes fail independently and cannot replace a successful inference response with an error.

## Read-only Provider Health API

`GET /v1/health/providers` reports configured provider/model identity, effective state, eligibility,
reason, consecutive failures, cooldown, last success/failure, and `probe_in_flight`.
It is distinct from service liveness at `GET /health`. Expired OPEN records are shown as effectively
HALF_OPEN without a database write or probe claim. Missing records use CLOSED / health_unknown;
disabled providers are omitted. Timestamps are UTC; missing data remains null. Store failures return
503, not fabricated healthy data. Snapshots do not guarantee admission of the next request.
There are no reset, manual open/close, or manual probe endpoints.

## Metrics and storage

Completed attempts are stored in SQLite at `./data/modelpilot.db` by default. Aggregates use at most
the newest **100 attempts per provider/model within the previous 7 days**. This is a query window,
not automatic database retention; older durable records are not automatically deleted.

Token counts remain `null` when the provider omits or returns invalid usage. Failures are recorded with
a bounded, sanitized error category. This branch uses schema **4**; fresh databases initialize at v4,
and v1/v2/v3 migrate through the existing chain while preserving attempts, pricing, routing decisions
and provider health. Two separate tables store benchmark runs and cases. Old V0.2/V0.3 explanation
JSON remains readable. SQLite uses WAL; the directory is created automatically.
Relative database paths resolve from the backend process working directory.

### Estimated cost

An estimate is produced only when both inputs exist:

```text
configured per-million-token pricing metadata
  + actual provider-reported input/output tokens
  = estimated request cost
```

The default server contains no built-in price catalog and performs no live pricing sync. Pricing is
configuration metadata supplied through the `MetricsStore` contract. Stored estimates are Decimal
values and are not recalculated when pricing metadata changes. They are estimates, not provider bills.

## Read-only Metrics API

- `GET /v1/metrics/summary?hours=24` — distinct requests, attempts, outcomes, latency, estimated cost,
  providers, models, and routing decisions; `hours` is limited to 1–168
- `GET /v1/metrics/providers` — seven-day/100-attempt provider-model snapshots with optional exact
  `provider` and `model` filters
- `GET /v1/metrics/routing-decisions?limit=20` — persisted structured routing evidence
- `GET /v1/metrics/failures?limit=20` — recent sanitized failure categories

Decision and failure limits are capped at 100. Decimal costs are exact JSON strings and unavailable
values remain `null`. No metrics or pricing write endpoint is exposed.

## Dashboard

The Dashboard reads service health, Provider Health, and four metrics endpoints in parallel. It displays requests, attempts,
success rate, average latency, estimated cost, provider metrics, routing decisions, and recent failures.
Loading, empty, error, and unavailable states are independent, and a manual Refresh action does not
enable polling. Estimated costs are labelled as estimates; selected and served providers are separate.
Provider Health shows Healthy / Open / Recovering, consecutive failures, UTC cooldown deadlines,
probe readiness or progress, and last success/failure. No-record providers are explicitly labeled;
null timestamps display an em dash. Refresh reloads both metrics and health, without write controls.

The Benchmark section displays the most recent 20 matching runs, not all history. Apply submits the
provider/model/suite/version filters; details load on selection. A separate explicit provider/model
query shows current configured-suite quality, coverage, execution completeness, heuristic confidence,
category evidence, provenance and latest-run diagnostics. Historical run details are not the same as
the current quality snapshot. Zero is displayed as zero; missing evaluation is unavailable. Refresh
retains applied filters and selections. There are no benchmark execution controls or automatic polling.
See [Dashboard acceptance evidence](docs/v0.4/DASHBOARD.md) for interaction/layout validation.

## Privacy

The metrics and health database does **not** persist prompts, completions, API keys, authorization headers, or raw
provider error bodies. Request IDs, provider/model names, timestamps, latency, bounded error categories,
nullable usage, cost estimates, and routing metadata are persisted. Provider credentials remain in the
process environment. Route and health explanations do not expose credentials or secrets.
Benchmark persistence likewise excludes raw prompts, expected answers, model outputs and credentials;
it stores scores, normalized execution metadata and provenance. Suite files contain their own prompts
and expected answers: treat them as deliberately selected local data, not as a place for secrets.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Comma-separated allowed Dashboard origins | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | Maximum in-memory request-log records | `500` |
| `MODELPILOT_METRICS_DB` | SQLite metrics database path | `./data/modelpilot.db` |
| `MODELPILOT_QUALITY_SUITE_PATH` | Explicit local suite for optional overall quality routing and quality API | unset/blank: disabled |
| `MODELPILOT_CIRCUIT_FAILURE_THRESHOLD` | Consecutive counted failures before OPEN; integer >= 1 | `3` |
| `MODELPILOT_CIRCUIT_COOLDOWN_SECONDS` | Circuit cooldown in seconds; integer >= 0 | `60` |
| `OPENAI_API_KEY` | Enables the OpenAI adapter | unset |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI endpoint and automatic candidate | official URL / `gpt-4o-mini` |
| `GEMINI_API_KEY` | Enables the Gemini adapter | unset |
| `GEMINI_BASE_URL` / `GEMINI_MODEL` | Gemini endpoint and automatic candidate | official URL / `gemini-2.0-flash` |
| `DEEPSEEK_API_KEY` | Enables the DeepSeek adapter | unset |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek endpoint and automatic candidate | official URL / `deepseek-chat` |
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Backend URL used by the Dashboard | template: `http://localhost:8000`; unset: same-origin |

The names and defaults above match `.env.example`; its API-key values are intentionally empty.

Health is recorded after each normalized provider outcome using its UTC completion time.
Invalid circuit configuration fails during settings loading. Health and attempt persistence
fail independently; neither changes a successful provider response. Auto routing filters OPEN
circuits. Outcomes rejected by the existing OPEN/cooldown or timestamp contract
leave health unchanged and produce a warning; attempt metrics are still recorded.

## Benchmark execution and quality

From `backend`, after installing the backend and explicitly supplying the selected provider's
credentials in the process environment:

```bash
python -m modelpilot.benchmarks.cli run --suite ../benchmarks/suites/smoke-v1.json --provider openai --model YOUR_MODEL --max-cases 100 --timeout 30
```

Replace `YOUR_MODEL` deliberately. This command can incur real Provider charges; the test suite uses
fake providers and does not prove cloud-provider compatibility. The CLI does not automatically load
`.env`. Suite, provider and model are required; `auto` is rejected. Each case executes once in order,
without retry or fallback. Authentication failure stops further cases; produced results are saved
before exit code 3. Ordinary provider failures/wrong answers can be saved with exit code 0; invalid
input/configuration exits 2, system/persistence failure exits 4. Limits are not a monetary budget.
Even temperature zero does not guarantee byte-for-byte reproducible Provider output.

HTTP and Dashboard are read-only: `GET /v1/benchmarks/runs`, `/v1/benchmarks/runs/{run_id}` and
`/v1/benchmarks/quality?provider=...&model=...`. Listing has an explicit limit, default 20/max 100.
No configured suite returns 503 `quality_suite_not_configured`; no matching quality evidence returns
404, not a fabricated zero. GET requests do not run benchmarks or update health/probe state.
See [CLI/API details](docs/v0.4/BENCHMARKS.md).

Quality describes performance only under the pinned suite and evaluator rules:

- An evaluated wrong answer is genuine score **0** and stays in the weighted quality mean.
- Execution failure has `evaluation=null`; it reduces completeness/confidence, not quality directly.
- Coverage is observed unique cases / suite cases. Execution completeness is evaluated unique cases /
  suite cases. No evaluated cases means quality `null` and confidence 0.
- Identity includes provider, model, suite ID, version and fingerprint; incompatible histories do not mix.
- Latest attempt per case wins by `(run.finished_at, run_id)`. A newer failure removes the old score.
  Unattempted cases in a partial run may retain older evidence; latest-run diagnostics expose that fact.
- Confidence is `clamp((n-5)/45, 0, 1) × weighted_evaluation_coverage × execution_completeness`, with
  `n` unique evaluated cases. Repeating cases does not create independent samples. Categories use their
  own counts. Confidence is a heuristic blending weight, **not** a statistical confidence interval.
- A perfect three-case smoke suite has quality 1, confidence 0 and cannot establish broad model ability.
  Routing uses overall quality only; categories are evidence, not a task classifier.

Set `MODELPILOT_QUALITY_SUITE_PATH` explicitly to opt in; blank retains static quality. Relative paths
use the backend working directory. The definition loads once at startup; restart after changes.
Invalid explicit paths/definitions fail startup. Runtime evidence/read errors instead log a sanitized
warning and retain static quality. The blend is `static × (1-confidence) + quality × confidence`,
without multiplying coverage again. Health filtering remains first; quality cannot bypass OPEN.

Each quality lookup reads and aggregates **all matching history**, not the run list's recent 20. There
is no cache, TTL or automatic expiry; growing histories increase per-request work. Old evidence can
remain relevant, so inspect source runs, their timestamps and latest-run completeness. `generated_at`
is snapshot computation time, not the time the model answered. No QualitySnapshot is persisted.
See [exact evaluation rules](docs/v0.4/EVALUATION.md).

## Current limitations

This branch has no distributed circuit breaker, multi-process probe coordination, manual circuit controls,
circuit event history, background health checker, authentication, multi-user/multi-tenant model,
billing/payment, streaming, benchmark execution over HTTP/UI, scheduler, LLM judge, task/category
classifier, ML/AI Router, Redis, PostgreSQL, additional provider expansion, or live pricing
synchronization. SQLite is intended for one local ModelPilot instance.

## Storage rollback

Stop all application/CLI writers and make a consistent SQLite backup before first opening an older
database with V0.4. Do not copy just a live `.db` file and omit outstanding WAL data. Schema 4 has no
automatic down-migration; released v0.3.0 rejects it. Returning to an older program requires a
compatible pre-upgrade backup, not changing the version marker. See the verified backup/restore
commands and temporary-database migration evidence in [V0.4 completion](docs/v0.4/COMPLETION.md).

## Validation

```bash
cd backend
python -m pytest --tb=short
python -m ruff check .

cd ../frontend
npm ci
npm test
npm run lint
npm run typecheck
npm run build
npm audit
```

Backend tests use fake providers and temporary databases. Frontend `npm test` covers helpers/static
rendering and is not browser acceptance. Browser interactions/layout, real local frontend/backend/SQLite
connections, exact commands/counts and evidence are recorded separately in
[DASHBOARD.md](docs/v0.4/DASHBOARD.md) and [COMPLETION.md](docs/v0.4/COMPLETION.md).
Actual cloud Provider acceptance is **NOT_RUN**. Synthetic fixture results are not real model quality
benchmarks. Remote CI for the unpushed release-preparation commit remains **pending**, not passed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and pull-request guidance.

## License

MIT
