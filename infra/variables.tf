variable "project_id" { type = string }

variable "region" {
  type    = string
  default = "europe-west3"
}

variable "service_name" {
  type    = string
  default = "episode-calendar"
}

variable "image" { type = string }

variable "deploy_cloud_run" {
  type    = bool
  default = false
}

variable "enable_import_schedule" {
  type    = bool
  default = false
}

variable "import_job_name" {
  type    = string
  default = "episode-calendar-import"
}

variable "import_schedule" {
  type    = string
  default = "0 3 * * *"
}

variable "import_timezone" {
  type    = string
  default = "Europe/Vienna"
}

variable "cors_origins" {
  type    = string
  default = "http://localhost:5173,http://127.0.0.1:5173"
}

variable "database_name" {
  type    = string
  default = "episode_calendar"
}

variable "database_user" {
  type    = string
  default = "episode_calendar"
}

variable "database_password" {
  type      = string
  sensitive = true
  nullable  = false

  validation {
    condition     = length(trimspace(var.database_password)) >= 16
    error_message = "database_password must contain at least 16 non-whitespace characters."
  }
}

variable "database_url_secret" {
  type    = string
  default = "episode-calendar-database-url"
}

variable "joyn_api_key_secret" {
  type    = string
  default = "episode-calendar-joyn-api-key"
}

variable "rtlplus_client_secret_secret" {
  type    = string
  default = "episode-calendar-rtlplus-client-secret"
}
