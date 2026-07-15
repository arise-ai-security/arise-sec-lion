#!/usr/bin/env bash
# Publish many SEC-bench tools tags without building or pulling their images.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="${BASH_SOURCE[0]}"
FIXTURE_DIR="$ROOT_DIR/plugins/security/tests/fixtures"
DEFAULT_DATASET="$ROOT_DIR/experiments/n1-secbench-full/dataset.yaml"
DEFAULT_REFERENCE_BASE="hwiwonlee/secb.eval.x86_64.openjpeg.cve-2024-56827@sha256:d17f6788f2915d31b21c8af1b3fa9b68ee71447247a1f97d4cec1217a06b0c5a"

usage() {
  cat <<'EOF'
Usage:
  deployment/publish-secbench-tools.sh REGISTRY --tool-image IMAGE [OPTIONS]

Publish the 300 N1 secb-tools tags by grafting one shared tool layer stack onto
each upstream base in Docker Hub. No CVE image is built or pulled locally.

Options:
  --tool-image IMAGE      Shared image produced by build-secbench-toolchain.sh.
  --reference-base IMAGE  Immutable base used for that build (default: pinned).
  --dataset FILE          Dataset YAML with default_cves (default: N1 300).
  --lock FILE             Lock YAML with selection_order instead of --dataset.
  --parallel N            Concurrent registry operations (default: 8).
  --regctl PATH           Explicit regctl binary; otherwise pinned v0.11.5 is used.
  --force                 Replace tags already present on the registry.
  --dry-run               Resolve and print all base -> destination mappings.

Run `docker login` for REGISTRY first.

Example:
  deployment/build-secbench-toolchain.sh cheshire0814
  deployment/publish-secbench-tools.sh cheshire0814 \
    --tool-image cheshire0814/secb-tools:toolchain-focal-amd64-v1 --parallel 8
EOF
}

process_one() {
  local id="$1"
  local entry base destination output
  entry=$(awk -F'\t' -v id="$id" '$1 == id {print; exit}' "$TAGMAP")
  if [ -z "$entry" ]; then
    printf 'FAIL  %s (no image mapping)\n' "$id" >&2
    printf 'FAIL:%s\n' "$id" >> "$RESULTS"
    return 0
  fi
  base=$(printf '%s\n' "$entry" | cut -f2)
  destination=$(printf '%s\n' "$entry" | cut -f3)

  args=(
    uv run python -m plugins.security.toolchain_graft
    --regctl "$REGCTL_PATH"
    --base "$base"
    --destination "$destination"
    --tool-image "$TOOL_IMAGE"
    --reference-base "$REFERENCE_BASE"
  )
  [ "$FORCE" -eq 1 ] && args+=(--force)
  if output=$("${args[@]}" 2>&1); then
    if printf '%s' "$output" | grep -q '"status": "skipped"'; then
      printf 'SKIP  %s (already on registry)\n' "$id" >&2
      printf 'SKIP\n' >> "$RESULTS"
    else
      printf 'OK    %s\n' "$id" >&2
      printf 'OK\n' >> "$RESULTS"
    fi
    return 0
  fi
  printf 'FAIL  %s: %s\n' "$id" "$output" >&2
  printf 'FAIL:%s\n' "$id" >> "$RESULTS"
}

if [ "${1:-}" = "--worker" ]; then
  process_one "$2"
  exit 0
fi

REGISTRY=""
TOOL_IMAGE=""
REFERENCE_BASE="$DEFAULT_REFERENCE_BASE"
DATASET="$DEFAULT_DATASET"
LOCK_FILE=""
PARALLEL=8
REGCTL_PATH=""
FORCE=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tool-image)     TOOL_IMAGE="$2"; shift 2 ;;
    --reference-base) REFERENCE_BASE="$2"; shift 2 ;;
    --dataset)        DATASET="$2"; LOCK_FILE=""; shift 2 ;;
    --lock)           LOCK_FILE="$2"; shift 2 ;;
    --parallel)       PARALLEL="$2"; shift 2 ;;
    --regctl)         REGCTL_PATH="$2"; shift 2 ;;
    --force)          FORCE=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    -*)               printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *)                REGISTRY="$1"; shift ;;
  esac
done

if [ -z "$REGISTRY" ] || [ -z "$TOOL_IMAGE" ]; then
  printf 'REGISTRY and --tool-image are required.\n\n' >&2
  usage >&2
  exit 2
fi
if ! [[ "$PARALLEL" =~ ^[1-9][0-9]*$ ]]; then
  printf '%s\n' "--parallel must be a positive integer: $PARALLEL" >&2
  exit 2
fi
REGISTRY="${REGISTRY%/}"
SOURCE_FILE="$DATASET"
[ -n "$LOCK_FILE" ] && SOURCE_FILE="$LOCK_FILE"
if [ ! -f "$SOURCE_FILE" ]; then
  printf 'Dataset or lock file not found: %s\n' "$SOURCE_FILE" >&2
  exit 2
fi

RESULTS="$(mktemp -t publish-tools-results.XXXXXX)"
TAGMAP="$(mktemp -t publish-tools-tagmap.XXXXXX)"
trap 'rm -f "$RESULTS" "$TAGMAP"' EXIT

uv run python - "$SOURCE_FILE" "$FIXTURE_DIR" "$REGISTRY" > "$TAGMAP" <<'PY'
import sys

import yaml

from plugins.security import CVEInstance, resolve_secbench_image

source_file, fixture_dir, registry = sys.argv[1:]
with open(source_file, encoding="utf-8") as stream:
    source = yaml.safe_load(stream)
ids = source.get("selection_order") or source.get("default_cves")
if not isinstance(ids, list):
    raise SystemExit(f"No selection_order or default_cves list in {source_file}")
for instance_id in ids:
    cve = CVEInstance.from_json_file(f"{fixture_dir}/{instance_id}.json")
    local = resolve_secbench_image(cve.docker_image, security_tools_enabled=True)
    print(f"{instance_id}\t{cve.docker_image}\t{registry}/{local}")
PY

if [ ! -s "$TAGMAP" ]; then
  printf 'No images resolved from %s\n' "$SOURCE_FILE" >&2
  exit 2
fi
IDS=()
while IFS=$'\t' read -r id _; do IDS+=("$id"); done < "$TAGMAP"

printf '%d images from %s (parallel=%d)\n' "${#IDS[@]}" "$SOURCE_FILE" "$PARALLEL" >&2
if [ "$DRY_RUN" -eq 1 ]; then
  awk -F'\t' '{printf "%s\n  base: %s\n  dest: %s\n", $1, $2, $3}' "$TAGMAP"
  exit 0
fi

if [ -z "$REGCTL_PATH" ]; then
  if ! REGCTL_PATH=$(uv run python -m plugins.security.toolchain_graft --print-regctl-path); then
    printf 'Failed to resolve pinned regctl.\n' >&2
    exit 2
  fi
fi
export RESULTS TAGMAP REGCTL_PATH TOOL_IMAGE REFERENCE_BASE FORCE

printf '%s\0' "${IDS[@]}" | xargs -0 -P "$PARALLEL" -I {} bash "$SELF" --worker {}

OK_N=$(grep -c '^OK$' "$RESULTS" 2>/dev/null || true)
SKIP_N=$(grep -c '^SKIP$' "$RESULTS" 2>/dev/null || true)
FAIL_IDS=$(grep '^FAIL:' "$RESULTS" 2>/dev/null | sed 's/^FAIL://' || true)
FAIL_N=0
[ -n "$FAIL_IDS" ] && FAIL_N=$(printf '%s\n' "$FAIL_IDS" | wc -l | tr -d ' ')

{
  printf '\n=====================================\n'
  printf ' Shared-layer publish summary\n'
  printf ' Published: %s\n' "${OK_N:-0}"
  printf ' Skipped:   %s\n' "${SKIP_N:-0}"
  printf ' Failed:    %s\n' "$FAIL_N"
  printf '=====================================\n'
  [ "$FAIL_N" -gt 0 ] && { printf 'Failed:\n'; printf '%s\n' "$FAIL_IDS" | sed 's/^/  - /'; }
} >&2

[ "$FAIL_N" -gt 0 ] && exit 1
exit 0
