resource "kubernetes_namespace_v1" "genetics" {
  metadata {
    name = var.namespace
  }

  depends_on = [google_container_node_pool.primary_nodes]
}

resource "kubernetes_service_account_v1" "genetics_suite" {
  metadata {
    name      = "genetics-suite"
    namespace = kubernetes_namespace_v1.genetics.metadata[0].name

    annotations = var.manage_iam ? {
      "iam.gke.io/gcp-service-account" = google_service_account.genetics_suite[0].email
    } : {}
  }
}
