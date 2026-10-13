# single-cell expression data (CellXGene): the bucket the Cloud Batch workers write to, the
# Batch API they run on, and the worker identity. Owned by one deployment only — see
# var.expression_data_enabled for why the other deployment in the same project must leave it off.

locals {
  expression_iam = var.expression_data_enabled && var.manage_iam

  # keys are static so for_each never depends on an email that is unknown until apply
  expression_bucket_readers = local.expression_iam ? merge(
    { deployment = "serviceAccount:${google_service_account.genetics_suite[0].email}" },
    { for email in var.expression_bucket_extra_readers : email => "serviceAccount:${email}" },
  ) : {}
}

resource "google_project_service" "batch" {
  count   = var.expression_data_enabled ? 1 : 0
  project = var.project_id
  service = "batch.googleapis.com"

  # the other deployment in this project, and anything run by hand, may depend on the API
  disable_on_destroy = false
}

# the Batch workers parse third-party files, so they get a VPC of their own with no ingress rules
# (implied deny); egress goes out through each worker's external IP, so no NAT. Neither existing
# network is safe: GKE's gke-<cluster>-...-vms rule admits the node subnet to every node port,
# and broad-allow holds the admin dev VM under an allow-all-internal rule.
resource "google_compute_network" "expression_batch" {
  count                   = var.expression_data_enabled ? 1 : 0
  name                    = "expression-batch-network"
  project                 = var.project_id
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "expression_batch" {
  count                    = var.expression_data_enabled ? 1 : 0
  name                     = "expression-batch-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.expression_batch[0].id
  ip_cidr_range            = var.expression_subnet_cidr
  private_ip_google_access = true
}

resource "google_storage_bucket" "expression_data" {
  count                       = var.expression_data_enabled ? 1 : 0
  name                        = var.expression_bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  labels = {
    managed-by = "terraform"
    purpose    = "expression-data"
  }
}

resource "google_service_account" "expression_worker" {
  count        = local.expression_iam ? 1 : 0
  account_id   = "expression-batch-worker"
  display_name = "Expression data Cloud Batch worker"
  description  = "Runs the CellXGene per-dataset Batch jobs; writes only to the expression bucket"
}

resource "google_storage_bucket_iam_member" "expression_worker_writer" {
  count  = local.expression_iam ? 1 : 0
  bucket = google_storage_bucket.expression_data[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.expression_worker[0].email}"
}

# Batch requires the job's service account to hold agentReporter so the VM agent can report
# task state; without it jobs sit in SCHEDULED and fail. logWriter is what makes the job's
# logs reach Cloud Logging (logsPolicy CLOUD_LOGGING).
resource "google_project_iam_member" "expression_worker_batch_agent" {
  count   = local.expression_iam ? 1 : 0
  project = var.project_id
  role    = "roles/batch.agentReporter"
  member  = "serviceAccount:${google_service_account.expression_worker[0].email}"
}

resource "google_project_iam_member" "expression_worker_log_writer" {
  count   = local.expression_iam ? 1 : 0
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.expression_worker[0].email}"
}

# redundant today with the project-level storage.objectViewer in iam.tf; kept at bucket level
# so serving survives that grant being narrowed
resource "google_storage_bucket_iam_member" "expression_readers" {
  for_each = local.expression_bucket_readers
  bucket   = google_storage_bucket.expression_data[0].name
  role     = "roles/storage.objectViewer"
  member   = each.value
}
