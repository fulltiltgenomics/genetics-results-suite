resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.zone

  # use a separately managed node pool
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  network    = google_compute_network.gke.id
  subnetwork = google_compute_subnetwork.gke.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  dynamic "workload_identity_config" {
    for_each = var.manage_iam ? [1] : []
    content {
      workload_pool = "${var.project_id}.svc.id.goog"
    }
  }

  datapath_provider = "ADVANCED_DATAPATH"

  addons_config {
    http_load_balancing {
      disabled = false
    }
  }

  # release channel for automatic upgrades
  release_channel {
    channel = "REGULAR"
  }

  # Google Managed Prometheus for metrics collection
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "POD", "DEPLOYMENT", "DAEMONSET", "STATEFULSET", "HPA"]
    managed_prometheus {
      enabled = true
    }
  }
}

resource "google_container_node_pool" "primary_nodes" {
  name     = "${var.cluster_name}-pool"
  location = var.zone
  cluster  = google_container_cluster.primary.name

  autoscaling {
    min_node_count = var.min_node_count
    max_node_count = var.max_node_count
  }

  node_config {
    machine_type    = var.machine_type
    service_account = var.manage_iam ? google_service_account.genetics_suite[0].email : (
      var.node_service_account != "" ? var.node_service_account : null
    )

    dynamic "workload_metadata_config" {
      for_each = var.manage_iam ? [1] : []
      content {
        mode = "GKE_METADATA"
      }
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    labels = {
      env = "production"
    }
  }
}
