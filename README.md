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
in `providers/base.py`. Joyn Austria is implemented in `providers/joyn.py`; its current API
contract and known fragility are documented in [docs/providers/joyn.md](docs/providers/joyn.md).
A future RTL+ adapter will follow the same boundary. A separate importer will call an adapter,
normalize its result, and persist it idempotently; API requests will never fetch from providers
directly.

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

To use the Joyn provider, set `JOYN_API_KEY` to the public key used by the Joyn Austria web
application. It is runtime client configuration, not a user password; do not commit the value.
Maintain the initial provider lists in `config/series.json` (the path can be overridden with
`SERIES_CONFIG_PATH`), for example:

```json
{"joyn": ["villa-der-versuchung", "so-denkt-oesterreich"], "rtlplus": []}
```

Run the manual importer after the database is available:

```shell
uv run python -m episode_calendar.importer joyn
```

The same command can be run in the application container with:

```shell
docker compose run --rm app uv run --no-sync python -m episode_calendar.importer joyn
```

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

### Setup Cloud Environment

The following creates the Google Cloud project prerequisites. These commands do not provision
Cloud Run or Cloud SQL yet; that will be handled by Terraform in a later step. Google Cloud
requires a billing account for the services we plan to use, so review quotas and expected costs
before continuing.

1. Install and initialize the [Google Cloud CLI](https://cloud.google.com/sdk/docs/initialize),
   then sign in:

   ```shell
   gcloud init
   gcloud auth application-default login
   ```

2. Choose a globally unique project ID and create the project (replace the placeholders):

   ```shell
   gcloud projects create PROJECT_ID --name="Episode Calendar" --set-as-default
   ```

   Project IDs are permanent identifiers. Do not use credentials or API keys as the project ID.

3. Link a billing account. List the billing accounts available to your user and copy the ID:

   ```shell
   gcloud billing accounts list
   gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
   ```

4. Set the project and align the Application Default Credentials quota project:

   ```shell
   gcloud config set project PROJECT_ID
   gcloud auth application-default set-quota-project PROJECT_ID
   ```

   If Google Cloud reports that the billing account is not open, activate or create an open
   billing account before enabling services. Verify it with `gcloud billing accounts list` and
   link it using the command from step 3.

5. Enable the APIs needed for the planned deployment:

   ```shell
   gcloud services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com iam.googleapis.com
   ```

6. Create an Artifact Registry repository for container images:

   ```shell
   gcloud artifacts repositories create episode-calendar --repository-format=docker --location=europe-west3 --description="episode-calendar images"
   ```

7. Authenticate Docker, build the image, and push it to Artifact Registry:

   ```shell
   gcloud auth configure-docker REGION-docker.pkg.dev
   docker build -t REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:latest .
   docker push REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:latest
   ```

   Replace `REGION` with `europe-west3` (or the repository region). This validates the registry
   setup; Cloud Run is not deployed by these commands.

8. Verify the local identity and project configuration before running Terraform:

   ```shell
   gcloud config get-value project
   gcloud auth list
   gcloud auth application-default print-access-token >/dev/null && echo "ADC ready"
   ```

Never commit service-account keys, Terraform state, OAuth tokens, provider API keys, or `.env`.
Terraform should use Application Default Credentials locally and Workload Identity Federation in
CI rather than long-lived service-account key files. See Google's [Terraform authentication
guide](https://cloud.google.com/docs/terraform/authentication) for the recommended setup.

## Tests and checks

Tests use an isolated in-memory SQLite database and never contact a streaming provider.

```shell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Development

The current development workflow uses a temporary, JSON-based list of provider series
series. This will later be replaced by a more complete catalog/import workflow.

1. Copy `.env.example` to `.env` and set the local `JOYN_API_KEY`.
   The Joyn key is public web-client configuration. To find the current value, open
   `joyn.at` in a browser, open Developer Tools → Network, reload a series page, select the
   request to `api.joyn.de/graphql`, and copy the `x-api-key` request header into `.env`.
   For RTL+, open Developer Tools → Network, reload `plus.rtl.de`, accept the cookie banner,
   select the request to `auth.rtl.de/.../protocol/openid-connect/token`, and copy the
   `client_secret` form value into `RTLPLUS_OIDC_CLIENT_SECRET`. These values are public client
   configuration, but do not copy or commit `Authorization`, `X-Bedrock-Token`, cookies, or
   other account/device data.
2. Maintain `config/series.json` with one list per provider, for example:

   ```json
   {"joyn": ["villa-der-versuchung", "so-denkt-oesterreich"], "rtlplus": []}
   ```

3. Start PostgreSQL and the application:

   ```shell
   docker compose up --build -d
   ```

4. Import the configured series manually:

   ```shell
   docker compose run --rm app \
     uv run --no-sync python -m episode_calendar.importer joyn
   ```

   Beide Provider können gemeinsam importiert werden:

   ```shell
   docker compose run --rm app uv run --no-sync python -m episode_calendar.importer all
   ```

   Einzelne fehlgeschlagene Serien werden protokolliert und blockieren die übrigen Serien nicht;
   der Prozess endet anschließend mit einem Fehlercode, wenn mindestens ein Import fehlgeschlagen ist.

   The importer fetches complete series trees, normalizes them, and performs idempotent
   provider-scoped upserts into PostgreSQL.

5. Query the database-backed API. The API does not contact Joyn during a request:

   ```http
   GET http://localhost:8000/api/v1/series
   GET http://localhost:8000/api/v1/series/{id}
   GET http://localhost:8000/api/v1/episodes?from=2026-09-06T00:00:00Z&to=2026-09-13T23:59:59Z
   GET http://localhost:8000/api/v1/episodes/current-week
   GET http://localhost:8000/api/v1/episodes/next-week
   GET http://localhost:8000/api/v1/episodes/current-week?timezone=UTC
   ```

   The `{id}` value is the internal database UUID returned by the series list endpoint. Episode
   results are ordered by release time; `from`, `to`, and `series` are optional filters. The
   convenience endpoints use Monday-to-Sunday calendar weeks in the `Europe/Vienna` timezone
   and also accept the optional `series`, `platform` (`joyn` or `rtlplus`), and `timezone`
   (IANA name, default `Europe/Vienna`) filters. The
   series endpoints likewise accept `platform` as an optional query parameter.

Provider imports run sequentially with a configurable pause (`IMPORT_DELAY_SECONDS`, default
1 second). Temporary errors are retried up to `IMPORT_MAX_RETRIES` times using exponential
backoff with jitter; the providers' `Retry-After` header is honored when present.

RTL+ is supported experimentally through its current Bedrock layout endpoint. Configure the
numeric program ID (or a URL slug ending in `_p_<id>`) in `config/series.json` and provide the
public web-client value `RTLPLUS_OIDC_CLIENT_SECRET` at runtime; never commit it. The provider
automatically obtains short-lived OIDC and Bedrock tokens. RTL+ currently exposes episode metadata in layout blocks, while release times are
only present in the editorial SEO markdown schedule table. That parser is deliberately isolated
in `providers/rtlplus.py` and may require updates when the site changes. The tokens are browser
session credentials and are not suitable as permanent application secrets.

The same endpoints can be called from Bruno. Use `GET`, set the URL above, and send no request
body. All date-time query parameters should include an explicit timezone such as `Z` or `+02:00`.

## Planned work

Later increments can add provider adapters and an idempotent import service, followed by the
series and episode REST resources, periodic execution, a calendar UI, and iCal or Home
Assistant integrations.
