locals { service_account = "${var.service_name}-runtime@${var.project_id}.iam.gserviceaccount.com" }

resource "google_project_service" "required" {
  for_each           = toset(["run.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com", "cloudscheduler.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

data "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = var.service_name
  depends_on    = [google_project_service.required]
}

resource "google_sql_database_instance" "postgres" {
  name             = "${var.service_name}-postgres"
  database_version = "POSTGRES_16"
  region           = var.region
  settings {
    edition           = "ENTERPRISE"
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10
    backup_configuration { enabled = true }
  }
  deletion_protection = true
  depends_on          = [google_project_service.required]
}

resource "google_sql_database" "app" {
  name     = var.database_name
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "app" {
  name     = var.database_user
  instance = google_sql_database_instance.postgres.name
  password = var.database_password
}

resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-runtime"
  display_name = "Episode Calendar Cloud Run runtime"
}

resource "google_project_iam_member" "cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = var.database_url_secret
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "joyn_api_key" {
  secret_id = var.joyn_api_key_secret
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "rtlplus_client_secret" {
  secret_id = var.rtlplus_client_secret_secret
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each = {
    database = google_secret_manager_secret.database_url.id
    joyn     = google_secret_manager_secret.joyn_api_key.id
    rtlplus  = google_secret_manager_secret.rtlplus_client_secret.id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "app" {
  count    = var.deploy_cloud_run ? 1 : 0
  name     = var.service_name
  location = var.region
  template {
    service_account = google_service_account.runtime.email
    scaling { max_instance_count = 2 }
    containers {
      image = var.image
      ports { container_port = 8000 }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "JOYN_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.joyn_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "RTLPLUS_OIDC_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.rtlplus_client_secret.secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.postgres.connection_name]
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.deploy_cloud_run ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_service_account" "scheduler" {
  count        = var.enable_import_schedule ? 1 : 0
  account_id   = "${var.service_name}-scheduler"
  display_name = "Episode Calendar import scheduler"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  count    = var.enable_import_schedule ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.import_job_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "import" {
  count     = var.enable_import_schedule ? 1 : 0
  name      = "${var.service_name}-import-daily"
  region    = var.region
  schedule  = var.import_schedule
  time_zone = var.import_timezone

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${var.import_job_name}:run"
    http_method = "POST"
    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}
