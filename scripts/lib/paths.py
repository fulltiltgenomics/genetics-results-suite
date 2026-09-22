"""The fixed locations of this repo, for the scripts that run on the host.

WHY A MODULE. Every script under scripts/ has to know where the repo is before it can do
anything, and each one used to answer that with its own copy of the same dirname() chain
and its own spelling of the paths beneath it — the one-fact-in-N-copies shape
check-duplication.py measures. A script that imports this still has to find it, so each
caller keeps ONE line of its own, sys.path.insert(0, <its path to scripts/lib>), and that
line is the whole of what it knows about the layout.

WHAT BELONGS HERE: the repo's directories and its one canonical registry. A file that a
single script reads stays in that script, next to the reason it reads it.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPTS_DIR = os.path.join(ROOT, "scripts")
DATASETS_YAML = os.path.join(ROOT, "configs", "datasets.yaml")
K8S_DIR = os.path.join(ROOT, "k8s")
K8S_DEPLOYMENTS_DIR = os.path.join(K8S_DIR, "deployments")
K8S_POLICIES_DIR = os.path.join(K8S_DIR, "network-policies")
SANDBOX_DIR = os.path.join(ROOT, "sandbox")
SCHEMA_DIR = os.path.join(SANDBOX_DIR, "schema")
STUBS_DIR = os.path.join(SANDBOX_DIR, "stubs")
STAGED_SDK_SRC = os.path.join(SANDBOX_DIR, ".sdk-src")
URL_FETCHER_DIR = os.path.join(ROOT, "url-fetcher")

# must match dev-stack.sh's own default (RUN_DIR="${DEV_STACK_RUN_DIR:-$HOME/.cache/genetics-dev-stack}"),
# so a developer who sets DEV_STACK_RUN_DIR does not also have to pass --run-dir to each harness
DEV_STACK_RUN_DIR = os.environ.get("DEV_STACK_RUN_DIR") or os.path.join(
    os.path.expanduser("~"), ".cache", "genetics-dev-stack")
