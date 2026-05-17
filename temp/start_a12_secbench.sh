#!/usr/bin/env bash
# Boot up A1/A2 secbench experiment on a GCP VM.
#
# Idempotent — safe to re-run. On a fresh VM, builds all missing secbench
# Docker images in parallel before launching the matrix.
#
# Usage:
#   bash temp/start_a12_secbench.sh
#   bash temp/start_a12_secbench.sh --parallel 8   # more concurrent CVE runs
#   bash temp/start_a12_secbench.sh --build-jobs 4 # parallel Docker builds

set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────────────

STUDY_ID="a12-batch-autogen"
CELLS="A1,A2"
PARALLEL="${PARALLEL:-8}"    # concurrent CVE runs
BUILD_JOBS="${BUILD_JOBS:-4}" # concurrent Docker image builds

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parallel)    PARALLEL="$2";    shift 2 ;;
    --build-jobs)  BUILD_JOBS="$2";  shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Locate repo root regardless of where the script is invoked from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

info()    { echo "▶ $*"; }
success() { echo "✓ $*"; }
die()     { echo "ERROR: $*" >&2; exit 1; }

# ─── 1. Environment ───────────────────────────────────────────────────────────

ENV_FILE="$REPO_DIR/deployment/.env"
[[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE — run deployment/gcp_setup.sh first"

set -a
source "$ENV_FILE"
set +a

# Postgres lives on localhost when running directly on the VM host (not inside
# the compose network), so force the host override regardless of what .env says.
export POSTGRES_HOST=localhost

[[ -n "${POSTGRES_PASSWORD:-}" ]] || die "POSTGRES_PASSWORD is empty in $ENV_FILE"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "ANTHROPIC_API_KEY is not set — A/B cells require it"

export PATH="${HOME}/.local/bin:${PATH}"

# ─── 2. Postgres ──────────────────────────────────────────────────────────────

info "Checking Postgres..."
if ! docker exec arise-db pg_isready -U "${POSTGRES_USER:-arise}" -d "${POSTGRES_DB:-arise_events}" &>/dev/null; then
  info "Starting Postgres (docker compose db)..."
  docker compose -f "$REPO_DIR/deployment/docker-compose.yml" --profile local up -d db
  for i in $(seq 1 30); do
    if docker exec arise-db pg_isready -U "${POSTGRES_USER:-arise}" -d "${POSTGRES_DB:-arise_events}" &>/dev/null; then
      success "Postgres is ready"
      break
    fi
    sleep 2
    [[ $i -lt 30 ]] || die "Postgres did not become healthy after 60s"
  done
else
  success "Postgres already running"
fi

# ─── 3. Secbench Docker images ────────────────────────────────────────────────

DATASET="$REPO_DIR/experiments/${STUDY_ID}/dataset.yaml"
[[ -f "$DATASET" ]] || die "Dataset not found: $DATASET"

mapfile -t ALL_CVES < <(grep '^ *- ' "$DATASET" | awk '{print $2}' | grep -v '/')
[[ ${#ALL_CVES[@]} -gt 0 ]] || die "No CVEs found in $DATASET"

# Resolve expected image name for a CVE slug (mirrors build-secbench-tools.sh logic)
resolve_target_image() {
  local cve="$1"
  cd "$REPO_DIR"
  uv run python - "hwiwonlee/secb.eval.x86_64.${cve}:patch" <<'PY'
import sys
from plugins.security import resolve_secbench_image
print(resolve_secbench_image(sys.argv[1], security_tools_enabled=True))
PY
}

MISSING=()
info "Checking secbench images for ${#ALL_CVES[@]} CVEs..."
for cve in "${ALL_CVES[@]}"; do
  img="$(resolve_target_image "$cve")"
  if ! docker image inspect "$img" &>/dev/null; then
    MISSING+=("$cve")
    echo "  missing: $img"
  else
    echo "  ok:      $img"
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo
  info "Building ${#MISSING[@]} missing image(s) with BUILD_JOBS=${BUILD_JOBS}..."
  # Export vars needed by each xargs worker subprocess
  export REPO_DIR BUILD_JOBS
  printf '%s\n' "${MISSING[@]}" \
    | xargs -P "$BUILD_JOBS" -I{} bash -c '
        cd "$REPO_DIR"
        echo "[build] starting {}"
        deployment/build-secbench-tools.sh "{}"
        echo "[build] done    {}"
      '
  success "All missing images built"
else
  success "All secbench images already present — skipping build"
fi

# ─── 4. Run the matrix ────────────────────────────────────────────────────────

echo
echo "════════════════════════════════════════════════════"
echo "  Launching A1,A2 matrix"
echo "  study:    $STUDY_ID"
echo "  cells:    $CELLS"
echo "  parallel: $PARALLEL"
echo "  started:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "════════════════════════════════════════════════════"
echo

cd "$REPO_DIR"
uv run python -m experiments.shared.scripts.run_matrix \
  --study "$STUDY_ID" \
  --cells "$CELLS" \
  --parallel "$PARALLEL" \
  --continue-on-error
