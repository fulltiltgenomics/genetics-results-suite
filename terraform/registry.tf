resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  repository_id = "genetics-results"
  format        = "DOCKER"
  description   = "Docker images for genetics-results-suite"
}
