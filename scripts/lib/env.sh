# shellcheck shell=bash
#
# Resolve WHICH deployment this invocation targets, and load that deployment's config.
# Sourced by deploy.sh, create-secrets.sh, build.sh and build-all.sh — every entry point that
# could otherwise talk to the wrong cluster or push to the wrong registry.
#
#   DEPLOY_ENV=<name>   selects terraform/terraform.tfvars.<name>, terraform/<name>.tfbackend
#                       and .env.<name>. Known names: daly, daly-staging, finngen.
#   DEPLOY_ENV unset    legacy single-deployment mode: terraform/terraform.tfvars (auto-loaded
#                       by terraform) with the backend derived from its config_profile. Kept so
#                       instances that manage exactly one deployment work unchanged.
#
# Exports: DEPLOY_ENV, TFVARS, BACKEND_FILE, ENV_FILE, TF_VAR_FILE_ARGS (array), and the
# tfvar() helper. Callers source their own .env via load_deploy_env.

_ENV_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "${_ENV_SH_DIR}/../.." && pwd)}"

# read a string variable out of the resolved tfvars file (same grep/sed shape the scripts
# used inline before; returns empty when the file or the key is missing)
#
# the trailing `|| true` is load-bearing under `set -euo pipefail`: a missing file or a
# missing key makes grep exit 1, pipefail propagates it past sed, and every caller here
# reads the result in an assignment — so the script died with no output before its own
# fallback or error message was reached (genetics-results-suite-1xp). Callers that need to
# distinguish "absent" from "empty" must test the value, not the status.
tfvar() {
  grep -E "^[[:space:]]*$1[[:space:]]*=" "${TFVARS}" 2>/dev/null \
    | head -1 | sed 's/.*=[[:space:]]*"\(.*\)".*/\1/' || true
}

resolve_deploy_env() {
  DEPLOY_ENV="${DEPLOY_ENV:-}"
  TF_VAR_FILE_ARGS=()

  if [ -n "${DEPLOY_ENV}" ]; then
    TFVARS="${ROOT_DIR}/terraform/terraform.tfvars.${DEPLOY_ENV}"
    BACKEND_FILE="${ROOT_DIR}/terraform/${DEPLOY_ENV}.tfbackend"
    ENV_FILE="${ROOT_DIR}/.env.${DEPLOY_ENV}"

    if [ ! -f "${TFVARS}" ]; then
      echo "ERROR: no tfvars for DEPLOY_ENV=${DEPLOY_ENV} (expected ${TFVARS})" >&2
      _list_known_envs
      return 1
    fi
    if [ ! -f "${BACKEND_FILE}" ]; then
      echo "ERROR: no state backend for DEPLOY_ENV=${DEPLOY_ENV} (expected ${BACKEND_FILE})" >&2
      return 1
    fi
    # terraform auto-loads a bare terraform.tfvars on top of -var-file. Any variable the
    # per-environment file omits would then silently come from that file — i.e. from another
    # deployment. Refuse rather than deploy a half-merged config.
    if [ -f "${ROOT_DIR}/terraform/terraform.tfvars" ]; then
      echo "ERROR: terraform/terraform.tfvars exists alongside per-environment tfvars files." >&2
      echo "       Terraform auto-loads it, so its values would leak into DEPLOY_ENV=${DEPLOY_ENV}." >&2
      echo "       Rename it to terraform/terraform.tfvars.<env> (e.g. .daly) and re-run." >&2
      return 1
    fi
    TF_VAR_FILE_ARGS=(-var-file="${TFVARS}")
  else
    TFVARS="${ROOT_DIR}/terraform/terraform.tfvars"
    ENV_FILE="${ROOT_DIR}/.env"
    if [ ! -f "${TFVARS}" ]; then
      echo "ERROR: terraform/terraform.tfvars not found and DEPLOY_ENV is not set." >&2
      _list_known_envs
      return 1
    fi
    local profile
    profile="$(tfvar config_profile)"
    BACKEND_FILE="${ROOT_DIR}/terraform/${profile}.tfbackend"
    if [ ! -f "${BACKEND_FILE}" ]; then
      echo "ERROR: backend config not found for config_profile=${profile}: ${BACKEND_FILE}" >&2
      return 1
    fi
  fi
  export DEPLOY_ENV TFVARS BACKEND_FILE ENV_FILE
}

_list_known_envs() {
  local found=""
  for f in "${ROOT_DIR}"/terraform/terraform.tfvars.*; do
    case "$f" in *.example|*'*') continue ;; esac
    found="${found} ${f##*terraform.tfvars.}"
  done
  [ -n "${found}" ] && echo "       Available DEPLOY_ENV values:${found}" >&2
  return 0
}

# source the deployment's secrets/knobs. Deliberately does NOT fall back to a bare .env when
# DEPLOY_ENV is set: falling back would push one deployment's secrets into another's cluster.
load_deploy_env() {
  if [ -f "${ENV_FILE}" ]; then
    set -a; . "${ENV_FILE}"; set +a
  elif [ -n "${DEPLOY_ENV}" ]; then
    echo "WARN: ${ENV_FILE} not found — deploy-time secrets and knobs for ${DEPLOY_ENV} are unset" >&2
  fi
}

# Registry for this deployment, derived from the tfvars so a forgotten REGISTRY export cannot
# push staging images over production tags. Mirrors terraform's `registry` output.
default_registry() {
  local project region suffix
  project="$(tfvar project_id)"
  region="$(tfvar region)"
  suffix="$(tfvar resource_suffix)"
  [ -n "${project}" ] && [ -n "${region}" ] || return 1
  echo "${region}-docker.pkg.dev/${project}/genetics-results${suffix}"
}

# Resolve REGISTRY for this deployment, given the derived value ($1, default: from tfvars).
#
# An inherited REGISTRY export is a genuine hazard once a project holds more than one
# deployment: README tells you to export it, shells keep it, and a stale value silently pushes
# one deployment's images over another's :latest tags — which the next restart then pulls.
# So when DEPLOY_ENV names an environment, an explicit REGISTRY must agree with it.
# REGISTRY_FORCE=1 overrides for the rare deliberate case (a scratch registry).
resolve_registry() {
  local derived="${1:-}"
  [ -n "${derived}" ] || derived="$(default_registry || true)"

  if [ -n "${REGISTRY:-}" ] && [ -n "${DEPLOY_ENV}" ] && [ -n "${derived}" ] \
     && [ "${REGISTRY}" != "${derived}" ] && [ "${REGISTRY_FORCE:-}" != "1" ]; then
    echo "ERROR: REGISTRY does not match DEPLOY_ENV=${DEPLOY_ENV}." >&2
    echo "         REGISTRY  = ${REGISTRY}" >&2
    echo "         expected  = ${derived}" >&2
    echo "       Usually a stale export from your shell profile. Pushing or deploying with it" >&2
    echo "       would cross deployments. Run 'unset REGISTRY' and retry, or set REGISTRY_FORCE=1" >&2
    echo "       if the mismatch is deliberate." >&2
    return 1
  fi

  REGISTRY="${REGISTRY:-${derived}}"
  if [ -z "${REGISTRY}" ]; then
    echo "ERROR: REGISTRY must be set (e.g. \$GCP_REGION-docker.pkg.dev/\$GCP_PROJECT/genetics-results)" >&2
    return 1
  fi
  export REGISTRY
}
