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
  description = "Email domain allowed for OAuth2 login (e.g. broadinstitute.org)"
  type        = string
  default     = "broadinstitute.org"
}

variable "snapshot_retention_days" {
  description = "Number of days to retain chat-data disk snapshots"
  type        = number
  default     = 14
}
