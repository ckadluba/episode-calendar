# Project instructions

Episode Calendar collects provider release data and exposes normalized TV series, season,
episode, and release records. It uses Python, FastAPI, async SQLAlchemy 2.x, PostgreSQL,
Alembic, httpx, pytest, uv, and Docker Compose.

- Keep provider HTTP/payload details inside `src/episode_calendar/importer/providers`; adapters
  return the normalized models defined in `importer/providers/base.py`. The shared adapter behavior
  is documented in `docs/providers/README.md` and each adapter's specialties/deviations in
  `docs/providers/<provider>.md`; keep those pages in sync when an adapter changes.
- Joyn Austria's verified-vs-assumed API contract is documented in `docs/providers/joyn.md`; keep
  its headers, GraphQL details, and API key handling inside the Joyn adapter.
- The initial provider lists live in `config/series.json` (override with `SERIES_CONFIG_PATH`).
  The file uses a top-level `platforms` list; each platform has an `id`, display `name`, optional
  `run_import` and `display` booleans (both default to true), an optional `hasPrereleases` boolean
  (default true), and a `series` list. Series entries support the same two optional booleans.
  Import with `uv run python -m episode_calendar.importer joyn`; imports must remain idempotent.
- Series configuration entries always require an `id`; omit the optional `comment` when the ID is
  already a clear, human-readable slug.
- Every new provider must get a distinct platform badge color in
  `frontend/src/styles.css`; do not leave its calendar badge on the generic fallback color.
- Never fetch provider data from an API request. Imports are a separate workflow and must be
  idempotent.
- Every change to the import/rerun logic (`importer/service.py` and anything that shapes stored
  releases) requires a full before/after comparison: run `uv run python -m episode_calendar.importer all`
  into two empty databases, once with and once without the change, then diff providers, series,
  seasons, episodes, and `episode_releases` (keyed by external IDs). At minimum do this whenever
  the change risks unexpected or unwanted side effects on providers other than the one being
  fixed.
- Preserve external IDs under provider-scoped uniqueness. Do not move release data onto
  `Episode`; one episode can have multiple `EpisodeRelease` records.
- Require timezone-aware release timestamps and normalize them to UTC for persistence.
- Add an Alembic migration for schema changes. Do not edit an already deployed migration.
- Never treat Joyn `PREVIEW`-marked content as a prerelease; only Joyn+ (`PLUS`/`PREMIUM`) content
  counts as "Vorab". `PREVIEW` means "Vor TV" for free users and is a regular early release.
- Do not add real provider integrations, a scheduler, authentication, queues, caching, or UI
  infrastructure without a task requiring them.

## Git workflow

Start every new change on a fresh work branch created from the currently pulled `main`:

```sh
git switch main
git pull
git switch -c <work-branch>
```

Never develop directly on `main`. Do not commit or push without an explicit request.

After every code or test change, run `uv run --no-sync ruff check .` and
`uv run --no-sync ruff format --check .`; also run `uv run pytest` before committing.
Run locally with `uv run uvicorn episode_calendar.api.main:app --reload`; copy `.env.example` to
`.env`, start PostgreSQL with `docker compose up -d db`, and migrate with
`uv run alembic upgrade head`.
