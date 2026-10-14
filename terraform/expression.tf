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

  # lifecycle actions run asynchronously (up to ~24 h), so a raw file sits in Standard for its
  # first day; the worker may upload with storage class COLDLINE to skip that
  lifecycle_rule {
    condition {
      age                   = 0
      matches_prefix        = ["cellxgene/raw/"]
      matches_storage_class = ["STANDARD"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  # raw files are named by dataset_version_id and never overwritten, so with versioning off
  # nothing ever becomes noncurrent: a superseded version is marked instead, by the driver
  # setting the object's customTime when CELLxGENE retires that version. A metadata patch, not a
  # copy, so it costs no Coldline retrieval or early-deletion fee. customTime can never be
  # cleared or moved earlier — an object marked by mistake is rescued by pushing it far forward.
  # An object nobody marks is kept indefinitely.
  lifecycle_rule {
    condition {
      days_since_custom_time = 90
      matches_prefix         = ["cellxgene/raw/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["cellxgene/runs/"]
    }
    action {
      type = "Delete"
    }
  }

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

resource "google_project_service" "cloudscheduler" {
  count              = var.expression_data_enabled ? 1 : 0
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

# the driver itself runs as a Batch job: it plans, submits and supervises the per-dataset worker
# jobs, then publishes. BigQuery grants for the cellxgene tables are added with those tables.
resource "google_service_account" "expression_driver" {
  count        = local.expression_iam ? 1 : 0
  account_id   = "expression-batch-driver"
  display_name = "Expression data refresh driver"
  description  = "Submits and supervises the CellXGene worker Batch jobs and publishes their output"
}

resource "google_project_iam_member" "expression_driver" {
  for_each = local.expression_iam ? toset([
    "roles/batch.jobsEditor",
    "roles/batch.agentReporter",
    "roles/logging.logWriter",
  ]) : toset([])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.expression_driver[0].email}"
}

resource "google_storage_bucket_iam_member" "expression_driver_writer" {
  count  = local.expression_iam ? 1 : 0
  bucket = google_storage_bucket.expression_data[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.expression_driver[0].email}"
}

# on the worker GSA only: a project-wide serviceAccountUser would let the driver run jobs as
# any service account in the project, including the cluster's
resource "google_service_account_iam_member" "expression_driver_acts_as_worker" {
  count              = local.expression_iam ? 1 : 0
  service_account_id = google_service_account.expression_worker[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.expression_driver[0].email}"
}

resource "google_service_account" "expression_scheduler" {
  count        = local.expression_iam ? 1 : 0
  account_id   = "expression-scheduler"
  display_name = "Expression data refresh scheduler"
  description  = "Identity Cloud Scheduler uses to create the expression driver Batch job"
}

resource "google_project_iam_member" "expression_scheduler_batch" {
  count   = local.expression_iam ? 1 : 0
  project = var.project_id
  role    = "roles/batch.jobsEditor"
  member  = "serviceAccount:${google_service_account.expression_scheduler[0].email}"
}

resource "google_service_account_iam_member" "expression_scheduler_acts_as_driver" {
  count              = local.expression_iam ? 1 : 0
  service_account_id = google_service_account.expression_driver[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.expression_scheduler[0].email}"
}

# created paused; unpausing (gcloud scheduler jobs resume) is the decision to start weekly
# refreshes, taken after the initial full run is approved. paused is ignored after create so a
# later apply, including deploy.sh's unattended one, never re-pauses a resumed job.
resource "google_cloud_scheduler_job" "expression_refresh" {
  count     = local.expression_iam && var.expression_refresh_schedule_enabled ? 1 : 0
  name      = "expression-refresh"
  project   = var.project_id
  region    = var.region
  schedule  = var.expression_refresh_schedule
  time_zone = "Etc/UTC"
  paused    = true

  http_target {
    http_method = "POST"
    uri         = "https://batch.googleapis.com/v1/projects/${var.project_id}/locations/${var.region}/jobs"
    headers     = { "Content-Type" = "application/json" }
    body        = filebase64(var.expression_refresh_job_spec_path)

    oauth_token {
      service_account_email = google_service_account.expression_scheduler[0].email
    }
  }

  depends_on = [google_project_service.cloudscheduler]

  lifecycle {
    ignore_changes = [paused]

    precondition {
      condition     = var.expression_refresh_job_spec_path != ""
      error_message = "expression_refresh_schedule_enabled needs expression_refresh_job_spec_path: the driver Batch job spec (JSON)."
    }
  }
}

# repo-scoped (not project-level) so the Batch VM and the driver can pull the pipeline image
# without reading any other repository in the project.
resource "google_artifact_registry_repository_iam_member" "expression_image_reader" {
  for_each = local.expression_iam ? {
    worker = google_service_account.expression_worker[0].email
    driver = google_service_account.expression_driver[0].email
  } : {}
  project    = google_artifact_registry_repository.docker.project
  location   = google_artifact_registry_repository.docker.location
  repository = google_artifact_registry_repository.docker.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}
