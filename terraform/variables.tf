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

variable "enable_log_sinks" {
  description = "Whether to create BigQuery log sinks (e.g. chat-backend logs to genetics_chat_logs)"
  type        = bool
  default     = false
}
