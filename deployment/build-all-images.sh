#!/usr/bin/env bash
# Bulk-build SEC-bench tools images for every fixture JSON.
# Delegates the per-fixture docker build to deployment/build-secbench-tools.sh.
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

Options:
  -j, --parallel N        Build N images concurrently (default: 1).
  --continue-on-error     Keep going after a failed build (default).
  --fail-fast             Stop launching new builds after the first failure.
  --dry-run               List target tags only; do not invoke docker.
  -h, --help              Show this help.

Examples:
  deployment/build-all-images.sh -j 4
  deployment/build-all-images.sh plugins/security/tests/fixtures/openjpeg.cve-2016-7445.json
EOF
}

target_tag_for_fixture() {
  printf 'secb-tools:%s-patch\n' "$(basename "$1" .json)"
}

# Worker mode: build one fixture and append one result line to $RESULTS.
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
  if docker image inspect "$target" >/dev/null 2>&1; then
    printf 'Skipping existing image %s\n' "$target" >&2
    printf 'SKIPPED\n' >> "$RESULTS"
    return 0
  fi
  printf '==> Building %s\n' "$target" >&2
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

PARALLEL=1; FAIL_FAST=0; DRY_RUN=0; INPUTS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -j|--parallel)        PARALLEL="$2"; shift 2 ;;
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
  printf 'Would build %d fixture(s).\n' "${#FIXTURES[@]}" >&2
  exit 0
fi

RESULTS="$(mktemp -t build-all-images.XXXXXX)"
STOP_FILE=""
[ "$FAIL_FAST" -eq 1 ] && STOP_FILE="$(mktemp -u -t build-all-images-stop.XXXXXX)"
trap 'rm -f "$RESULTS" "$STOP_FILE"' EXIT
export RESULTS STOP_FILE SINGLE_BUILDER

if [ "$PARALLEL" -le 1 ]; then
  for fx in "${FIXTURES[@]}"; do run_worker "$fx"; done
else
  # xargs -P spawns up to N worker invocations in parallel. -0/-I keep paths
  # with spaces or globs intact and pass one fixture per worker call.
  printf '%s\0' "${FIXTURES[@]}" | \
    xargs -0 -n1 -P "$PARALLEL" -I {} bash "${BASH_SOURCE[0]}" --worker {}
fi

BUILT=$(grep -c '^BUILT$' "$RESULTS" 2>/dev/null || echo 0)
SKIPPED=$(grep -c '^SKIPPED$' "$RESULTS" 2>/dev/null || echo 0)
FAILED_IDS=$(grep '^FAILED:' "$RESULTS" 2>/dev/null | sed 's/^FAILED://')
FAILED=0
[ -n "$FAILED_IDS" ] && FAILED=$(printf '%s\n' "$FAILED_IDS" | wc -l | tr -d ' ')

{
  printf '=====================================\n'
  printf ' Build summary\n'
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
