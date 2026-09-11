#!/usr/bin/env bash
set -euo pipefail

# One-time bootstrap for the GitHub OIDC identity and Terraform state.
# All long-lived project resources and IAM grants remain Terraform-managed.

PROJECT_ID=""
REPOSITORY=""
REGION="europe-west3"
POOL_ID="github"
PROVIDER_ID="github"
DEPLOY_SERVICE_ACCOUNT="github-deploy"
STATE_BUCKET=""

usage() {
  echo "Usage: $0 --project PROJECT_ID --repository OWNER/REPO [options]" >&2
  echo "  --region REGION (default: europe-west3)" >&2
  echo "  --pool POOL_ID (default: github)" >&2
  echo "  --provider PROVIDER_ID (default: github)" >&2
  echo "  --service-account ID (default: github-deploy)" >&2
  echo "  --state-bucket NAME (default: PROJECT_ID-tfstate)" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --repository) REPOSITORY="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --pool) POOL_ID="$2"; shift 2 ;;
    --provider) PROVIDER_ID="$2"; shift 2 ;;
    --service-account) DEPLOY_SERVICE_ACCOUNT="$2"; shift 2 ;;
    --state-bucket) STATE_BUCKET="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$PROJECT_ID" && -n "$REPOSITORY" ]] || { usage; exit 2; }
STATE_BUCKET="${STATE_BUCKET:-${PROJECT_ID}-tfstate}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
POOL_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
PRINCIPAL="principalSet://iam.googleapis.com/${POOL_RESOURCE}/attribute.repository/${REPOSITORY}"
DEPLOY_EMAIL="${DEPLOY_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com serviceusage.googleapis.com storage.googleapis.com

if ! gcloud iam service-accounts describe "$DEPLOY_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DEPLOY_SERVICE_ACCOUNT" --project="$PROJECT_ID" --display-name="Episode Calendar GitHub deploy"
fi

if ! gcloud iam workload-identity-pools describe "$POOL_ID" --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL_ID" --project="$PROJECT_ID" --location=global --display-name="GitHub Actions"
fi

if ! gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" \
    --display-name="GitHub Actions OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == '${REPOSITORY}'"
fi

gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_EMAIL" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --member="$PRINCIPAL" >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_EMAIL" --project="$PROJECT_ID" \
  --role=roles/iam.serviceAccountTokenCreator --member="$PRINCIPAL" >/dev/null

if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access
fi
gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning >/dev/null

export TF_VAR_project_id="$PROJECT_ID"
export TF_VAR_region="$REGION"
export TF_VAR_image="${REGION}-docker.pkg.dev/${PROJECT_ID}/episode-calendar/app:bootstrap"
export TF_VAR_database_password="bootstrap-only-placeholder-not-used"
export TF_VAR_deploy_cloud_run=false
export TF_VAR_enable_import_schedule=false
export TF_VAR_manage_deploy_iam=true
if gcloud storage objects describe "gs://${STATE_BUCKET}/default.tfstate" --project="$PROJECT_ID" >/dev/null 2>&1; then
  # If a previous CI run initialized a partial remote state, merge the local
  # state backup (which contains the existing infrastructure) before continuing.
  if [[ ! -f infra/terraform.tfstate ]]; then
    LOCAL_BACKUP="$(find infra -maxdepth 1 -name 'terraform.tfstate.bootstrap-backup.*' -print | sort | tail -1)"
    if [[ -n "$LOCAL_BACKUP" ]]; then
      cp "$LOCAL_BACKUP" infra/terraform.tfstate
      rm -f infra/.terraform/terraform.tfstate
      terraform -chdir=infra init -input=false -backend=false
      for ROLE in "roles/artifactregistry.writer" "roles/cloudsql.admin" "roles/cloudscheduler.admin" "roles/firebasehosting.admin" "roles/iam.serviceAccountUser" "roles/run.admin" "roles/secretmanager.admin" "roles/serviceusage.serviceUsageAdmin" "roles/storage.admin"; do
        terraform -chdir=infra import -input=false \
          "google_project_iam_member.deploy[\"${ROLE}\"]" \
          "${PROJECT_ID}/${ROLE}/serviceAccount:${DEPLOY_EMAIL}" >/dev/null
      done
      rm -f infra/.terraform/terraform.tfstate
      terraform -chdir=infra init -input=false -migrate-state -force-copy -backend-config="bucket=${STATE_BUCKET}"
    fi
  fi
  # A previous local-backend checkout can make Terraform prompt for migration even
  # when the remote state already exists. Preserve those local files as a backup.
  BACKUP_SUFFIX="bootstrap-backup.$$"
  [[ -f infra/terraform.tfstate ]] && mv infra/terraform.tfstate "infra/terraform.tfstate.${BACKUP_SUFFIX}"
  [[ -f infra/terraform.tfstate.backup ]] && mv infra/terraform.tfstate.backup "infra/terraform.tfstate.backup.${BACKUP_SUFFIX}"
  [[ -f infra/.terraform/terraform.tfstate ]] && mv infra/.terraform/terraform.tfstate "infra/.terraform/terraform.tfstate.${BACKUP_SUFFIX}"
  terraform -chdir=infra init -input=false -reconfigure -backend-config="bucket=${STATE_BUCKET}"
else
  terraform -chdir=infra init -input=false -migrate-state -force-copy -backend-config="bucket=${STATE_BUCKET}"
fi
terraform -chdir=infra apply -input=false -auto-approve -target=google_project_iam_member.deploy

cat <<EOF
Bootstrap complete.
GCP_WORKLOAD_IDENTITY_PROVIDER=${POOL_RESOURCE}/providers/${PROVIDER_ID}
GCP_DEPLOY_SERVICE_ACCOUNT=${DEPLOY_EMAIL}
EOF
