#!/bin/bash
set -euo pipefail

# turn the browser's "is being updated" notice on or off for one deployment
# usage: ui-notice.sh [--context <kubectl-context>] on|off|status
#
# The notice is the `ui-notice` ConfigMap, which k8s/deployments/frontend.yaml mounts into the
# served tree as /notice/notice.json; open tabs poll that file. `off` writes
# {"updating": false} rather than deleting the ConfigMap: a tab keeps its last answer while
# the file is unreadable, so a deleted file would leave the notice up until a reload.
#
# Not instant: the kubelet refreshes a mounted ConfigMap on its sync period and a tab polls
# once a minute, so allow a couple of minutes each way.

usage() {
  echo "Usage: ui-notice.sh [--context <kubectl-context>] on|off|status" >&2
}

# a flag rather than an environment variable, for the reason rollout.sh gives
OVERRIDE_CONTEXT=""
_override_given=0
_positional=()
while [ $# -gt 0 ]; do
  case "$1" in
    --context)
      [ $# -ge 2 ] || { echo "ERROR: --context requires a kubectl context name." >&2; usage; exit 1; }
      OVERRIDE_CONTEXT="$2"; _override_given=1; shift 2 ;;
    --context=*)
      OVERRIDE_CONTEXT="${1#--context=}"; _override_given=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    -*)
      echo "ERROR: unknown option: $1" >&2; usage; exit 1 ;;
    *)
      _positional+=("$1"); shift ;;
  esac
done
if [ "${_override_given}" = "1" ] && [ -z "${OVERRIDE_CONTEXT}" ]; then
  echo "ERROR: --context was given an empty value; it must name the cluster you mean." >&2
  usage; exit 1
fi
if [ "${#_positional[@]}" != "1" ]; then
  usage; exit 1
fi
ACTION="${_positional[0]}"
case "${ACTION}" in
  on) UPDATING=true ;;
  off) UPDATING=false ;;
  status) UPDATING="" ;;
  *) echo "ERROR: unknown action: ${ACTION}" >&2; usage; exit 1 ;;
esac

NAMESPACE="${NAMESPACE:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env

require_kube_context \
  "ui-notice.sh" \
  "notice change" \
  "Changing the notice here" \
  "Updating Services" \
  "    That is one deployment's users being told about another deployment's update.
    Proceeding, because you named the target; stop here if you did not mean this."

if [ "${ACTION}" = "status" ]; then
  kubectl --context "${ACTING_CONTEXT}" get configmap ui-notice -n "${NAMESPACE}" \
    -o 'jsonpath={.data.notice\.json}{"\n"}'
  exit 0
fi

kubectl create configmap ui-notice -n "${NAMESPACE}" \
  --from-literal=notice.json="{\"updating\": ${UPDATING}}" \
  --dry-run=client -o yaml \
  | kubectl --context "${ACTING_CONTEXT}" apply -f -
echo "Update notice ${ACTION} on ${ACTING_CONTEXT}; open tabs follow within a couple of minutes."
