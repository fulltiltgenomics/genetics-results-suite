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

  # scoped to the in-cluster workloads (genetics-results-suite-re3). The bare
  # jsonPayload.log_type filter is project-wide, and the genetics-results-api-dev1 GCE VM emits the
  # same log_type through the Cloud Logging client library under log name "genetics-results-api".
  # That VM is not a decommissioned server and not real usage: it is a developer machine running
  # the results-api test suite (sourceLocation.file points inside a checkout under
  # /home/jkarjala/suite/genetics-results-api, and it burst 1,638 entries in one second), and it is
  # still emitting today — 1,377 rows on 2026-08-11. A BigQuery sink names its destination table
  # after the log ID, so that test noise lands in a table called `genetics_results_api` while the
  # GKE services — which log to stdout and therefore to a table called `stdout` — sit next to it.
  # Anyone asking "who calls the API" read the table whose name matched the service and got the
  # test suite, where user_email is always NULL.
  #
  # Applying this filter stops that feed deliberately and with no replacement: the dev VM's
  # endpoint_access records will no longer reach BigQuery at all. That is the intent — it is test
  # noise, not traffic anyone reports on.
  filter                 = "jsonPayload.log_type=\"endpoint_access\" AND resource.type=\"k8s_container\" AND resource.labels.namespace_name=\"genetics\""
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
