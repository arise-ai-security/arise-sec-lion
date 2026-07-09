#!/usr/bin/env bash
# Publish a study's pinned secb-tools images from a machine too small to hold
# them all at once. Processes the lock in batches; within a batch each instance
# is handled by its own worker that builds -> pushes -> removes the image, so
# CPU-bound builds and network-bound pushes OVERLAP across workers (no idle
# cores waiting on uploads) and local disk holds at most ~PARALLEL images.
#
# Image names are resolved from each fixture's JSON (a lock id like 'php.*' can
# map to an image like 'php-src.*'), so pushes match what the runtime pulls.
#
# Idempotent: an instance already present on REGISTRY is skipped (not rebuilt),
# so the job is safe to stop and re-run. Removed images are re-pulled on demand
# at run time (security.tools_image_registry), so deleting them after a
# confirmed push is lossless.
#
# Requires: `docker login` as the REGISTRY user before running.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="${BASH_SOURCE[0]}"
SINGLE_BUILDER="$ROOT_DIR/deployment/build-secbench-tools.sh"
FIXTURE_DIR="$ROOT_DIR/plugins/security/tests/fixtures"
DEFAULT_LOCK="$ROOT_DIR/experiments/shared/datasets/cve50-2026-06-09.lock.yaml"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-push-batch.sh REGISTRY [--lock FILE] [--batch N] [--parallel J] [--dry-run]

Build the lock's pinned instances in batches, pushing each to REGISTRY (a Docker
Hub namespace like 'cheshire0814') and removing it locally as soon as it pushes.
Instances already on REGISTRY are skipped.

Options:
  --batch N      Instances per batch / cache-prune checkpoint (default: 5).
  --parallel J   Concurrent build->push->remove workers per batch (default: 5).
  --dry-run      Print the batch plan and exit.

Run `docker login` as the REGISTRY user first.

Example:
  docker login
  deployment/build-push-batch.sh cheshire0814 --batch 5 --parallel 5
EOF
}

# --- worker mode: build -> push -> remove one instance (invoked via xargs) -----
# Reads REGISTRY, FIXTURE_DIR, SINGLE_BUILDER, RESULTS, TAGMAP from exported env.
# TAGMAP lines: "<lock_id>\t<local_tag>\t<base_tag>".
process_one() {
  local id="$1"
  local entry local_tag base_tag remote_tag
  entry=$(awk -F'\t' -v id="$id" '$1 == id {print; exit}' "$TAGMAP")
  if [ -z "$entry" ]; then
    printf 'FAIL  %s (no tag mapping)\n' "$id" >&2
    printf 'FAIL:%s\n' "$id" >> "$RESULTS"; return 0
  fi
  local_tag=$(printf '%s\n' "$entry" | cut -f2)
  base_tag=$(printf '%s\n' "$entry" | cut -f3)
  remote_tag="${REGISTRY}/${local_tag}"

  if docker manifest inspect "$remote_tag" >/dev/null 2>&1; then
    printf 'SKIP  %s (already on registry)\n' "$id" >&2
    printf 'SKIP\n' >> "$RESULTS"; return 0
  fi
  if ! docker image inspect "$local_tag" >/dev/null 2>&1; then
    if ! bash "$SINGLE_BUILDER" "$FIXTURE_DIR/${id}.json" >&2; then
      printf 'FAIL  %s (build)\n' "$id" >&2
      printf 'FAIL:%s\n' "$id" >> "$RESULTS"; return 0
    fi
  fi
  printf '==> Pushing %s\n' "$remote_tag" >&2
  if docker tag "$local_tag" "$remote_tag" && docker push "$remote_tag" >&2; then
    printf 'OK    %s\n' "$id" >&2
    printf 'OK\n' >> "$RESULTS"
    # Reclaim only AFTER a confirmed push. Frees the shared base layers too.
    docker rmi "$remote_tag" "$local_tag" "$base_tag" >/dev/null 2>&1 || true
  else
    printf 'FAIL  %s (push — keeping local image)\n' "$id" >&2
    printf 'FAIL:%s\n' "$id" >> "$RESULTS"
  fi
}

if [ "${1:-}" = "--worker" ]; then
  process_one "$2"; exit 0
fi

# --- main ----------------------------------------------------------------------
REGISTRY=""; LOCK_FILE="$DEFAULT_LOCK"; BATCH=5; PARALLEL=5; DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --lock)       LOCK_FILE="$2"; shift 2 ;;
    --batch)      BATCH="$2"; shift 2 ;;
    --parallel)   PARALLEL="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
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

# selection_order is a flat '- <id>' list (machine-written by curate_cve50.py);
# stop at the first non-list line. Read loop (not mapfile) for bash 3.2 (macOS).
IDS=()
while IFS= read -r id; do
  [ -n "$id" ] && IDS+=("$id")
done < <(awk '/^selection_order:/ {in_list=1; next}
              in_list && /^- / {print $2; next}
              in_list {exit}' "$LOCK_FILE")
if [ "${#IDS[@]}" -eq 0 ]; then
  printf 'No selection_order entries in lock file: %s\n' "$LOCK_FILE" >&2; exit 2
fi

TOTAL="${#IDS[@]}"
NUM_BATCHES=$(( (TOTAL + BATCH - 1) / BATCH ))
printf '%d instances, batch=%d, parallel=%d => %d batches (registry: %s)\n' \
  "$TOTAL" "$BATCH" "$PARALLEL" "$NUM_BATCHES" "$REGISTRY" >&2

if [ "$DRY_RUN" -eq 1 ]; then
  b=0
  for ((i = 0; i < TOTAL; i += BATCH)); do
    b=$((b + 1)); printf 'Batch %d: %s\n' "$b" "${IDS[*]:i:BATCH}" >&2
  done
  exit 0
fi

RESULTS="$(mktemp -t build-push-results.XXXXXX)"
TAGMAP="$(mktemp -t build-push-tagmap.XXXXXX)"
trap 'rm -f "$RESULTS" "$TAGMAP"' EXIT

# Resolve real image + base names from each fixture's JSON (lock id != image
# name for php-src/ollama/...). One python call; workers read the map.
uv run python - "$LOCK_FILE" "$FIXTURE_DIR" > "$TAGMAP" <<'PY'
import sys, yaml
from plugins.security import CVEInstance, resolve_secbench_image
lock, fixdir = sys.argv[1], sys.argv[2]
for i in yaml.safe_load(open(lock))["selection_order"]:
    c = CVEInstance.from_json_file(f"{fixdir}/{i}.json")
    local = resolve_secbench_image(c.docker_image, security_tools_enabled=True)
    print(f"{i}\t{local}\t{c.docker_image}")
PY
if [ ! -s "$TAGMAP" ]; then
  printf 'Failed to resolve image tags via python.\n' >&2; exit 2
fi
export REGISTRY FIXTURE_DIR SINGLE_BUILDER RESULTS TAGMAP

b=0
for ((i = 0; i < TOTAL; i += BATCH)); do
  b=$((b + 1)); chunk=("${IDS[@]:i:BATCH}")
  printf '\n===== Batch %d/%d: %s =====\n' "$b" "$NUM_BATCHES" "${chunk[*]}" >&2
  # Pool of build->push->remove workers; builds and uploads overlap across them.
  printf '%s\0' "${chunk[@]}" | xargs -0 -n1 -P "$PARALLEL" -I {} bash "$SELF" --worker {}
  # Reclaim transient build cache before the next batch (CVEs are different
  # projects, so cross-batch cache reuse is negligible).
  docker builder prune -f >/dev/null 2>&1 || true
done

OK_N=$(grep -c '^OK$' "$RESULTS" 2>/dev/null || echo 0)
SKIP_N=$(grep -c '^SKIP$' "$RESULTS" 2>/dev/null || echo 0)
FAIL_IDS=$(grep '^FAIL:' "$RESULTS" 2>/dev/null | sed 's/^FAIL://')
FAIL_N=0; [ -n "$FAIL_IDS" ] && FAIL_N=$(printf '%s\n' "$FAIL_IDS" | wc -l | tr -d ' ')

{
  printf '\n=====================================\n'
  printf ' Build+push summary (registry: %s)\n' "$REGISTRY"
  printf ' Pushed:   %s\n' "$OK_N"
  printf ' Skipped:  %s (already on registry)\n' "$SKIP_N"
  printf ' Failed:   %s\n' "$FAIL_N"
  printf '=====================================\n'
  [ "$FAIL_N" -gt 0 ] && { printf 'Failed:\n'; printf '%s\n' "$FAIL_IDS" | sed 's/^/  - /'; }
} >&2

[ "$FAIL_N" -gt 0 ] && exit 1
exit 0
