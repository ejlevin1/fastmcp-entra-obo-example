# AGENTS.md

Purpose: a small, shareable reference example. Keep it minimal and readable; every line should teach something.

- Python 3.12+, managed with uv. `uv run pytest -q` must pass. `uv run ruff check . && uv run ruff format --check .`.
- Use only public FastMCP APIs (no private attributes, no monkey-patching).
- Never commit secrets, tenant IDs, client IDs, or real user names/emails. Config comes from env (`.env.example` lists keys).
- Conventional Commits.
