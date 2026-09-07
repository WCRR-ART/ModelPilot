# Contributing to ModelPilot

Thanks for helping build ModelPilot.

## Development setup

1. Fork and clone the repository.
2. Copy `.env.example` to `.env` and add only the provider keys you want to test.
3. Install the backend in a virtual environment with `pip install -e ".[dev]"` from `backend/`.
4. Install the dashboard with `npm ci` from `frontend/`.
5. Run the validation commands in `AGENTS.md` before opening a pull request.

## Pull requests

- Keep each pull request focused.
- Add or update tests for behavior changes.
- Never include credentials or real customer prompts in fixtures and logs.
- Explain user-visible changes and any compatibility impact.

By contributing, you agree that your contribution is licensed under the MIT License.
