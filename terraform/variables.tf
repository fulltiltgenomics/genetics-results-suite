variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for the GKE cluster"
  default     = "europe-west1"
}

variable "zone" {
  description = "GCP zone for the GKE node pool"
  default     = "europe-west1-b"
}

variable "cluster_name" {
  description = "Name of the GKE cluster"
  type        = string
  default     = "finngenie"
}

variable "kube_context" {
  description = <<-EOT
    The kubectl context name for THIS deployment's cluster, e.g.
    "gke_daly-finngenie_us-central1-a_finngenie-staging". Consumed by the context guard in
    scripts/lib/env.sh (require_kube_context), which both scripts/rollout.sh and
    scripts/create-secrets.sh call to refuse to mutate a cluster the selected tfvars does not
    name; NO resource reads it. It is stated rather than derived from project_id/zone/cluster_name on
    purpose: every text-matching derivation over HCL that was tried failed towards terraform's
    defaults, and those name a PRODUCTION cluster in both projects. Declared here only so
    terraform does not warn "Value for undeclared variable" on every plan.
  EOT
  type        = string
  default     = ""
}

variable "namespace" {
  description = "Kubernetes namespace"
  default     = "genetics"
}

variable "static_ip_name" {
  description = "Name of the reserved global static IP address"
  type        = string
  default     = "finngenie-ip"
}

variable "domains" {
  description = "Domain names for the deployment"
  type        = list(string)
}

variable "machine_type" {
  description = "Machine type for GKE nodes"
  default     = "e2-standard-4"
}

variable "node_service_account" {
  description = "GCP service account email for GKE nodes. Used when manage_iam is false."
  type        = string
  default     = ""
}

variable "manage_iam" {
  description = "Create a dedicated Workload Identity SA with IAM bindings (requires project-level IAM admin). When false, nodes use node_service_account or the default compute SA."
  type        = bool
  default     = true
}

variable "sandbox_pool_enabled" {
  description = "Create the dedicated gVisor (GKE Sandbox) node pool. Default false: scripts/deploy.sh runs `terraform apply -auto-approve` on every full deploy, so a pool gated on nothing would be created by an ordinary deploy that nobody opted into. This flag gates whether the RESOURCE EXISTS and must never appear inside its node_config — see the comment on google_container_node_pool.sandbox_nodes."
  type        = bool
  default     = false
}

variable "sandbox_machine_type" {
  description = "Machine type for the dedicated gVisor sandbox node pool. ForceNew: changing it destroys and recreates the pool, so migrate via a new pool plus cordon/drain instead. Whether e2-standard-2 meets GKE Sandbox's requirements on this cluster is unverified (genetics-results-suite-5r2)."
  type        = string
  default     = "e2-standard-2"
}

variable "sandbox_node_service_account" {
  description = "GCP service account email for the gVisor sandbox node pool. Required whenever sandbox_pool_enabled is true; the checks live as lifecycle preconditions on the pool resource, not here, so a deployment that does not create the pool is not forced to invent a value. The default is \"\" purely so terraform does not prompt interactively — it is not a usable value: empty would mean the Compute Engine default SA (typically roles/editor), and reusing genetics-suite would put the suite's BigQuery/GCS roles on the node running untrusted code. Create a dedicated GSA holding only roles/logging.logWriter, roles/monitoring.metricWriter, roles/monitoring.viewer, roles/stackdriver.resourceMetadata.writer and roles/artifactregistry.reader. Terraform neither creates this SA nor checks what roles it holds."
  type        = string
  default     = ""
}

variable "min_node_count" {
  description = "Minimum number of nodes per zone"
  default     = 1
}

variable "max_node_count" {
  description = "Maximum number of nodes per zone"
  default     = 3
}

variable "config_profile" {
  description = "Data profile for results-api (daly or finngen)"
  type        = string
  default     = "daly"

  validation {
    condition     = contains(["daly", "finngen"], var.config_profile)
    error_message = "config_profile must be 'daly' or 'finngen'."
  }
}

variable "oauth_email_domain" {
  description = "Email domain(s) allowed for OAuth2 login. Comma-separated for several, e.g. \"broadinstitute.org,finngen.fi\"."
  type        = string
  default     = "broadinstitute.org"
}

variable "oauth_allowed_emails" {
  description = "Specific email addresses allowed in addition to oauth_email_domain (comma-separated, e.g. for Apple users on me.com/icloud.com/privaterelay.appleid.com). Empty = none."
  type        = string
  default     = ""
}

variable "app_name" {
  description = "Product/brand name shown in the UI and the assistant persona"
  type        = string
  default     = "FinnGenie"
}

variable "redirect_from_host" {
  description = "Legacy hostname to 301-redirect away (empty = no redirect). Used to migrate an old domain to a new one; the auth-gateway serves the redirect."
  type        = string
  default     = ""
}

variable "redirect_to_host" {
  description = "Destination hostname for the legacy redirect (path + query preserved). Required when redirect_from_host is set; must also be in domains so its cert is valid."
  type        = string
  default     = ""
}

variable "snapshot_retention_days" {
  description = "Number of days to retain chat-data disk snapshots"
  type        = number
  default     = 14
}

variable "keycloak_backup_bucket_name" {
  description = "GCS bucket for Keycloak Postgres pg_dump backups. Empty = derive as <project_id>-keycloak-backups."
  type        = string
  default     = ""
}

variable "keycloak_backup_retention_days" {
  description = "Days to retain Keycloak Postgres backups in GCS before lifecycle deletion"
  type        = number
  default     = 14
}

variable "require_tfvars" {
  description = "Refuse to plan/apply unless terraform/terraform.tfvars is present next to the config. Guards against a worktree or fresh clone silently using variable defaults. Set false only when supplying values another way (-var-file elsewhere, TF_VAR_*, CI)."
  type        = bool
  default     = true
}

variable "enable_log_sinks" {
  description = "Whether to create BigQuery log sinks (e.g. chat-backend logs to genetics_chat_logs)"
  type        = bool
  default     = false
}

variable "resource_suffix" {
  description = <<-EOT
    Suffix appended to PROJECT-scoped resource names (Artifact Registry repo, workload-identity
    service account, snapshot policy, Keycloak backup bucket, log sinks and their BigQuery
    datasets). Empty for the original deployment in a project; set it (e.g. "-staging") for any
    additional deployment sharing the same GCP project, whose names would otherwise collide.
    Cluster-scoped names (network, subnet, firewall, node pool) already derive from cluster_name.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.resource_suffix == "" || can(regex("^-[a-z0-9-]+$", var.resource_suffix))
    error_message = "resource_suffix must be empty or start with '-' and contain only lowercase letters, digits and hyphens."
  }
}

variable "environment" {
  description = "Environment label applied to GKE nodes (production, staging, …). Cosmetic; does not gate any behaviour."
  type        = string
  default     = "production"
}

variable "subnet_cidr" {
  description = "Primary IPv4 range of the GKE subnet. Must not overlap another deployment's if the VPCs are ever peered."
  type        = string
  default     = "10.0.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary range for GKE pods"
  type        = string
  default     = "10.16.0.0/14"
}

variable "services_cidr" {
  description = "Secondary range for GKE services"
  type        = string
  default     = "10.20.0.0/20"
}
