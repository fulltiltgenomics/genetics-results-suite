resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  repository_id = "genetics-results${local.name_suffix}"
  format        = "DOCKER"
  description   = "Docker images for genetics-results-suite"
}
