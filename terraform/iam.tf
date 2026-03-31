# GCP service account for workload identity (read-only access)
resource "google_service_account" "genetics_suite" {
  count        = var.manage_iam ? 1 : 0
  account_id   = "genetics-suite-gke"
  display_name = "Genetics Suite GKE Workload Identity"
  description  = "Read-only access to BigQuery and GCS for the genetics results suite"
}

# BigQuery: read table data
resource "google_project_iam_member" "bq_viewer" {
  count   = var.manage_iam ? 1 : 0
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.genetics_suite[0].email}"
}

# BigQuery: run queries (required for SELECT, cannot modify data)
resource "google_project_iam_member" "bq_job_user" {
  count   = var.manage_iam ? 1 : 0
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.genetics_suite[0].email}"
}

# GCS: read objects
resource "google_project_iam_member" "storage_viewer" {
  count   = var.manage_iam ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.genetics_suite[0].email}"
}

# allow the KSA to impersonate the GSA via Workload Identity
resource "google_service_account_iam_member" "workload_identity" {
  count              = var.manage_iam ? 1 : 0
  service_account_id = google_service_account.genetics_suite[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/genetics-suite]"
}
