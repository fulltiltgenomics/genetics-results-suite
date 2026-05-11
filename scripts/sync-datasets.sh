#!/usr/bin/env bash
# syncs the canonical datasets.yaml to sibling service repos for local dev
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUITE_DIR="$(dirname "$SCRIPT_DIR")"
SOURCE="$SUITE_DIR/configs/datasets.yaml"

if [ ! -f "$SOURCE" ]; then
    echo "ERROR: source file not found: $SOURCE" >&2
    exit 1
fi

TARGETS=(
    "../genetics-results-db"
    "../genetics-results-api"
)

for rel in "${TARGETS[@]}"; do
    target_repo="$SUITE_DIR/$rel"
    target_dir="$target_repo/configs"
    target_file="$target_dir/datasets.yaml"

    if [ ! -d "$target_repo" ]; then
        echo "WARN: repo not found, skipping: $target_repo"
        continue
    fi

    mkdir -p "$target_dir"
    cp "$SOURCE" "$target_file"
    echo "OK: copied to $target_file"
done
