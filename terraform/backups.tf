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
