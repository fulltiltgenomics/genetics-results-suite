terraform {
  required_version = ">= 1.14"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }

  backend "gcs" {
    bucket = "genetics-results-terraform-daly"
    prefix = "genetics-results-suite"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_client_config" "default" {}

# reference the cluster created in gke.tf
provider "kubernetes" {
  host                   = "https://${google_container_cluster.primary.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.primary.master_auth[0].cluster_ca_certificate)
}

check "iam_config" {
  assert {
    condition     = !(var.manage_iam && var.node_service_account != "")
    error_message = "Set either manage_iam=true (creates Workload Identity SA) or node_service_account (uses existing SA), not both."
  }
}

# reference existing static IP
data "google_compute_global_address" "static_ip" {
  name = var.static_ip_name
}
