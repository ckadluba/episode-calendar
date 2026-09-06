# episode-calendar

Episode Calendar is an open-source service for collecting, normalizing, and exposing TV
episode release dates from streaming providers. This repository currently contains the
application foundation only; no real provider integration or import scheduler exists yet.

## Architecture

The FastAPI application and async SQLAlchemy persistence layer live in `src/episode_calendar`.
The core model is `Provider -> Series -> Season -> Episode -> EpisodeRelease`. Releases are
separate from episodes so one episode can have multiple streaming or broadcast dates.
Provider identifiers are stored within provider-scoped unique constraints, which gives future
import code stable upsert keys without assuming identifiers are globally unique.

Provider adapters implement `ProviderAdapter` and return the normalized, immutable structures
in `providers/base.py`. Future modules such as `providers/joyn.py` and `providers/rtlplus.py`
will contain all provider-specific HTTP and payload details. A separate importer will call an
adapter, normalize its result, and persist it idempotently; API requests will never fetch from
providers directly.

## Local development

Prerequisites are [uv](https://docs.astral.sh/uv/) and Docker with Compose.

```shell
cp .env.example .env
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn episode_calendar.main:app --reload
```

The API is then available at <http://localhost:8000>; `GET /health` reports its status. The
example environment file uses disposable local credentials. Choose different secrets outside
local development. `DATABASE_URL` must use SQLAlchemy's async PostgreSQL driver form:
`postgresql+asyncpg://user:password@host:5432/database`.

To build and run both the database and application in containers instead:

```shell
cp .env.example .env
docker compose up --build
```

### Dev Container

The repository also includes an optional Dev Container for compatible editors. Open the
repository in the container to start PostgreSQL and install the locked development dependencies
automatically. If `.env` does not exist, the container setup copies `.env.example` first.

The development virtual environment is kept inside the container, separate from any host
`.venv`. Apply migrations and start the API from the container terminal:

```shell
uv run alembic upgrade head
uv run uvicorn episode_calendar.main:app --reload --host 0.0.0.0
```

## Tests and checks

Tests use an isolated in-memory SQLite database and never contact a streaming provider.

```shell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Planned work

Later increments can add provider adapters and an idempotent import service, followed by the
series and episode REST resources, periodic execution, a calendar UI, and iCal or Home
Assistant integrations.
