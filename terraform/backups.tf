locals {
  keycloak_backup_bucket = var.keycloak_backup_bucket_name != "" ? var.keycloak_backup_bucket_name : "${var.project_id}-keycloak-backups"
}

# GCS bucket for Keycloak Postgres logical backups (pg_dump from the backup CronJob).
# Lifecycle rule enforces retention so old dumps are auto-deleted.
resource "google_storage_bucket" "keycloak_backups" {
  name                        = local.keycloak_backup_bucket
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = var.keycloak_backup_retention_days
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    managed-by = "terraform"
    purpose    = "keycloak-backup"
  }
}

# let the workload-identity SA write/read backups (only when terraform manages IAM;
# with manage_iam=false, grant the workload SA roles/storage.objectAdmin on this bucket manually)
resource "google_storage_bucket_iam_member" "keycloak_backups_writer" {
  count  = var.manage_iam ? 1 : 0
  bucket = google_storage_bucket.keycloak_backups.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.genetics_suite[0].email}"
}

# daily snapshot schedule for the chat-backend PVC disk
resource "google_compute_resource_policy" "chat_data_snapshots" {
  name    = "chat-data-daily-snapshot"
  region  = var.region
  project = var.project_id

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "03:00"
      }
    }

    retention_policy {
      max_retention_days    = var.snapshot_retention_days
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }

    snapshot_properties {
      storage_locations = [var.region]
      labels = {
        managed-by = "terraform"
        purpose    = "chat-data-backup"
      }
    }
  }
}
