# Project instructions

Episode Calendar collects provider release data and exposes normalized TV series, season,
episode, and release records. It uses Python, FastAPI, async SQLAlchemy 2.x, PostgreSQL,
Alembic, httpx, pytest, uv, and Docker Compose.

- Keep provider HTTP/payload details inside `src/episode_calendar/providers`; adapters return
  the normalized models defined in `providers/base.py`.
- Never fetch provider data from an API request. Imports are a separate workflow and must be
  idempotent.
- Preserve external IDs under provider-scoped uniqueness. Do not move release data onto
  `Episode`; one episode can have multiple `EpisodeRelease` records.
- Require timezone-aware release timestamps and normalize them to UTC for persistence.
- Add an Alembic migration for schema changes. Do not edit an already deployed migration.
- Do not add real provider integrations, a scheduler, authentication, queues, caching, or UI
  infrastructure without a task requiring them.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` before committing.
Run locally with `uv run uvicorn episode_calendar.main:app --reload`; copy `.env.example` to
`.env`, start PostgreSQL with `docker compose up -d db`, and migrate with
`uv run alembic upgrade head`.
