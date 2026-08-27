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
  # see daly.tfbackend, daly-staging.tfbackend and finngen.tfbackend
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # PROJECT-scoped resource names carry this suffix so two deployments can share one GCP
  # project. `id_suffix` is the same value in a form BigQuery dataset ids accept (no hyphens).
  name_suffix = var.resource_suffix
  id_suffix   = replace(var.resource_suffix, "-", "_")
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

  # every tfvars file that could have supplied values to this run: the bare terraform.tfvars
  # (legacy single-deployment mode, auto-loaded) and the per-environment terraform.tfvars.<env>
  # that scripts/lib/env.sh passes with -var-file. All are gitignored, so the set is empty in a
  # worktree or a fresh clone — which is the case the guard below exists to catch. The
  # committed .example is excluded or it would satisfy the guard everywhere.
  tfvars_present = [
    for f in fileset(path.module, "terraform.tfvars*") : f
    if f != "terraform.tfvars.example"
  ]
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

  # fail closed when no tfvars file is present at all. they are gitignored and live only in the
  # main checkout, so a run from a git worktree would otherwise silently fall back to variable
  # defaults (enable_log_sinks=false, manage_iam=true, config_profile=daly, ...) and plan a
  # destroy of live infrastructure that reads like an ordinary small change.
  lifecycle {
    precondition {
      condition     = !var.require_tfvars || length(local.tfvars_present) > 0
      error_message = <<-EOT
        no terraform.tfvars / terraform.tfvars.<env> found in ${path.module}. They are gitignored and exist only in the main checkout, so a run from a git worktree (.claude/worktrees/*) or a fresh clone falls back to variable defaults instead.
        Those defaults are destructive: enable_log_sinks=false DESTROYS both BigQuery log sinks, manage_iam=true with an empty node_service_account REPLACES the GKE node pool, and config_profile/oauth_email_domain revert to the daly/Broad values.
        Fix: run terraform from the main checkout (scripts/deploy.sh selects the file from DEPLOY_ENV — see docs/environments.md), or copy terraform.tfvars.example and fill it in. To supply values another way, pass both -var-file=/path/to/tfvars and -var require_tfvars=false.
      EOT
    }

    # backstop for a bare `terraform apply` that bypasses the entry-point scripts: the main checkout
    # keeps a terraform.tfvars.<env> per deployment, so the values passed with -var-file can belong
    # to a different profile than the state this directory is initialized against. Both sides fall
    # back to "" when they cannot be determined (never initialized, or no .tfbackend for this
    # profile) — in both of those cases terraform fails on its own before touching anything.
    #
    # It compares BUCKETS only, so it cannot separate two environments that share a bucket and
    # differ by prefix (daly vs daly-staging). scripts/lib/env.sh is what makes those two safe, by
    # deriving the tfvars and the .tfbackend from one DEPLOY_ENV.
    precondition {
      condition     = !var.require_tfvars || local.initialized_backend_bucket == "" || local.expected_backend_bucket == "" || local.initialized_backend_bucket == local.expected_backend_bucket
      error_message = <<-EOT
        config profile does not match the initialized terraform state. config_profile = "${var.config_profile}" expects state bucket "${local.expected_backend_bucket}", but this directory was initialized against "${local.initialized_backend_bucket}".
        Applying would write one profile's project, region, domains and IAM into the other profile's state. The values all look plausible, so the plan will not look wrong.
        Fix: re-run through scripts/deploy.sh with the right DEPLOY_ENV, which selects the tfvars and the backend together. To re-init by hand: terraform init -backend-config=<env>.tfbackend -reconfigure, passing the matching -var-file=terraform.tfvars.<env>. Do NOT copy one into a bare terraform.tfvars — scripts/lib/env.sh refuses to run while that file exists beside the per-environment ones.
      EOT
    }
  }
}
