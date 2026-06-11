#!/usr/bin/env bash
# Push the locally-built secb-tools images for a study's pinned instances to a
# registry, so others auto-pull them instead of rebuilding. For each instance
# it tags secb-tools:<id>-patch -> <registry>/secb-tools:<id>-patch and pushes.
# The matching pull happens automatically at run time (security.tools_image_registry).
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_LOCK="$ROOT_DIR/experiments/shared/datasets/cve50-2026-06-09.lock.yaml"

usage() {
  cat <<'EOF'
Usage:
  deployment/push-study-images.sh REGISTRY [--lock FILE]

Tag and push every secb-tools:<id>-patch image pinned in a dataset lock to
REGISTRY (a Docker Hub namespace like 'cheshire0814', or 'ghcr.io/org'). Run
`docker login` first. Locally-missing images are skipped with a warning
(build them first via deployment/build-all-images.sh --lock ...).

Examples:
  deployment/push-study-images.sh cheshire0814
  deployment/push-study-images.sh ghcr.io/cheshire0814 --lock path/to/other.lock.yaml
EOF
}

REGISTRY=""; LOCK_FILE="$DEFAULT_LOCK"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --lock)       LOCK_FILE="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *)            REGISTRY="$1"; shift ;;
  esac
done

if [ -z "$REGISTRY" ]; then
  printf 'REGISTRY argument is required.\n\n' >&2; usage >&2; exit 2
fi
REGISTRY="${REGISTRY%/}"

if [ ! -f "$LOCK_FILE" ]; then
  printf 'Lock file not found: %s\n' "$LOCK_FILE" >&2; exit 2
fi

# selection_order is a flat list of '- <id>' lines (machine-written by
# curate_cve50.py); stop at the first non-list line (smoke_tasks:, studies:).
IDS="$(awk '/^selection_order:/ {in_list=1; next}
            in_list && /^- / {print $2; next}
            in_list {exit}' "$LOCK_FILE")"
if [ -z "$IDS" ]; then
  printf 'No selection_order entries in lock file: %s\n' "$LOCK_FILE" >&2; exit 2
fi

PUSHED=0; MISSING=0; FAILED=0; MISSING_IDS=""; FAILED_IDS=""
while IFS= read -r id; do
  local_tag="secb-tools:${id}-patch"
  remote_tag="${REGISTRY}/secb-tools:${id}-patch"
  if ! docker image inspect "$local_tag" >/dev/null 2>&1; then
    printf 'MISSING %s (build it first)\n' "$local_tag" >&2
    MISSING=$((MISSING + 1)); MISSING_IDS="${MISSING_IDS}${id}\n"; continue
  fi
  printf '==> Pushing %s\n' "$remote_tag" >&2
  if docker tag "$local_tag" "$remote_tag" && docker push "$remote_tag" >&2; then
    PUSHED=$((PUSHED + 1))
  else
    printf 'FAIL  %s\n' "$remote_tag" >&2
    FAILED=$((FAILED + 1)); FAILED_IDS="${FAILED_IDS}${id}\n"
  fi
done <<< "$IDS"

{
  printf '=====================================\n'
  printf ' Push summary (registry: %s)\n' "$REGISTRY"
  printf ' Pushed:  %s\n' "$PUSHED"
  printf ' Missing: %s\n' "$MISSING"
  printf ' Failed:  %s\n' "$FAILED"
  printf '=====================================\n'
  [ "$MISSING" -gt 0 ] && { printf 'Missing (not built locally):\n'; printf "$MISSING_IDS" | sed 's/^/  - /'; }
  [ "$FAILED" -gt 0 ] && { printf 'Failed to push:\n'; printf "$FAILED_IDS" | sed 's/^/  - /'; }
} >&2

[ "$FAILED" -gt 0 ] && exit 1
exit 0
