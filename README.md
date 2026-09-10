# episode-calendar

Episode Calendar is an open-source service for collecting, normalizing, and exposing TV episode
release dates from streaming providers.

## Architecture

The FastAPI application and async SQLAlchemy persistence layer live in `src/episode_calendar`.
The domain model is `Provider -> Series -> Season -> Episode -> EpisodeRelease`; releases are
separate so an episode can have multiple streaming or broadcast releases. Provider IDs are
scoped by provider and imports use idempotent upserts.

Provider adapters implement the generic abstraction in `providers/base.py`. Joyn Austria and RTL+
are isolated in their provider modules. The importer fetches complete series trees, normalizes
them, and persists them. API requests read PostgreSQL and never call providers directly.

## Development

### Prerequisites

Install or make available:

- **Git** — source control.
- **Python 3.13** — application runtime and tests.
- **uv** — Python dependencies and virtual environments.
- **Node.js 20+ and npm** — local frontend development and production builds.
- **Docker with Compose** — local PostgreSQL, containers, and image builds.
- **VS Code** (optional) — editor and integrated pytest runner.
- **Bruno** (optional) — manual REST/GraphQL requests.
- **curl and jq** (optional) — HTTP and JSON troubleshooting.
- **A modern browser with Developer Tools** — inspect public provider web-client configuration.

Cloud deployment additionally requires the **Google Cloud CLI (gcloud)**, **Terraform**, the
**Firebase CLI**, a Google Cloud project with billing, and a GitHub account for repository access. Homebrew is convenient for
installing command-line tools on macOS.

Never commit `.env`, provider keys, OAuth values, service-account keys, or Terraform state.

### Local Development

Install dependencies, configure the local environment, start PostgreSQL, and run migrations:

```shell
cp .env.example .env
uv sync
docker compose up -d db
uv run alembic upgrade head
```

Start the API in the first terminal:

```shell
uv run uvicorn episode_calendar.main:app --reload
```

The API is available at <http://localhost:8000>; `GET /health` reports its status. Keep this
terminal running. The local
`DATABASE_URL` uses SQLAlchemy's async PostgreSQL form:
`postgresql+asyncpg://user:password@host:5432/database`.

#### Dev Container

The optional Dev Container starts PostgreSQL and installs the locked dependencies automatically.
Open the repository in a compatible editor; if `.env` is missing, setup copies `.env.example`.
From the container terminal run:

```shell
uv run alembic upgrade head
uv run uvicorn episode_calendar.main:app --reload --host 0.0.0.0
```

#### Provider configuration and local imports

Maintain provider series in `config/series.json` (override with `SERIES_CONFIG_PATH`):

```json
{"joyn": ["villa-der-versuchung", "so-denkt-oesterreich"], "rtlplus": []}
```

For Joyn, open `joyn.at`, accept consent, open Developer Tools → Network, reload a series
page, select `api.joyn.de/graphql`, and copy its `x-api-key` header to `JOYN_API_KEY` in
`.env`. For RTL+, reload `plus.rtl.de`, accept the cookie banner, select the request to
`auth.rtl.de/.../protocol/openid-connect/token`, and copy the `client_secret` form value to
`RTLPLUS_OIDC_CLIENT_SECRET`. These are public web-client values, not user passwords. Never
copy or commit `Authorization`, `X-Bedrock-Token`, cookies, or other browser session data.

Import locally after the database is available:

```shell
uv run python -m episode_calendar.importer joyn
docker compose run --rm app uv run --no-sync python -m episode_calendar.importer all
```

For a fresh local database, run the combined import once after setting the provider credentials
and `config/series.json`. It is safe to repeat because imports are idempotent:

```shell
uv run python -m episode_calendar.importer all
```

Run this in a third terminal while the API is running, or before starting the API. The API only
shows episodes after the corresponding provider import has completed.

Imports are sequential, rate-limited, retried with backoff, and idempotent. Useful API requests:

```http
GET http://localhost:8000/api/v1/series
GET http://localhost:8000/api/v1/series/{id}
GET http://localhost:8000/api/v1/episodes?from=2026-09-06T00:00:00Z&to=2026-09-13T23:59:59Z
GET http://localhost:8000/api/v1/episodes/current-week?timezone=Europe/Vienna
GET http://localhost:8000/api/v1/episodes/next-week?platform=rtlplus
```

Episode results are ordered by release time. `from`, `to`, `series`, `platform`, and
`timezone` are optional filters; week endpoints use Monday-to-Sunday weeks in
`Europe/Vienna` by default. The series `{id}` is the internal database UUID. The same
requests can be made from Bruno.

#### Local frontend

In a second terminal, start the responsive React/Vite frontend from `frontend/`. It uses the local
API by default:

```shell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Set `VITE_API_BASE_URL` when the API runs somewhere else, for
example `VITE_API_BASE_URL=http://localhost:8000 npm run dev`. Create a production build with
`npm run build` and preview it with `npm run preview`. Filter selections are kept in the browser's
local storage; no account or installation is required.

### Tests and checks

Tests use an isolated SQLite database and never contact streaming providers:

```shell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Cloud Environment

Cloud deployment uses Cloud Run, Cloud SQL, Secret Manager, Artifact Registry, Cloud Scheduler,
and Terraform. Terraform is intentionally not run by CI. Review billing and quotas before
continuing. Use Application Default Credentials locally rather than service-account key files.

### Setup Cloud Environment

1. Install and authenticate the [Google Cloud CLI](https://cloud.google.com/sdk/docs/initialize):

   ```shell
   gcloud init
   gcloud auth application-default login
   ```

2. Create or select a globally unique project and link an open billing account:

   ```shell
   gcloud projects create PROJECT_ID --name="Episode Calendar" --set-as-default
   gcloud billing accounts list
   gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
   gcloud config set project PROJECT_ID
   gcloud auth application-default set-quota-project PROJECT_ID
   ```

   If service activation reports that billing is not open, activate or create an open billing
   account first. Project IDs are permanent; do not use credentials or API keys as the ID.

3. Enable APIs and create the Artifact Registry repository:

   ```shell
   gcloud services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com iam.googleapis.com
   gcloud artifacts repositories create episode-calendar --repository-format=docker --location=europe-west3 --description="episode-calendar images"
   ```

   If the repository already exists, keep it and continue. Authenticate Docker once per machine:

   ```shell
   gcloud auth configure-docker europe-west3-docker.pkg.dev
   ```

4. Verify the selected identity and project:

   ```shell
   gcloud config get-value project
   gcloud auth list
   gcloud auth application-default print-access-token >/dev/null && echo "ADC ready"
   ```

### Deploy Infrastructure

The first Terraform apply creates Cloud SQL, IAM, and empty Secret Manager containers. Copy the
example variables, generate a password, and put it only in the ignored local tfvars file:

```shell
cp infra/terraform.tfvars.example infra/terraform.tfvars
openssl rand -hex 24
```

Copy the generated value into `database_password` in `infra/terraform.tfvars`. Keep
`deploy_cloud_run = false` and `enable_import_schedule = false` for this first apply. The
password is sensitive but still exists in local Terraform state.

```shell
terraform -chdir=infra init
terraform -chdir=infra plan
terraform -chdir=infra apply
```

The Cloud SQL configuration explicitly uses the `ENTERPRISE` edition because `db-f1-micro` is
not available in `ENTERPRISE_PLUS`. If an apply fails part-way through, fix the issue and run
the same apply again.

Add Secret Manager versions without files or command-line secret arguments. Use the same database
password and provider values discovered during local setup:

```shell
CONNECTION_NAME="$(terraform -chdir=infra output -raw sql_connection_name)"
read -r -s DB_PASSWORD; echo
printf 'postgresql+asyncpg://episode_calendar:%s@/episode_calendar?host=/cloudsql/%s\n' "$DB_PASSWORD" "$CONNECTION_NAME" | gcloud secrets versions add episode-calendar-database-url --data-file=-
read -r -s JOYN_API_KEY; echo
printf '%s' "$JOYN_API_KEY" | gcloud secrets versions add episode-calendar-joyn-api-key --data-file=-
read -r -s RTLPLUS_CLIENT_SECRET; echo
printf '%s' "$RTLPLUS_CLIENT_SECRET" | gcloud secrets versions add episode-calendar-rtlplus-client-secret --data-file=-
unset DB_PASSWORD JOYN_API_KEY RTLPLUS_CLIENT_SECRET CONNECTION_NAME
```

### Deploy App

Build and push an amd64 image. Apple Silicon users must specify the platform; use an immutable
tag or digest when possible:

```shell
docker build --platform linux/amd64 --provenance=false -t REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG . && docker push REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG
```

Set that image reference in `infra/terraform.tfvars`, set `deploy_cloud_run = true`, and apply:

```shell
terraform -chdir=infra plan
terraform -chdir=infra apply
```

The output contains `service_url`. The application container does not run migrations on startup.
Deploy and execute the migration job:

```shell
gcloud run jobs deploy episode-calendar-migrate --image=REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG --region=REGION --project=PROJECT_ID --service-account=episode-calendar-runtime@PROJECT_ID.iam.gserviceaccount.com --set-cloudsql-instances=PROJECT_ID:REGION:episode-calendar-postgres --set-secrets=DATABASE_URL=episode-calendar-database-url:latest --command=uv --args=run,--no-sync,alembic,upgrade,head
gcloud run jobs execute episode-calendar-migrate --region=REGION --project=PROJECT_ID --wait
```

Deploy and execute the provider import job:

```shell
gcloud run jobs deploy episode-calendar-import --image=REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG --region=REGION --project=PROJECT_ID --service-account=episode-calendar-runtime@PROJECT_ID.iam.gserviceaccount.com --set-cloudsql-instances=PROJECT_ID:REGION:episode-calendar-postgres --set-secrets=DATABASE_URL=episode-calendar-database-url:latest,JOYN_API_KEY=episode-calendar-joyn-api-key:latest,RTLPLUS_OIDC_CLIENT_SECRET=episode-calendar-rtlplus-client-secret:latest --command=uv --args=run,--no-sync,python,-m,episode_calendar.importer,all
gcloud run jobs execute episode-calendar-import --region=REGION --project=PROJECT_ID --wait
```

### Deploy Frontend

Firebase Hosting serves the static Vite build from the existing Google Cloud project. Install and
authenticate the Firebase CLI once:

```shell
npm install --global firebase-tools
firebase login
gcloud services enable firebase.googleapis.com --project=episode-calendar-67234
firebase projects:addfirebase
firebase use episode-calendar-67234
firebase hosting:sites:create episode-calendar-67234 --project=episode-calendar-67234
```

When `firebase projects:addfirebase` prompts for a project, select `episode-calendar-67234`. Before
running it, open the project once in the Firebase console and accept the Firebase Terms if prompted;
this acceptance cannot be completed by the CLI. Adding Firebase is a one-time,
irreversible project-level operation. The default Hosting site is normally provisioned as part of
this step; if it is not, run `firebase hosting:sites:create` afterwards. If site creation still
returns HTTP 403, the signed-in account needs the **Firebase Hosting Admin** role (or Firebase
Develop Admin) under **IAM & Admin → IAM**.

Build the frontend with the deployed API URL and publish it:

```shell
cd frontend
VITE_API_BASE_URL="$(terraform -chdir=../infra output -raw service_url)" npm run build
cd ..
firebase deploy --only hosting
```

The frontend is then available at `https://episode-calendar-67234.web.app`. Add that origin to
`cors_origins` in `infra/terraform.tfvars` (alongside the local origins) and apply Terraform so
the Cloud Run API accepts browser requests from Firebase Hosting:

```shell
terraform -chdir=infra apply
```

Verify that the deployed API returns a CORS header before opening the frontend:

```shell
curl -i -H "Origin: https://episode-calendar-67234.web.app" "$(terraform -chdir=infra output -raw service_url)/api/v1/series"
```

The response must contain `access-control-allow-origin: https://episode-calendar-67234.web.app`.

If the header is missing, inspect the active Cloud Run revision and its environment:

```shell
gcloud run services describe episode-calendar --region=europe-west3 --project=episode-calendar-67234 --format='yaml(status.latestReadyRevisionName,spec.template.containers[0].image,spec.template.containers[0].env)'
```

Set `enable_import_schedule = true` and apply Terraform once more. The default schedule is
`0 3 * * *` in `Europe/Vienna`; it invokes the existing import job with a dedicated service
account:

```shell
terraform -chdir=infra apply
gcloud scheduler jobs list --location=REGION
gcloud scheduler jobs run episode-calendar-import-daily --location=REGION
```

Cloud Scheduler currently provides three jobs per billing account per month at no charge; one
daily scheduler job is within that allowance. Cloud Run job execution and Cloud SQL runtime are
separate usage considerations. See the [official Scheduler pricing](https://cloud.google.com/scheduler/pricing).

Verify the deployment:

```shell
curl "$(terraform -chdir=infra output -raw service_url)/health"
curl "$(terraform -chdir=infra output -raw service_url)/api/v1/series"
```

### Deploy an update

After changing application code, `config/series.json`, or API behavior:

1. Run local tests and checks:
   ```shell
   uv run pytest && uv run ruff check .
   ```
2. Build and push a new `linux/amd64` image with an immutable tag, then update `image` in
   `infra/terraform.tfvars`:
   ```shell
   docker build --platform linux/amd64 --provenance=false -t REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG . && docker push REGION-docker.pkg.dev/PROJECT_ID/episode-calendar/app:TAG
   ```
3. Deploy the new revision:
   ```shell
   terraform -chdir=infra plan && terraform -chdir=infra apply
   ```
4. If migrations changed, execute the migration job; then refresh provider data:
   ```shell
   gcloud run jobs execute episode-calendar-migrate --region=REGION --project=PROJECT_ID --wait
   gcloud run jobs execute episode-calendar-import --region=REGION --project=PROJECT_ID --wait
   ```
5. Verify `/health` and the API. The daily scheduler continues to execute the existing import job.

## Planned work

Future increments can add a calendar UI, iCal feeds, Home Assistant integrations, and additional
provider adapters.
