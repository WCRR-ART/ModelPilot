# AGENTS.md

## Scope

These instructions apply to the entire repository.

## Project boundaries

- Keep changes within the V0.1 scope described in the README.
- Preserve the OpenAI-compatible request and response shape.
- Never commit API keys, access tokens, or populated `.env` files.
- Keep provider-specific HTTP details inside `backend/src/modelpilot/providers/`.
- Keep routing decisions deterministic and covered by tests.
- Do not add persistence, authentication, billing, or streaming without an explicit version decision.

## Validation

Before handing off changes, run:

```bash
cd backend
python -m pytest
python -m ruff check .

cd ../frontend
npm run lint
npm run typecheck
npm run build
```

## Style

- Python: typed, small modules, Ruff-compliant.
- TypeScript: strict mode; avoid `any`.
- Document new environment variables in `.env.example`.
