# ModelPilot

Open-source intelligent LLM gateway with automatic model routing, fallback, cost optimization, and performance-aware selection.

> V0.1 is an intentionally small, runnable foundation. It provides deterministic in-process routing and an in-memory request log; it does not include persistence, authentication, billing, streaming, or distributed health checks.

[中文文档](README_CN.md)

## Why ModelPilot

Applications often hard-code one model and inherit its outages, latency, and price profile. ModelPilot provides one OpenAI-compatible entry point that can rank configured models and try the next provider when an automatic route fails.

## V0.1 features

- FastAPI service with `GET /health`
- OpenAI-compatible `POST /v1/chat/completions`
- OpenAI, Gemini, and DeepSeek provider adapters
- `model: "auto"` model selection
- Weighted quality, cost, latency, and reliability scoring
- Ordered provider fallback
- Structured, bounded in-memory request logs at `GET /v1/logs`
- Responsive Next.js dashboard

## Tech stack

- Python, FastAPI, Pydantic, and httpx
- Next.js, React, and TypeScript
- pytest, Ruff, and ESLint
- GitHub Actions

## Architecture

```text
Client
  -> FastAPI /v1/chat/completions
  -> deterministic weighted router
  -> OpenAI | Gemini | DeepSeek adapter
  -> provider response in OpenAI-compatible shape
  -> bounded in-memory request log
```

Provider-specific HTTP translation stays under `backend/src/modelpilot/providers/`. The router uses V0.1 baseline scores; it does not collect live benchmarks.

## Quick start

### Backend

```bash
cp .env.example .env
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m uvicorn modelpilot.main:app --reload --env-file ../.env
```

The API is available at `http://localhost:8000`. A provider is enabled only when its API key is present in the environment.

On the V0.2 development branch, completed provider attempts are stored in SQLite at
`./data/modelpilot.db` by default. Set `MODELPILOT_METRICS_DB` to use another path; the parent
directory is created automatically. Prompts, completions, and credentials are not stored.
When matching configured pricing and real input/output token usage are available, ModelPilot stores
a Decimal cost estimate. It is not provider billing; without either input, cost remains unavailable.

### Dashboard

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:3000`. Set `NEXT_PUBLIC_MODELPILOT_API_URL` if the API runs elsewhere.

## API example

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

Optional routing priorities can be supplied in `modelpilot.preferences`; omitted weights default to an even, balanced profile.

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "Summarize this."}],
  "modelpilot": {
    "preferences": {"quality": 0.4, "cost": 0.3, "latency": 0.2, "reliability": 0.1}
  }
}
```

### What `model: "auto"` means

For `auto`, ModelPilot filters out providers without API keys, calculates a weighted score from quality, cost, latency, and reliability, then attempts candidates from highest to lowest score. If an attempt fails, the next ranked provider is tried. The V0.1 scores are static normalized estimates, not benchmark claims.

### ModelPilot response extension

On the V0.2 development branch, successful automatic responses keep the standard OpenAI-compatible
`id`, `object`, `model`, `choices`, and `usage` fields and add routing evidence under the
ModelPilot-specific top-level `modelpilot` extension:

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
      "candidates": [
        {
          "provider": "deepseek",
          "model": "deepseek-chat",
          "rank": 1,
          "selected": true,
          "final_score": 0.88,
          "sources": {
            "quality": "configured",
            "latency": "blended",
            "reliability": "blended",
            "cost": "static_unavailable"
          }
        }
      ]
    }
  }
}
```

The selected candidate is the Router's first choice; `served_by` identifies the provider that
actually returned the response after any fallback. Scores are internal routing inputs derived from
configured baselines and metrics measured by this local ModelPilot installation. They are not live
provider benchmarks. Explicit-model requests include `served_by` but no automatic routing explanation.

### Read-only metrics API

The V0.2 development branch exposes persisted local metrics through four read-only endpoints:

- `GET /v1/metrics/summary?hours=24` — distinct requests, attempts, outcomes, latency,
  estimated cost, providers, models, and routing decisions, limited to 1–168 hours
- `GET /v1/metrics/providers` — current seven-day/100-attempt provider-model snapshots;
  optional exact `provider` and `model` filters are supported
- `GET /v1/metrics/routing-decisions?limit=20` — persisted structured routing evidence
- `GET /v1/metrics/failures?limit=20` — recent standardized failure categories

Decision and failure limits are capped at 100. Decimal cost values are returned as exact JSON fixed-point
strings and remain `null` when unavailable. Costs are estimates based on configured pricing and real
provider-reported usage, not billing data. These endpoints never return prompts, completions,
credentials, authorization headers, or raw provider error messages. No metrics or pricing write endpoint
is exposed.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Comma-separated allowed dashboard origins | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | Maximum in-memory request-log records | `500` |
| `MODELPILOT_METRICS_DB` | SQLite file for completed provider attempts | `./data/modelpilot.db` |
| `OPENAI_API_KEY` | Enables the OpenAI adapter | unset |
| `OPENAI_BASE_URL` | OpenAI-compatible base URL | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | OpenAI candidate used by `auto` | `gpt-4o-mini` |
| `GEMINI_API_KEY` | Enables the Gemini adapter | unset |
| `GEMINI_BASE_URL` | Gemini API base URL | Google Generative Language API |
| `GEMINI_MODEL` | Gemini candidate used by `auto` | `gemini-2.0-flash` |
| `DEEPSEEK_API_KEY` | Enables the DeepSeek adapter | unset |
| `DEEPSEEK_BASE_URL` | DeepSeek API base URL | `https://api.deepseek.com/v1` |
| `DEEPSEEK_MODEL` | DeepSeek candidate used by `auto` | `deepseek-chat` |
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Backend URL used by the dashboard | `http://localhost:8000` |

Never commit a populated `.env` file.

## V0.1 limitations

- Non-streaming chat completions only
- Static routing scores and process-local request logs
- No database, authentication, billing, or distributed health checks
- Explicit model requests are served only by the adapter configured for that model

## Roadmap

V0.1 establishes the gateway contract and provider boundary. Possible later work includes measured routing signals, durable logs, access control, and streaming, but none of those capabilities are implemented or committed to a release yet.

## Development

```bash
cd backend
python -m pytest
python -m ruff check .

cd ../frontend
npm run lint
npm run typecheck
npm run build
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and pull-request guidance.

## License

MIT
