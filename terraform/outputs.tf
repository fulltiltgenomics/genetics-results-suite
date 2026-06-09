output "project_id" {
  description = "GCP project ID"
  value       = var.project_id
}

output "region" {
  description = "GCP region"
  value       = var.region
}

output "zone" {
  description = "GCP zone for the GKE cluster"
  value       = var.zone
}

output "cluster_endpoint" {
  description = "GKE cluster endpoint"
  value       = google_container_cluster.primary.endpoint
  sensitive   = true
}

output "cluster_name" {
  description = "GKE cluster name"
  value       = google_container_cluster.primary.name
}

output "service_account_email" {
  description = "GCP service account for workload identity"
  value       = var.manage_iam ? google_service_account.genetics_suite[0].email : null
}

output "domain" {
  description = "Primary domain name"
  value       = var.domains[0]
}

output "domains" {
  description = "All domain names (comma-separated)"
  value       = join(",", var.domains)
}

output "static_ip_name" {
  description = "Name of the reserved global static IP"
  value       = var.static_ip_name
}

output "static_ip" {
  description = "Reserved static IP address"
  value       = data.google_compute_global_address.static_ip.address
}

output "kubectl_command" {
  description = "Command to configure kubectl"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.primary.name} --zone ${var.zone} --project ${var.project_id}"
}

output "snapshot_policy_name" {
  description = "Name of the snapshot schedule policy for chat-data disk"
  value       = google_compute_resource_policy.chat_data_snapshots.name
}

output "oauth_email_domain" {
  description = "Email domain(s) allowed for OAuth2 login (comma-separated)"
  value       = var.oauth_email_domain
}

output "oauth_allowed_emails" {
  description = "Specific email addresses allowed in addition to the domains (comma-separated)"
  value       = var.oauth_allowed_emails
}

output "config_profile" {
  description = "Data profile for results-api"
  value       = var.config_profile
}

output "app_name" {
  description = "Product/brand name shown in the UI and the assistant persona"
  value       = var.app_name
}

output "redirect_from_host" {
  description = "Legacy hostname that 301-redirects to redirect_to_host (empty = none)"
  value       = var.redirect_from_host
}

output "redirect_to_host" {
  description = "Destination hostname for the legacy redirect"
  value       = var.redirect_to_host
}

output "registry" {
  description = "Artifact Registry URL for docker images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker.repository_id}"
}
