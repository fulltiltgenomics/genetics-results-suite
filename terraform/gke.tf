resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.zone

  # use a separately managed node pool
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  network    = google_compute_network.gke.id
  subnetwork = google_compute_subnetwork.gke.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # UNCONDITIONAL on purpose, and it must stay that way. The sandbox pool below runs in
  # GKE_METADATA mode, which the GKE API rejects unless the cluster has a workload_pool —
  # and it rejects it AT APPLY, not at plan. Gating this on var.manage_iam (as it was)
  # meant a manage_iam=false deployment planned cleanly and then failed at apply, whose
  # cheap wrong fix is to re-gate the sandbox pool's metadata mode and reopen the raw
  # metadata server to untrusted code. Enabling the workload pool is an in-place cluster
  # update: it does not recreate the cluster and does not change any existing pool's
  # metadata mode (the primary pool below keeps its own manage_iam-gated setting).
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  datapath_provider = "ADVANCED_DATAPATH"

  addons_config {
    http_load_balancing {
      disabled = false
    }
  }

  # release channel for automatic upgrades
  release_channel {
    channel = "REGULAR"
  }

  # Google Managed Prometheus for metrics collection
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "POD", "DEPLOYMENT", "DAEMONSET", "STATEFULSET", "HPA"]
    managed_prometheus {
      enabled = true
    }
  }
}

resource "google_container_node_pool" "primary_nodes" {
  name     = "${var.cluster_name}-pool"
  location = var.zone
  cluster  = google_container_cluster.primary.name

  autoscaling {
    min_node_count = var.min_node_count
    max_node_count = var.max_node_count
  }

  node_config {
    machine_type    = var.machine_type
    service_account = var.manage_iam ? google_service_account.genetics_suite[0].email : (
      var.node_service_account != "" ? var.node_service_account : null
    )

    dynamic "workload_metadata_config" {
      for_each = var.manage_iam ? [1] : []
      content {
        mode = "GKE_METADATA"
      }
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    labels = {
      env = var.environment
    }
  }
}

# Dedicated gVisor (GKE Sandbox) pool for the code-execution sandbox. Separate from the
# primary pool on isolation grounds, not capacity grounds: GKE Sandbox can only be enabled
# per-pool, and the point is that chat-backend can never be co-scheduled with untrusted
# LLM-authored code. It contributes nothing to the primary pool's rollout surge budget.
#
# Pinned at one node (min == max == 1). The primary pool autoscales 1-3, which is fine for
# request-serving pods; here a scale-down would kill an in-flight script. Accepted cost:
# one permanently-running node.
#
# Sizing and the reasoning behind every field below are in docs/project-spec.md
# ("Node pool sizing") and docs/code-execution-security.md ("gVisor (GKE Sandbox)").
#
# THE POD IS THE OTHER HALF OF THE CONTROL, AND THIS POOL DOES NOT ESTABLISH IT.
# GKE_METADATA below does not deny credentials — it swaps NODE identity for KSA identity.
# Whether that is worth anything depends entirely on the KSA the sandbox pod runs as, and
# nothing in terraform enforces it. The repo's house style is the failure mode: all 8
# deployments in k8s/deployments/*.yaml use `serviceAccountName: genetics-suite`, and
# terraform/iam.tf binds exactly that KSA to a GSA holding bigquery.dataViewer,
# bigquery.jobUser, storage.objectViewer and logging.viewer. If k8s/deployments/sandbox.yaml
# copies that line, GKE_METADATA buys nothing at all. The sandbox pod MUST use a DEDICATED
# KSA with no iam.gke.io/gcp-service-account annotation and no google_service_account_iam_member
# binding — explicitly NOT genetics-suite, and not the namespace default either.
#
# THREE RULES FOR var.sandbox_pool_enabled, which gate-keeps this resource:
#  1. The flag gates whether the RESOURCE EXISTS. It must NEVER appear inside node_config —
#     no dynamic block, no ternary, no count-derived value on any field of the pool spec.
#     workload_metadata_config { mode = "GKE_METADATA" } stays a bare literal, so the only
#     two representable states are "no pool" and "a pool in GKE_METADATA mode". A third
#     state (a pool with a weakened metadata mode) must not be expressible.
#  2. The cluster's workload_identity_config above STAYS UNCONDITIONAL — do not tie it to
#     this flag. Tied, enabling the pool becomes a two-step apply, and whoever hits the
#     resulting apply-time failure reaches for exactly the cheap wrong fix (re-gating the
#     metadata mode) that rule 1 forbids.
#  3. The service-account requirement lives on this resource as a precondition, so the
#     escape hatch is "don't create the pool" — never "create it with a weaker SA".
resource "google_container_node_pool" "sandbox_nodes" {
  count = var.sandbox_pool_enabled ? 1 : 0

  name     = "${var.cluster_name}-sandbox-pool"
  location = var.zone
  cluster  = google_container_cluster.primary.name

  autoscaling {
    min_node_count = 1
    max_node_count = 1
  }

  node_config {
    # ForceNew: changing this DESTROYS and recreates the pool. A machine-type change has to
    # be a new pool plus cordon/drain, not an edit here.
    # UNVERIFIED (genetics-results-suite-5r2): that e2-standard-2 satisfies GKE Sandbox's
    # machine-type requirements on this cluster's channel/version, and that the runsc
    # sentry's own footprint fits alongside the pod's 3Gi limit. Both are enforced at pool
    # creation / runtime, not at plan, so only a real apply in a non-production project
    # settles them.
    machine_type = var.sandbox_machine_type
    image_type   = "COS_CONTAINERD" # gVisor requires containerd

    # hashicorp/google v7 names this argument "type"; the older google-beta spelling
    # "sandbox_type" (as written in docs/code-execution-security.md's draft spec) is
    # rejected by `terraform validate`.
    # The GKE REST enum is "GVISOR" (uppercase) and the provider does no normalization —
    # there is no lowercase "gvisor" string in the provider binary. Whether the API accepts
    # the lowercase form is UNVERIFIED and is a 30-second check at the first real apply. If
    # it is rejected, the fix is type = "GVISOR", and docs/project-spec.md ("The sandbox
    # pool") and docs/code-execution-security.md both quote this value and need updating too.
    sandbox_config {
      type = "gvisor"
    }

    # THE central isolation control, and unconditional on purpose. Unset means GCE_METADATA,
    # which exposes 169.254.169.254 to every pod on the node: one HTTP GET of
    # /computeMetadata/v1/instance/service-accounts/default/token then yields a node-level
    # access token, and Workload Identity is irrelevant in that mode because the identity
    # handed out is the node's, not the KSA's. Do NOT gate this on var.manage_iam to escape
    # an apply-time failure — that reintroduces the hole verbatim in exactly the deployment
    # (platform-team-owned IAM) where it is most dangerous. The fix for that failure is the
    # unconditional workload_identity_config on the cluster above.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Mandatory whenever this pool exists — enforced by the preconditions at the bottom of
    # this resource, not by variable validation, so a deployment that leaves the pool off is
    # not forced to invent a value. Empty here would mean the Compute Engine default SA
    # (typically roles/editor), and genetics-suite would hand the sandbox's node the
    # BigQuery/GCS roles the sandbox must never reach. Those checks are FORMAT and IDENTITY
    # checks only: terraform neither creates this SA nor reads back the roles bound to it.
    service_account = var.sandbox_node_service_account

    # Explicit rather than the provider default. NOT load-bearing for the pod-facing
    # guarantee: in GKE_METADATA mode pod tokens are minted through the IAM Credentials API
    # for the WI-bound GSA and are not bounded by node scopes at all. This defends two other
    # cases only — a GCE_METADATA misconfiguration, and a gVisor escape that reaches the
    # node SA's own token. Nothing in the security design may treat it as the control.
    # devstorage.read_only is REQUIRED for Artifact Registry image pulls;
    # roles/artifactregistry.reader on the SA is not sufficient on its own.
    oauth_scopes = [
      "https://www.googleapis.com/auth/devstorage.read_only",
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/monitoring.write",
      "https://www.googleapis.com/auth/service.management.readonly",
      "https://www.googleapis.com/auth/servicecontrol",
      "https://www.googleapis.com/auth/trace.append",
    ]

    # Per-pod pid ceiling is a kubelet setting, not a pod-spec field — one more reason the
    # sandbox needs its own pool. docs/code-execution-security.md originally specified 256;
    # GKE's documented range for podPidsLimit starts at 1024, so 256 would be rejected at
    # pool creation. NOT established by any tool here: the provider schema is a bare optional
    # number with no range check, so `terraform validate` passes on 256 as readily as on
    # 1024. UNCONFIRMED against this cluster (genetics-results-suite-5r2). Fork-bomb
    # containment does not rest on this number anyway: the supervisor enforces a child pid
    # budget far below it (4h6.7/4h6.41). This is the outer backstop.
    kubelet_config {
      pod_pids_limit = 1024
    }

    labels = {
      env      = "production"
      workload = "sandbox"
    }
  }

  # No taint declared here: GKE taints gVisor nodes sandbox.gke.io/runtime=gvisor:NoSchedule
  # automatically, and the sandbox Deployment carries the matching toleration plus
  # runtimeClassName: gvisor. Adding it by hand would drift from what GKE manages.

  # Preconditions rather than variable validation: they fire only when the pool is actually
  # being created (rule 3 above). Requires terraform >= 1.14, which main.tf already pins.
  lifecycle {
    precondition {
      condition     = can(regex("^[a-z0-9-]+@${var.project_id}\\.iam\\.gserviceaccount\\.com$", lower(var.sandbox_node_service_account)))
      error_message = "sandbox_node_service_account must be a service account in this project: <name>@${var.project_id}.iam.gserviceaccount.com. It is required whenever sandbox_pool_enabled = true, in every mode including manage_iam = false. The variable's default of \"\" exists only to stop terraform prompting interactively; it is not a usable value. If you do not want this pool, set sandbox_pool_enabled = false — do not weaken the SA."
    }

    precondition {
      condition     = !can(regex("(^|[^a-z0-9-])genetics-suite", lower(var.sandbox_node_service_account)))
      error_message = "sandbox_node_service_account must not be the genetics-suite GSA: it holds roles/bigquery.dataViewer, bigquery.jobUser, storage.objectViewer and logging.viewer, which the sandbox node must never be able to reach."
    }

    # The primary pool's SA is the one that matters most under manage_iam = false (the live
    # mode): that pool grants oauth_scopes = ["cloud-platform"], so reusing its SA here puts
    # the suite's entire credential on the node running untrusted code.
    precondition {
      condition     = var.node_service_account == "" || lower(var.sandbox_node_service_account) != lower(var.node_service_account)
      error_message = "sandbox_node_service_account must not equal node_service_account. That is the primary pool's SA, whose pool grants the cloud-platform OAuth scope; sharing it puts the suite's entire credential on the node running untrusted code."
    }
  }
}
