# ModelPilot

Open-source intelligent LLM gateway with automatic model routing, ordered fallback, cost estimates,
and performance-aware provider selection.

[中文文档](README_CN.md)

## What is ModelPilot?

ModelPilot gives applications one OpenAI-compatible chat-completions endpoint while keeping
provider selection inside the gateway. It ranks configured models deterministically, attempts the
next ranked provider after a failure, and records local operational evidence so later decisions can
use measured latency, reliability, and estimated cost.

## V0.3 features

Source version: **0.3.0 (release preparation)**. The latest published stable release remains
v0.2.0 until the v0.3.0 tag and GitHub Release are published.

### Gateway

- FastAPI gateway with `GET /health` and OpenAI-compatible `POST /v1/chat/completions`
- OpenAI, Gemini, and DeepSeek adapters
- `model: "auto"` routing and deterministic ordered fallback

### Routing
- Real provider latency, success/failure outcomes, and nullable token usage
- Decimal cost estimates from configured pricing metadata and provider-reported token usage
- SQLite persistence for attempts, pricing metadata, and structured routing decisions
- Confidence-blended latency, reliability, and cost signals
- Static quality signal; no benchmark-derived quality claims
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
python -m uvicorn modelpilot.main:app --reload --env-file ../.env
```

In another terminal:

```bash
cd frontend
npm ci
export NEXT_PUBLIC_MODELPILOT_API_URL=http://localhost:8000
# PowerShell: $env:NEXT_PUBLIC_MODELPILOT_API_URL="http://localhost:8000"
npm run dev
```

The API and Dashboard default to `http://localhost:8000` and `http://localhost:3000`. Set
`NEXT_PUBLIC_MODELPILOT_API_URL` when the API runs elsewhere. Next.js does not load the repository-root
`.env`: export this public URL as above or put only that setting in `frontend/.env.local`.
Without it the Dashboard uses same-origin API paths; configure a reverse proxy for that deployment.
The public URL is captured at frontend build time and must not contain credentials.

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

- **Quality:** always the configured static baseline.
- **Latency:** blended from the configured baseline and measured p50 latency when available.
- **Reliability:** blended from the configured baseline and smoothed measured success rate.
- **Cost:** blended from the configured baseline and p50 estimated request cost when priced samples exist.

Missing dimensions keep their static baseline. This is deterministic operational routing, not an ML
router and not a provider benchmark.

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
a bounded, sanitized error category. Schema version 3 upgrades v1 through v2 to v3, or v2 to v3,
adding provider health while preserving attempts, pricing and routing decisions. Old V0.2 explanation
JSON without health fields remains readable. SQLite uses WAL; the directory is created automatically.
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

## Privacy

The metrics and health database does **not** persist prompts, completions, API keys, authorization headers, or raw
provider error bodies. Request IDs, provider/model names, timestamps, latency, bounded error categories,
nullable usage, cost estimates, and routing metadata are persisted. Provider credentials remain in the
process environment. Route and health explanations do not expose credentials or secrets.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Comma-separated allowed Dashboard origins | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | Maximum in-memory request-log records | `500` |
| `MODELPILOT_METRICS_DB` | SQLite metrics database path | `./data/modelpilot.db` |
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

## V0.3 limitations

V0.3 has no distributed circuit breaker, multi-process probe coordination, manual circuit controls,
circuit event history, background health checker, authentication, multi-user/multi-tenant model,
billing/payment, streaming, benchmark engine, ML/AI Router, Redis, PostgreSQL, additional provider
expansion, or live pricing synchronization. SQLite is intended for one local ModelPilot instance.

## Storage rollback

Stop the application and back up SQLite (including any outstanding WAL data) before upgrading.
V0.3 migrates schema v1/v2 to v3 and rejects unknown newer schemas; there is no down-migration.
To roll back to V0.2, restore the pre-upgrade database backup; V0.2 cannot open a schema-v3 database.

## Validation

```bash
cd backend
python -m pytest --tb=short
python -m ruff check .

cd ../frontend
npm ci
npm run lint
npm run typecheck
npm run build
npm audit
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and pull-request guidance.

## License

MIT
