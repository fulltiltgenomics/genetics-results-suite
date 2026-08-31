# shellcheck shell=bash
#
# Resolve WHICH deployment this invocation targets, and load that deployment's config.
# Sourced by deploy.sh, create-secrets.sh, rollout.sh, build.sh and build-all.sh — every entry
# point that could otherwise talk to the wrong cluster or push to the wrong registry.
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
# fallback or error message was reached. Callers that need to
# distinguish "absent" from "empty" must test the value, not the status.
#
# KNOWN WEAK, recorded rather than fixed here: `^[[:space:]]*<key>` plus `head -1` takes the
# first LINE-ANCHORED match, which is not the top-level HCL attribute. It reads a key nested
# inside an indented block, and it silently picks the first of a duplicated key rather than
# refusing. Terraform would reject the duplicate, but none of these callers run terraform, so
# nothing else catches it. read_kube_context below — the cluster context guard, whose wrong
# answers name a production cluster — therefore has its own column-0-anchored,
# refuse-on-ambiguity reader in this same file and does NOT call this one. Deliberately left as
# it is: hardening tfvar() changes REGISTRY and config_profile resolution for deploy.sh,
# build.sh, build-all.sh and create-secrets.sh at once — a wider blast radius than the guard's
# own fix had — and what it still misdirects is the REGISTRY rather than the cluster. File it separately if it is worth
# doing at all.
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
    # *~ : editor backups would otherwise be advertised as selectable environments
    case "$f" in *.example|*'~'|*'*') continue ;; esac
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

# ---------------------------------------------------------------------------------------------
# THE CLUSTER CONTEXT GUARD (lifted here from rollout.sh so
# create-secrets.sh gets the same guard rather than a second copy of it that can drift).
#
# EVIDENCE: the deployment STATES its cluster, in one line of its own tfvars:
#
#     kube_context = "gke_daly-finngenie_us-central1-a_finngenie-staging"
#
# There is no derivation and no fallback. The guard once rebuilt
# `gke_<project_id>_<zone>_<cluster_name>` from three tfvars keys, defaulting the two terraform
# gives defaults to — so a key that read as ABSENT produced `zone = europe-west1-b` with
# `cluster_name = finngenie`, and those name a PRODUCTION cluster in BOTH projects. Every way that
# reader could fail pointed at production.
#
# Nor does the reader below MODEL HCL, and that is the second lesson rather than a style choice.
# Four rounds of blind validation broke a reader that tried: each round found a new way for its
# `/* */` and heredoc state machine to mis-classify a LEGAL file, in both directions — a comment
# body read as live code, a `/*` inside a string refusing a good key. The analysis was the defect,
# so there is none. The rule needs no understanding of the language at all: the token
# `kube_context` must appear EXACTLY ONCE in the whole file — comments and strings included — and
# that one line must be a column-0 assignment of a plain quoted string. Everything else refuses.
# One stateless precondition sits in front of both: a file containing `/*` anywhere is refused
# outright, because a block comment is the only form that can present a commented-out key AS the
# one legal line. See read_kube_context.
#
# That is deliberately STRICTER than HCL. A file that mentions kube_context in a comment AND sets
# it is refused; that is one obvious edit, and the message names it. In exchange no construct —
# heredoc, block comment, string, CRLF, nesting — can make the reader return a value the operator
# did not write on that one line, because nothing is being interpreted. A refusal costs one line
# in a tfvars; a wrong answer costs a production cluster. `variable "kube_context"` is declared in
# terraform/variables.tf so terraform does not warn about it; no resource consumes it.
#
# tfvar() above is deliberately not used and deliberately not changed: it is
# `grep -E "^[[:space:]]*<key>[[:space:]]*=" | head -1`, which matches an indented key and picks
# the first of a duplicate rather than refusing. It is shared with deploy.sh, build.sh,
# build-all.sh and create-secrets.sh, where it resolves REGISTRY and config_profile, so hardening
# it is a different change with a different blast radius. The weakness is recorded at the helper.
#
# The reader is not usable in a command substitution: the refusal reason has to survive into the
# message, and a subshell would drop it.
EXPECTED_CONTEXT=""
KUBE_CONTEXT_ERROR=""

# Sets EXPECTED_CONTEXT from the file's one and only kube_context, or returns 1 with
# KUBE_CONTEXT_ERROR. `\b` around the token is what keeps kube_context_old and old_kube_context
# from inflating the count: `_` is a word character, so neither has a boundary there.
read_kube_context() {
  local hits n hit lineno line re block
  [ -r "${TFVARS}" ] || { KUBE_CONTEXT_ERROR="${TFVARS} could not be read."; return 1; }
  # `/*` ANYWHERE refuses, before the count is even taken. `#` and `//` commented-out keys are
  # already handled: the token still counts and the line fails the column-0 form, so the guard
  # refuses. `/* */` was the one comment form that could smuggle a value past BOTH checks — a file
  # whose only kube_context sits inside a block comment presents one occurrence on a column-0 line
  # in the strict form, and is accepted. An earlier round called that harmless on the grounds that
  # it "never widens what is allowed"; that was wrong, and the error is worth stating. Without
  # reading the commented-out line the count would be ZERO and the guard would refuse outright, so
  # reading it converts "refuse always" into "act on the cluster that line names" — from nothing to
  # a production cluster. Commenting a cluster line out while switching targets is exactly what
  # operators do, so it is a realistic input rather than a contrived one. This is stateless on
  # purpose: no pairing, no nesting, no tracking of which construct a `/*` opens — that modelling
  # is what four rounds of validation broke. The cost is a rare false refusal (a `/*` inside a
  # string, or a genuine block comment elsewhere in the file), fixed by one edit the message names,
  # and it buys the removal of the last input that can hand this guard a cluster nobody meant.
  # It runs FIRST because the count is taken over text this guard has just said it cannot read: on
  # a file with both faults, "delete the other mentions" could send the operator to delete the live
  # key and keep the commented one.
  block="$(grep -nF -m1 -- '/*' "${TFVARS}")" || block=""
  if [ -n "${block}" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} line ${block%%:*} contains a /* block comment, and this guard refuses to reason about block comments at all rather than risk reading a commented-out cluster as the live one. Remove it or convert it; # and // comments are fine and need no change."
    return 1
  fi
  hits="$(grep -oE '\bkube_context\b' "${TFVARS}")" || hits=""
  n=0
  [ -z "${hits}" ] || n="$(printf '%s\n' "${hits}" | wc -l | tr -d '[:space:]')"
  if [ "${n}" = "0" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} does not mention kube_context anywhere."
    return 1
  fi
  if [ "${n}" != "1" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} mentions kube_context ${n} times; it must appear exactly once in the file, inside comments and strings included, because this guard deliberately does not parse HCL and will not guess which occurrence is the real one. Delete the other mentions."
    return 1
  fi
  hit="$(grep -nE '\bkube_context\b' "${TFVARS}")"
  lineno="${hit%%:*}"
  line="${hit#*:}"
  re='^kube_context[[:blank:]]*=[[:blank:]]*"([^"]*)"[[:blank:]]*(#.*)?$'
  if [[ ! "${line}" =~ $re ]]; then
    KUBE_CONTEXT_ERROR="${TFVARS} line ${lineno} is the only kube_context and it is not a column-0 assignment of a plain quoted string, the one form this guard reads: ${line}"
    return 1
  fi
  # `kube_context = ""` is a LEGAL assignment and parses fine, so without this it would return 0
  # with an empty EXPECTED_CONTEXT and an empty KUBE_CONTEXT_ERROR — the caller's refusal would
  # then print a blank reason line, and the --context path would end "nothing cross-checked that
  # against the deployment." with nothing after it. Empty names no cluster, so it refuses like any
  # other unusable value, with a reason to act on. (terraform/variables.tf defaults the variable to
  # "", so a copied declaration is a plausible way to arrive here.)
  if [ -z "${BASH_REMATCH[1]}" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} line ${lineno} sets kube_context to the empty string, which names no cluster. Fill in this deployment's own context name."
    return 1
  fi
  EXPECTED_CONTEXT="${BASH_REMATCH[1]}"
}

# GUARD: refuse when the cluster kubectl is pointed at is not the cluster DEPLOY_ENV names.
# Call it BEFORE the caller's first cluster-contacting command; it exits 1 rather than returning,
# because there is no safe way to continue past it.
#
# Sets CURRENT_CONTEXT (never empty on any path that returns) and FREEZES it into the readonly
# ACTING_CONTEXT, which is what callers pin on every kubectl call: `--context "${ACTING_CONTEXT}"`.
# That pinning is not belt-and-braces: the guard reads the shared kubeconfig, and a
# `kubectl config use-context` from another terminal rewrites it, so an unpinned kubectl would
# re-read it and act on whatever landed there between the check and the call. The freeze is what
# makes the pin carry THIS FUNCTION'S VERDICT rather than a variable anything later can rewrite —
# see the block at the end of this function for what was driven through the unfrozen version.
#
# Reads OVERRIDE_CONTEXT — the caller's per-invocation `--context` flag, "" when not given. It is
# a FLAG in both callers and never an environment variable: an `export` outlives the invocation it
# was typed for and re-authorises every later run from the same shell, which is how a staging
# image once reached a production cluster.
#
# deploy.sh does not call this because it OVERWRITES the context from terraform output before its
# first apply, so the acting cluster cannot disagree with the resolved state backend. The callers
# here deliberately do NOT copy that: silently retargeting the operator's shell is its own hazard
# in tools people run one service, or one secret write, at a time without re-reading their prompt.
# Refusing leaves the shell exactly as it was and puts the decision back on the human.
#
# The echoed context this replaced in both callers was not a guard. There are three deployments
# across two projects and two of them are production; BOTH production clusters are named
# `finngenie` so only the project tells them apart, one of those projects is called
# `phewas-development`, and the daly production context differs from staging's by a trailing
# `-staging` alone. A string printed one line above a mutation does not survive that.
#
# THREAT MODEL — STATED, because the code below implies one it cannot deliver. This guard bounds
# ACCIDENTS. It does not bound an ATTACK, and no version of it can.
#
# `.env.<env>` holds the operator's own API keys and is written by the same person who runs these
# scripts. It is gitignored because it is SECRET, not because it is HOSTILE. create-secrets.sh
# sources it with `set -a; . file; set +a` — arbitrary shell in this process — and sourcing
# arbitrary shell in your own process ends the argument: anything in that file can redefine any
# command, rewrite PATH, or replace the script's own functions. A check written INSIDE a script is
# worth nothing against a file that runs inside the same shell, exactly as with BASH_ENV below.
#
# So read every "refuses", "cannot" and "closed" in this file as: bounds a mistake an operator can
# realistically make. The mistakes are ranked, and the ranking is the point — a stray `PATH=` line
# in a deployment `.env` is genuinely plausible; a `KUBECONFIG=` line is possible; a `kubectl()`
# shell function is not something anyone writes by accident. The guard is built for the first two.
#
# HAZARD, recorded rather than fixed: `.env.<env>` IS ITSELF A HAZARD OF THE BASH_ENV CLASS, and
# it belongs in this list even though the ordering and the freeze both exist because of it. Those
# two close `.env` REWRITING the guard's inputs (`TFVARS=`, `OVERRIDE_CONTEXT=`) and its output
# (`CURRENT_CONTEXT=`). They do nothing about `.env` changing what the frozen verdict MEANS:
# a `kubectl()` function, a `PATH=` line or a `KUBECONFIG=` line each let the pin expand faithfully
# and then be reinterpreted, with every write landing on another cluster behind a green line that
# is truthful about what was checked. All three were driven. The difference from BASH_ENV is
# reachability, not mechanism: BASH_ENV needs someone in the operator's environment, whereas
# `.env.<env>` is an ordinary config file the operator edits on purpose. create-secrets.sh
# therefore RE-ASSERTS `kubectl config current-context` against ACTING_CONTEXT after
# load_deploy_env returns — an accident detector for the PATH and KUBECONFIG lines, useless against
# the function, and documented as such at the site. It is not here because rollout.sh never sources
# `.env` and must not gain a second kubectl call for a window it does not have.
#
# HAZARD, recorded rather than fixed: this compares context NAMES, and a name is not an endpoint.
# A kubeconfig entry named `..._finngenie-staging` whose `cluster.server` resolves to the
# production endpoint passes this guard and mutates production. An earlier revision of this comment
# dismissed that as a hand-edited kubeconfig, "not reachable as configured"; that was wrong, in the
# same way and for the same reason the ROOT_DIR dismissal below was wrong. KUBECONFIG is honoured
# from the environment by kubectl itself, so an INHERITED EXPORT — with no edit to any file, by
# hand or otherwise — selects a different kubeconfig altogether, in which the expected name may be
# bound to any server at all. It is exactly as reachable as ROOT_DIR. Closing it means comparing
# the resolved `cluster.server` against terraform's endpoint output, which costs state access; it
# is left open, and the accepting path below therefore PRINTS the kubeconfig it consulted next to
# the tfvars, so both of the inputs this guard trusts are on screen rather than assumed. That
# printed path covers the INHERITED EXPORT only — the value is in scope when the line prints. It
# does not cover a `KUBECONFIG=` line in `.env.<env>`, which takes effect after the line is
# printed; see the note at that echo, and the .env hazard above.
#
# ROOT_DIR IS THE SAME SHAPE: it is honoured from the environment (above), so an inherited export
# relocates the tfvars this guard reads — it re-points the guard's EVIDENCE rather than its code,
# leaving it green while describing another checkout's cluster. It was DRIVEN, not theorised, by
# The validation (an earlier revision of this comment called it "not
# reachable as configured", which was false). It is not guarded because ROOT_DIR is a legitimate
# knob for running these scripts against another checkout; instead the accepting paths below PRINT
# the resolved ${TFVARS} they read, so a guard whose evidence came from the wrong tree no longer
# looks identical to a correct run. Reading the printed path is the mitigation.
#
# BASH_ENV IS ANOTHER (no ordinal: the list above grew by one and an ordinal is a count that
# rots), recorded here only so the next reader does not have to re-derive it: bash
# sources $BASH_ENV before line 1 of a non-interactive script, so a `kubectl()` shell function
# defined there answers `kubectl config current-context` for this guard — and every pinned call
# after it — while the real binary is never run. Driven and silent. It is deliberately NOT checked:
# anyone who can set BASH_ENV in the operator's environment can rewrite these scripts outright, so
# a check inside the script it subverts buys nothing. The mitigation belongs to the environment.
#
# The six parameters exist so ONE implementation can speak in each caller's own terms — the
# wording is the guard's user interface, and a generic "this script" message is worse at the
# moment someone is one paste away from production. They are, in order:
#   1 tool     the script name, e.g. "rollout.sh"
#   2 action   noun for one off-target run, e.g. "rollout" / "secret write"; also uppercased
#              into the OFF-TARGET banner
#   3 gerund   how the mismatch message opens, e.g. "Rolling out here"
#   4 readme   the README section the messages point at
#   5 detail   the already-indented lines closing the OFF-TARGET banner
#   6 extra    one already-indented line of caller context for that banner (optional)
require_kube_context() {
  local tool="$1" action="$2" gerund="$3" readme="$4" detail="$5" extra="${6:-}"
  local action_uc
  action_uc="$(printf '%s' "${action}" | tr '[:lower:]' '[:upper:]')"

  CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
  read_kube_context || EXPECTED_CONTEXT=""

  if [ -n "${OVERRIDE_CONTEXT:-}" ]; then
    if [ "${OVERRIDE_CONTEXT}" != "${CURRENT_CONTEXT}" ]; then
      echo "ERROR: --context does not name the context kubectl is actually on." >&2
      echo "         --context       = ${OVERRIDE_CONTEXT}" >&2
      echo "         current context = ${CURRENT_CONTEXT:-<none>}" >&2
      echo "       The flag exists to make an off-target ${action} deliberate, so it has to spell out" >&2
      echo "       the cluster you are about to mutate. It is not a bypass switch." >&2
      exit 1
    fi
    if [ -z "${EXPECTED_CONTEXT}" ]; then
      echo "OVERRIDE: acting on ${CURRENT_CONTEXT} by explicit --context (env: ${DEPLOY_ENV:-default})."
      echo "  WARNING: nothing cross-checked that against the deployment. ${KUBE_CONTEXT_ERROR}"
    elif [ "${OVERRIDE_CONTEXT}" != "${EXPECTED_CONTEXT}" ]; then
      echo "*** OFF-TARGET ${action_uc}: the cluster and the deployment DISAGREE, and --context asked for it."
      echo "      acting on = ${CURRENT_CONTEXT}"
      echo "      expected  = ${EXPECTED_CONTEXT}  (DEPLOY_ENV=${DEPLOY_ENV:-<unset>}, kube_context in ${TFVARS})"
      [ -z "${extra}" ] || echo "${extra}"
      echo "${detail}"
    else
      echo "OVERRIDE: acting on ${CURRENT_CONTEXT} by explicit --context (env: ${DEPLOY_ENV:-default})."
      echo "  It is the context this deployment expects, so the flag changed nothing. (kube_context in ${TFVARS})"
    fi
  elif [ -z "${EXPECTED_CONTEXT}" ]; then
    echo "ERROR: this deployment does not state which cluster it is, so ${tool} refuses to act." >&2
    echo "       ${KUBE_CONTEXT_ERROR}" >&2
    echo "         current context = ${CURRENT_CONTEXT:-<none>}" >&2
    echo "       Two of the three deployments are production and both production clusters are named" >&2
    echo "       finngenie, so a guess here is a guess about production. THE FIX is one line in" >&2
    echo "       ${TFVARS}, at the start of a line, taken verbatim, and the file's ONLY mention of" >&2
    echo "       kube_context — this guard counts the token rather than parsing HCL:" >&2
    echo "" >&2
    echo "         kube_context = \"${CURRENT_CONTEXT:-gke_<project>_<zone>_<cluster>}\"" >&2
    echo "" >&2
    echo "       Check that name IS the cluster this deployment owns before pasting it —" >&2
    echo "       'kubectl config get-contexts -o name' lists them. ${tool}'s --context flag is" >&2
    echo "       not the fix: it authorises one deliberately off-target ${action}, it does not tell" >&2
    echo "       the guard what this deployment is. See README, \"${readme}\"." >&2
    exit 1
  elif [ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]; then
    echo "ERROR: kubectl's current context is not the cluster this deployment names." >&2
    echo "         current  = ${CURRENT_CONTEXT:-<none: no current context, or kubectl unavailable>}" >&2
    echo "         expected = ${EXPECTED_CONTEXT}" >&2
    echo "         from     = kube_context in ${TFVARS} (DEPLOY_ENV=${DEPLOY_ENV:-<unset>})" >&2
    echo "       ${gerund} would update a different cluster than the one you selected, and" >&2
    echo "       two of the three deployments are production. The fix, ready to run:" >&2
    echo "" >&2
    echo "         kubectl config use-context ${EXPECTED_CONTEXT}" >&2
    echo "" >&2
    echo "       An off-target ${action} is possible on purpose, with the --context flag, which must" >&2
    echo "       name the cluster being mutated; see README, \"${readme}\". This message" >&2
    echo "       deliberately does not spell that command out: the only context it could fill in is" >&2
    echo "       the one you are being warned away from, and a paste-ready line is not a decision." >&2
    exit 1
  else
    # The tfvars path is named on the ACCEPTING path too, not only on the refusals and the
    # OFF-TARGET banner. The guard's evidence is one line in one file, and which file that is
    # depends on ROOT_DIR and DEPLOY_ENV — both taken from the environment — so a guard reading
    # some other checkout's tfvars used to produce a success line indistinguishable from a correct
    # run. Printing the file makes that class of failure visible for the price of one line.
    # The tfvars AND the kubeconfig are both named here: they are the guard's two environment-
    # supplied inputs (ROOT_DIR picks the first, KUBECONFIG the second — see the hazards above),
    # and neither is visible in the context name itself.
    #
    # BE EXACT ABOUT WHICH KUBECONFIG HAZARD THIS PRINTED PATH COVERS, because an earlier revision
    # of this comment claimed it covered both and that was FALSE. It is truthful about ONE vector:
    # a KUBECONFIG INHERITED FROM THE ENVIRONMENT, already exported before the script starts. That
    # value is in scope here, so the line names the file the guard actually read.
    # It is NOT truthful about the `.env.<env>` vector (see the .env hazard above). This line
    # prints while create-secrets.sh has not yet sourced `.env`; a `KUBECONFIG=` line in that file
    # then takes effect afterwards, so the path printed here is accurate about what the GUARD read
    # and OBSOLETE about what the WRITES use. create-secrets.sh re-asserts the context after
    # sourcing for exactly that reason; this line is not the mitigation there.
    #
    # THIS IS THE THIRD OVERCLAIMING HAZARD COMMENT IN THIS FILE, and the pattern is the warning:
    # ROOT_DIR was called "not reachable as configured" and was then driven; the same words were
    # used of KUBECONFIG and were equally wrong; and this line claimed a mitigation that does not
    # reach the vector it was written next to. A hazard comment that asserts a bound is a claim to
    # be tested, not prose — if you cannot name the vector it covers AND the vector it does not,
    # do not write the bound.
    echo "Context: ${CURRENT_CONTEXT} (env: ${DEPLOY_ENV:-default}, kube_context in ${TFVARS}, kubeconfig ${KUBECONFIG:-${HOME}/.kube/config})"
  fi

  # Every path that reaches here leaves CURRENT_CONTEXT non-empty: an empty current context either
  # fails the --context equality test above or mismatches a non-empty EXPECTED_CONTEXT. So pinning
  # it on the callers' kubectl calls cannot degrade into an empty --context= argument.

  # FREEZE THE VERDICT. CURRENT_CONTEXT is a plain global, and the decision above is only worth as
  # much as the value the callers actually pin. create-secrets.sh runs this guard and THEN sources
  # .env.<env> — `set -a; . file; set +a`, arbitrary shell out of a file this repo does not own —
  # and a single `CURRENT_CONTEXT=` line in that file rewrote the context every later `--context`
  # pin carried, sending all three Secret writes to a cluster nothing had checked while the success
  # line above stayed entirely truthful about what WAS checked. Driven, not theorised, and silent:
  # nothing downstream reprints the context. It also nullified the TOCTOU pinning that is this
  # function's whole reason for setting a variable rather than letting kubectl re-read the
  # kubeconfig. `readonly` is what closes it — bash refuses both reassignment and `unset` on a
  # readonly global, so no later-sourced shell can move it — and it lives HERE, not in the caller,
  # because a property both callers depend on must not depend on each caller remembering it.
  #
  # Re-invocation in one shell is defined rather than left to trip the readonly: no caller does it
  # today (each runs the guard exactly once), but a future loop over deployments would, and an
  # unconditional `readonly` would abort on the second pass with a bash-level message about a
  # variable name. Same context re-verified: no-op. Different context: refuse, because the pinned
  # value cannot follow it and half the calls would already be on the first cluster.
  if [ -n "${ACTING_CONTEXT+set}" ]; then
    if [ "${ACTING_CONTEXT}" != "${CURRENT_CONTEXT}" ]; then
      echo "ERROR: ${tool} already froze its acting context earlier in this shell, and it differs." >&2
      echo "         frozen  = ${ACTING_CONTEXT}" >&2
      echo "         now     = ${CURRENT_CONTEXT}" >&2
      echo "       The pinned context cannot be re-aimed mid-run: calls already made went to the" >&2
      echo "       first cluster. Run one deployment per process." >&2
      exit 1
    fi
  else
    readonly ACTING_CONTEXT="${CURRENT_CONTEXT}"
  fi
}
