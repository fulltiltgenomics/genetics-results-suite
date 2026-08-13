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

locals {
  # the state bucket `terraform init -backend-config=` actually bound this working directory to.
  # this is the only in-config signal of WHICH state a run writes to, and it is independent of every
  # variable — which is what makes it usable to check the tfvars in place against the selected state.
  backend_cache_path         = "${path.module}/.terraform/terraform.tfstate"
  initialized_backend_bucket = fileexists(local.backend_cache_path) ? try(jsondecode(file(local.backend_cache_path)).backend.config.bucket, "") : ""

  # the bucket the profile named by var.config_profile is supposed to use, read from its own
  # .tfbackend rather than hardcoded, so renaming a bucket cannot make this guard lie.
  backend_config_path     = "${path.module}/${var.config_profile}.tfbackend"
  expected_backend_bucket = fileexists(local.backend_config_path) ? try(regex("bucket\\s*=\\s*\"([^\"]+)\"", file(local.backend_config_path))[0], "") : ""
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

    # backstop for a bare `terraform apply` that bypasses scripts/deploy.sh: the main checkout keeps
    # terraform.tfvars.daly and .finngen beside the active terraform.tfvars, so the values in place
    # can belong to a different profile than the state this directory is initialized against. Both
    # sides fall back to "" when they cannot be determined (never initialized, or no .tfbackend for
    # this profile) — in both of those cases terraform fails on its own before touching anything.
    precondition {
      condition     = !var.require_tfvars || local.initialized_backend_bucket == "" || local.expected_backend_bucket == "" || local.initialized_backend_bucket == local.expected_backend_bucket
      error_message = <<-EOT
        config profile does not match the initialized terraform state. config_profile = "${var.config_profile}" expects state bucket "${local.expected_backend_bucket}", but this directory was initialized against "${local.initialized_backend_bucket}".
        Applying would write one profile's project, region, domains and IAM into the other profile's state. The values all look plausible, so the plan will not look wrong.
        Fix: put the matching values in place (cp terraform.tfvars.${var.config_profile} terraform.tfvars) or re-init against the matching backend (terraform init -backend-config=${var.config_profile}.tfbackend -reconfigure). scripts/deploy.sh does both consistently.
      EOT
    }
  }
}
