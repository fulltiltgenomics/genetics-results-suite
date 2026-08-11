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

  # backend config is selected per profile via -backend-config flag
  # see daly.tfbackend and finngen.tfbackend
  backend "gcs" {}
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

  # fail closed when terraform.tfvars is missing. the file is gitignored and lives only in the
  # main checkout, so a run from a git worktree would otherwise silently fall back to variable
  # defaults (enable_log_sinks=false, manage_iam=true, config_profile=daly, ...) and plan a
  # destroy of live infrastructure that reads like an ordinary small change.
  lifecycle {
    precondition {
      condition     = !var.require_tfvars || fileexists("${path.module}/terraform.tfvars")
      error_message = <<-EOT
        terraform.tfvars not found at ${path.module}/terraform.tfvars. It is gitignored and exists only in the main checkout, so a run from a git worktree (.claude/worktrees/*) or a fresh clone falls back to variable defaults instead.
        Those defaults are destructive: enable_log_sinks=false DESTROYS both BigQuery log sinks, manage_iam=true with an empty node_service_account REPLACES the GKE node pool, and config_profile/oauth_email_domain revert to the daly/Broad values.
        Fix: run terraform from the main checkout, or copy terraform.tfvars.example and fill it in. To supply values another way, pass both -var-file=/path/to/terraform.tfvars and -var require_tfvars=false.
      EOT
    }
  }
}
