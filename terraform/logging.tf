resource "google_bigquery_dataset" "api_logs" {
  count       = var.enable_log_sinks ? 1 : 0
  dataset_id  = "genetics_api_logs"
  project     = var.project_id
  location    = var.region
  description = "Sink destination for genetics results API endpoint access logs"
}

resource "google_logging_project_sink" "endpoint_access" {
  count                  = var.enable_log_sinks ? 1 : 0
  name                   = "endpoint-access-to-bigquery"
  project                = var.project_id
  description            = "Genetics results API endpoint access logs to BigQuery"
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.api_logs[0].dataset_id}"
  filter                 = "jsonPayload.log_type=\"endpoint_access\""
  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "endpoint_access_sink_writer" {
  count      = var.enable_log_sinks ? 1 : 0
  project    = var.project_id
  dataset_id = google_bigquery_dataset.api_logs[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.endpoint_access[0].writer_identity
}

resource "google_bigquery_dataset" "chat_logs" {
  count       = var.enable_log_sinks ? 1 : 0
  dataset_id  = "genetics_chat_logs"
  project     = var.project_id
  location    = var.region
  description = "Sink destination for chat-backend container logs (severity >= INFO)"
}

resource "google_logging_project_sink" "chat_backend" {
  count                  = var.enable_log_sinks ? 1 : 0
  name                   = "chat-backend-to-bigquery"
  project                = var.project_id
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.chat_logs[0].dataset_id}"
  filter                 = "resource.type=\"k8s_container\" AND resource.labels.container_name=\"chat-backend\" AND severity >= \"INFO\""
  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

# grant the sink's writer SA permission to write into the dataset
resource "google_bigquery_dataset_iam_member" "chat_backend_sink_writer" {
  count      = var.enable_log_sinks ? 1 : 0
  project    = var.project_id
  dataset_id = google_bigquery_dataset.chat_logs[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.chat_backend[0].writer_identity
}
