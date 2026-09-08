output "service_url" { value = try(google_cloud_run_v2_service.app[0].uri, null) }
output "sql_connection_name" { value = google_sql_database_instance.postgres.connection_name }
output "image_repository" { value = data.google_artifact_registry_repository.app.name }

output "import_schedule" {
  value = var.enable_import_schedule ? google_cloud_scheduler_job.import[0].name : null
}
