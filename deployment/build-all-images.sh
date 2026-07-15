#!/usr/bin/env bash
# Provision SEC-bench tools images for every fixture JSON.
# Pulls the shared registry cache first and builds only on a cache miss.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SINGLE_BUILDER="$ROOT_DIR/deployment/build-secbench-tools.sh"
FIXTURE_DIR="$ROOT_DIR/plugins/security/tests/fixtures"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-all-images.sh [OPTIONS] [FIXTURE_GLOB...]

Build secb-tools:<instance_id>-patch images for SEC-bench fixtures.
With no FIXTURE_GLOB, builds every plugins/security/tests/fixtures/*.json.

Registry-cached images are pulled and locally aliased first. Missing cache
entries fall back to deployment/build-secbench-tools.sh.

Options:
  -j, --parallel N        Provision N images concurrently (default: 1).
  --lock FILE             Build the instances pinned in a dataset lock YAML
                          (ids read from its selection_order list).
  --continue-on-error     Keep going after a failed build (default).
  --fail-fast             Stop launching new builds after the first failure.
  --dry-run               List target tags only; do not invoke docker.
  -h, --help              Show this help.

Examples:
  deployment/build-all-images.sh -j 4
  deployment/build-all-images.sh plugins/security/tests/fixtures/openjpeg.cve-2016-7445.json
  deployment/build-all-images.sh -j 4 --lock experiments/shared/datasets/cve50-2026-06-09.lock.yaml
EOF
}

target_tag_for_fixture() {
  printf 'secb-tools:%s-patch\n' "$(basename "$1" .json)"
}

local_image_ready() {
  local target="$1"
  local metadata
  metadata="$(
    docker image inspect \
      --format '{{.Architecture}}|{{.Descriptor.MediaType}}' "$target" 2>/dev/null
  )" || return 1
  [[ "$metadata" == amd64\|* ]] || return 1
  [[ "$metadata" != *image.index* && "$metadata" != *manifest.list* ]]
}

pull_cached_image() {
  local target="$1"
  local remote repository manifest_digest child architecture
  [ -n "$TOOLS_IMAGE_REGISTRY" ] || return 1
  remote="$TOOLS_IMAGE_REGISTRY/$target"
  if docker pull --platform linux/amd64 "$remote" >/dev/null 2>&1; then
    if ! docker tag "$remote" "$target" >/dev/null 2>&1; then
      docker image rm "$remote" >/dev/null 2>&1 || true
      return 1
    fi
    docker image rm "$remote" >/dev/null 2>&1 || true
    return 0
  fi

  # The first public cache generation has amd64 configs and binaries under OCI
  # descriptors mislabeled as arm64. Pull that exact child manifest, then fail
  # closed unless Docker verifies the resulting image config as amd64.
  manifest_digest="$(
    docker manifest inspect "$remote" 2>/dev/null |
      uv run python -c '
import json
import sys

index = json.load(sys.stdin)
for manifest in index.get("manifests", []):
    platform = manifest.get("platform", {})
    if platform.get("os") == "linux" and platform.get("architecture") == "arm64":
        print(manifest["digest"])
        break
else:
    raise SystemExit(1)
'
  )" || return 1
  [[ "$manifest_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  repository="${remote%:*}"
  child="$repository@$manifest_digest"
  if ! docker pull "$child" >/dev/null 2>&1; then
    return 1
  fi
  architecture="$(
    docker image inspect --format '{{.Architecture}}' "$child" 2>/dev/null
  )"
  if [ "$architecture" != "amd64" ] || ! docker tag "$child" "$target" >/dev/null 2>&1; then
    docker image rm "$child" >/dev/null 2>&1 || true
    return 1
  fi
  docker image rm "$child" >/dev/null 2>&1 || true
}

# Worker mode: provision one fixture and append one result line to $RESULTS.
# Single-line writes under PIPE_BUF (4KB) are atomic on POSIX, so parallel
# workers safely share one results file without locking.
run_worker() {
  local fixture="$1"
  local instance_id target rc
  instance_id="$(basename "$fixture" .json)"
  target="$(target_tag_for_fixture "$fixture")"

  if [ -n "${STOP_FILE:-}" ] && [ -f "$STOP_FILE" ]; then
    printf 'SKIPPED %s (fail-fast)\n' "$instance_id" >&2
    printf 'SKIPPED\n' >> "$RESULTS"
    return 0
  fi
  if local_image_ready "$target"; then
    printf 'Skipping existing image %s\n' "$target" >&2
    printf 'SKIPPED\n' >> "$RESULTS"
    return 0
  fi
  printf '==> Provisioning %s\n' "$instance_id" >&2
  if pull_cached_image "$target"; then
    printf 'CACHED\n' >> "$RESULTS"
    return 0
  fi
  printf 'Cache miss; building %s locally\n' "$instance_id" >&2
  bash "$SINGLE_BUILDER" "$fixture" >&2
  rc=$?
  if [ "$rc" -eq 0 ]; then
    printf 'BUILT\n' >> "$RESULTS"
    return 0
  fi
  printf 'FAIL  %s (rc=%d)\n' "$instance_id" "$rc" >&2
  printf 'FAILED:%s\n' "$instance_id" >> "$RESULTS"
  [ -n "${STOP_FILE:-}" ] && : > "$STOP_FILE"
  return 0
}

PARALLEL=1; FAIL_FAST=0; DRY_RUN=0; LOCK_FILE=""; INPUTS=()
TOOLS_IMAGE_REGISTRY="${TOOLS_IMAGE_REGISTRY:-cheshire0814}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -j|--parallel)        PARALLEL="$2"; shift 2 ;;
    --lock)               LOCK_FILE="$2"; shift 2 ;;
    --continue-on-error)  FAIL_FAST=0; shift ;;
    --fail-fast)          FAIL_FAST=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    --worker)             run_worker "$2"; exit 0 ;;
    --)                   shift; INPUTS+=("$@"); break ;;
    -*)                   printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *)                    INPUTS+=("$1"); shift ;;
  esac
done

if [ ! -f "$SINGLE_BUILDER" ]; then
  printf 'Missing single-fixture builder: %s\n' "$SINGLE_BUILDER" >&2
  exit 2
fi

if [ -n "$LOCK_FILE" ]; then
  if [ ! -f "$LOCK_FILE" ]; then
    printf 'Lock file not found: %s\n' "$LOCK_FILE" >&2
    exit 2
  fi
  # Lock files are machine-written (curate_cve50.py): zero-indent '- <id>' lines
  # directly under selection_order:, so awk suffices and the script stays
  # dependency-free. Stop at the first non-list line (smoke_tasks:, studies:).
  LOCK_IDS="$(awk '/^selection_order:/ {in_list=1; next}
                   in_list && /^- / {print $2; next}
                   in_list {exit}' "$LOCK_FILE")"
  if [ -z "$LOCK_IDS" ]; then
    printf 'No selection_order entries in lock file: %s\n' "$LOCK_FILE" >&2
    exit 2
  fi
  while IFS= read -r id; do
    INPUTS+=("$FIXTURE_DIR/$id.json")
  done <<< "$LOCK_IDS"
fi

FIXTURES=()
if [ "${#INPUTS[@]}" -eq 0 ]; then
  for f in "$FIXTURE_DIR"/*.json; do [ -f "$f" ] && FIXTURES+=("$f"); done
else
  for arg in "${INPUTS[@]}"; do
    for f in $arg; do
      if [ -f "$f" ]; then FIXTURES+=("$f")
      else printf 'Skipping non-file argument: %s\n' "$f" >&2
      fi
    done
  done
fi

if [ "${#FIXTURES[@]}" -eq 0 ]; then
  printf 'No fixtures matched.\n' >&2; exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  for fx in "${FIXTURES[@]}"; do
    printf '%s -> %s\n' "$fx" "$(target_tag_for_fixture "$fx")"
  done
  printf 'Would provision %d fixture(s).\n' "${#FIXTURES[@]}" >&2
  exit 0
fi

RESULTS="$(mktemp -t build-all-images.XXXXXX)"
STOP_FILE=""
[ "$FAIL_FAST" -eq 1 ] && STOP_FILE="$(mktemp -u -t build-all-images-stop.XXXXXX)"
trap 'rm -f "$RESULTS" "$STOP_FILE"' EXIT
export RESULTS STOP_FILE SINGLE_BUILDER TOOLS_IMAGE_REGISTRY

if [ "$PARALLEL" -le 1 ]; then
  for fx in "${FIXTURES[@]}"; do run_worker "$fx"; done
else
  # xargs -P spawns up to N worker invocations in parallel. -0/-I keep paths
  # with spaces or globs intact and pass one fixture per worker call.
  printf '%s\0' "${FIXTURES[@]}" | \
    xargs -0 -P "$PARALLEL" -I {} bash "${BASH_SOURCE[0]}" --worker {}
fi

BUILT=$(grep -c '^BUILT$' "$RESULTS" 2>/dev/null || true)
CACHED=$(grep -c '^CACHED$' "$RESULTS" 2>/dev/null || true)
SKIPPED=$(grep -c '^SKIPPED$' "$RESULTS" 2>/dev/null || true)
FAILED_IDS=$(grep '^FAILED:' "$RESULTS" 2>/dev/null | sed 's/^FAILED://')
FAILED=0
[ -n "$FAILED_IDS" ] && FAILED=$(printf '%s\n' "$FAILED_IDS" | wc -l | tr -d ' ')

{
  printf '=====================================\n'
  printf ' Image provisioning summary\n'
  printf ' Cached:  %s\n' "$CACHED"
  printf ' Built:   %s\n' "$BUILT"
  printf ' Skipped: %s\n' "$SKIPPED"
  printf ' Failed:  %s\n' "$FAILED"
  printf '=====================================\n'
  if [ "$FAILED" -gt 0 ]; then
    printf '%s\n' "$FAILED_IDS" | while IFS= read -r id; do
      [ -n "$id" ] && printf '  - %s\n' "$id"
    done
  fi
} >&2

[ "$FAILED" -gt 0 ] && exit 1
exit 0
