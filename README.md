# ModelPilot

Open-source intelligent LLM gateway with automatic model routing, ordered fallback, cost estimates,
and performance-aware provider selection.

[中文文档](README_CN.md)

## What is ModelPilot?

ModelPilot gives applications one OpenAI-compatible chat-completions endpoint while keeping
provider selection inside the gateway. It ranks configured models deterministically, attempts the
next ranked provider after a failure, and records local operational evidence so later decisions can
use measured latency, reliability, and estimated cost.

## V0.2 features

- FastAPI gateway with `GET /health` and OpenAI-compatible `POST /v1/chat/completions`
- OpenAI, Gemini, and DeepSeek adapters
- `model: "auto"` routing and deterministic ordered fallback
- Real provider latency, success/failure outcomes, and nullable token usage
- Decimal cost estimates from configured pricing metadata and provider-reported token usage
- SQLite persistence for attempts, pricing metadata, and structured routing decisions
- Confidence-blended latency, reliability, and cost signals
- Static quality signal; no benchmark-derived quality claims
- Structured route explanations that distinguish the selected route from the provider that served it
- Bounded, read-only Metrics API
- Next.js Dashboard backed by real local metrics
- Bounded in-memory request logs at `GET /v1/logs`

## Tech stack

- Python, FastAPI, Pydantic, httpx, and standard-library SQLite
- Next.js, React, and TypeScript
- pytest, Ruff, ESLint, and GitHub Actions

## Architecture

```text
Client
  -> POST /v1/chat/completions
  -> deterministic confidence-blended Router
  -> OpenAI | Gemini | DeepSeek
  -> ProviderOutcome
  -> AttemptRecord + estimated cost
  -> SQLite metrics + RoutingDecision
  -> OpenAI-compatible response + modelpilot explanation

SQLite metrics
  -> read-only Metrics API
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
npm run dev
```

The API and Dashboard default to `http://localhost:8000` and `http://localhost:3000`. Set
`NEXT_PUBLIC_MODELPILOT_API_URL` when the API runs elsewhere.

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

The Router filters providers without API keys, reads one frozen snapshot per provider/model, applies
the normalized request preferences, and ranks candidates once. Fallback follows that fixed order;
the request is not re-ranked between attempts.

- **Quality:** always the configured static baseline in V0.2.
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
      "routing_version": "v0.2",
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
routing explanation.

## Metrics and storage

Completed attempts are stored in SQLite at `./data/modelpilot.db` by default. Aggregates use at most
the newest **100 attempts per provider/model within the previous 7 days**. This is a query window,
not automatic database retention; older durable records are not deleted by V0.2.

Token counts remain `null` when the provider omits or returns invalid usage. Failures are recorded with
a bounded, sanitized error category. The schema version is 2 and upgrades a version-1 database by
adding routing-decision storage while preserving attempts and pricing.

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

The Dashboard reads the health and four metrics endpoints in parallel. It displays requests, attempts,
success rate, average latency, estimated cost, provider metrics, routing decisions, and recent failures.
Loading, empty, error, and unavailable states are independent, and a manual Refresh action does not
enable polling. Estimated costs are labelled as estimates; selected and served providers are separate.

## Privacy

The metrics database does **not** persist prompts, completions, API keys, authorization headers, or raw
provider error bodies. Request IDs, provider/model names, timestamps, latency, bounded error categories,
nullable usage, cost estimates, and routing metadata are persisted. Provider credentials remain in the
process environment.

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
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Backend URL used by the Dashboard | `http://localhost:8000` |

The names and defaults above match `.env.example`; its API-key values are intentionally empty.

V03-003 records health after each normalized provider outcome using its UTC completion time.
Invalid circuit configuration fails during settings loading. Health and attempt persistence
fail independently; neither changes a successful provider response. Routing does not yet
filter OPEN circuits. Outcomes rejected by the existing OPEN/cooldown or timestamp contract
leave health unchanged and produce a warning; attempt metrics are still recorded.

## V0.2 limitations

V0.2 intentionally has no authentication, multi-user or multi-tenant model, streaming, billing system,
benchmark engine, ML/AI router, Redis, PostgreSQL, distributed deployment, circuit breaker, or real-time
pricing synchronization. SQLite is intended for one local ModelPilot instance. Provider health is
represented by completed local attempts rather than distributed active probes.

## Storage rollback

Back up the SQLite file before changing versions. V0.2 migrates schema v1 to v2 on open and rejects
unknown newer schemas; it does not provide a down-migration. Rolling the application back to v0.1.0
does not require deleting the database, but v0.1.0 will not use V0.2 metrics or routing decisions.

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
