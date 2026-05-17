#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 GCP Compute Engine VM.
#
# After this script completes, run:
#   cd ~/arise-sec-lion
#   set -a && source deployment/.env && set +a
#   uv run python -m experiments.shared.scripts.run_matrix \
#     --study <study-id> --cells A1,A2 --parallel 8
#
# Usage:
#   bash gcp_setup.sh --study 2026-04-23-initial-secbench
#   bash gcp_setup.sh --study 2026-04-23-initial-secbench --repo-dir /opt/arise
#   bash gcp_setup.sh --skip-clone --study 2026-04-23-initial-secbench

set -euo pipefail

# ─── Config ────────────────────────────────────────────────────────────────────

REPO_URL="https://github.com/arise-ai-security/arise-sec-lion"
REPO_DIR="${HOME}/arise-sec-lion"
SKIP_CLONE=false
STUDY_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir)   REPO_DIR="$2"; shift 2 ;;
    --skip-clone) SKIP_CLONE=true; shift ;;
    --study)      STUDY_ID="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ─── Helpers ───────────────────────────────────────────────────────────────────

info()    { echo "▶ $*"; }
success() { echo "✓ $*"; }
section() { echo; echo "══════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════"; }

require_ubuntu() {
  if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
    echo "ERROR: This script targets Ubuntu 22.04/24.04. Detected: $(. /etc/os-release && echo "$NAME $VERSION_ID")"
    exit 1
  fi
}

# ─── Step 1: System packages ───────────────────────────────────────────────────

section "1/7  System packages"

require_ubuntu
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  git curl ca-certificates gnupg build-essential \
  2>/dev/null
success "Base packages installed"

# ─── Step 2: Docker Engine ────────────────────────────────────────────────────

section "2/7  Docker Engine"

if command -v docker &>/dev/null; then
  success "Docker already installed ($(docker --version))"
else
  info "Installing Docker Engine..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo systemctl enable --now docker
  success "Docker installed"
fi

# Add current user to docker group so experiments can spawn secbench containers
# without sudo. Takes effect in the newgrp invocation at the end.
if ! groups | grep -q docker; then
  sudo usermod -aG docker "$USER"
  info "Added $USER to the docker group (takes effect after re-login)"
else
  success "$USER is already in the docker group"
fi

# ─── Step 3: Node.js 20 + Claude Code CLI ─────────────────────────────────────

section "3/7  Node.js 20 + Claude Code CLI"

if command -v node &>/dev/null && [[ "$(node --version | cut -d. -f1 | tr -d v)" -ge 20 ]]; then
  success "Node.js already installed ($(node --version))"
else
  info "Installing Node.js 20..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
  success "Node.js installed ($(node --version))"
fi

if command -v claude &>/dev/null; then
  success "Claude Code CLI already installed ($(claude --version 2>/dev/null || echo 'installed'))"
else
  info "Installing Claude Code CLI..."
  sudo npm install -g @anthropic-ai/claude-code
  success "Claude Code CLI installed"
fi

# ─── Step 4: uv ───────────────────────────────────────────────────────────────

section "4/7  uv (Python package manager)"

if command -v uv &>/dev/null; then
  success "uv already installed ($(uv --version))"
else
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  success "uv installed ($(uv --version))"
fi

export PATH="${HOME}/.local/bin:${PATH}"

# ─── Step 5: Clone repo + Python deps ─────────────────────────────────────────

section "5/7  Repo + Python dependencies"

if [[ "$SKIP_CLONE" == "true" ]]; then
  info "Skipping clone (--skip-clone); using REPO_DIR=$REPO_DIR"
  [[ -d "$REPO_DIR" ]] || { echo "ERROR: $REPO_DIR does not exist"; exit 1; }
else
  if [[ -d "$REPO_DIR/.git" ]]; then
    info "Repo already present at $REPO_DIR — pulling latest"
    git -C "$REPO_DIR" pull --ff-only
  else
    info "Cloning $REPO_URL → $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
  fi
fi

cd "$REPO_DIR"
info "Installing Python dependencies (uv sync --frozen)..."
uv sync --frozen
success "Python dependencies installed"

# ─── Step 6: Environment file ─────────────────────────────────────────────────

section "6/7  Environment (.env)"

ENV_FILE="$REPO_DIR/deployment/.env"

if [[ -f "$ENV_FILE" ]]; then
  info ".env already exists at $ENV_FILE — skipping interactive setup"
  info "Edit it manually if you need to change secrets."
else
  info "Creating $ENV_FILE from template..."
  cp "$REPO_DIR/deployment/.env.example" "$ENV_FILE"

  echo
  echo "Required secrets (press Enter to leave blank and edit manually later):"
  echo

  read -r -p "  POSTGRES_PASSWORD (required): " _pg_pass
  read -r -p "  ANTHROPIC_API_KEY (required for A/B cells): " _ant_key
  read -r -p "  OPENAI_API_KEY (optional, for non-Claude models): " _oai_key

  if [[ -n "$_pg_pass" ]]; then
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${_pg_pass}|" "$ENV_FILE"
  fi
  if [[ -n "$_ant_key" ]]; then
    sed -i "s|^# *ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${_ant_key}|" "$ENV_FILE"
  fi
  if [[ -n "$_oai_key" ]]; then
    sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=${_oai_key}|" "$ENV_FILE"
  fi

  # GCP: Postgres is on localhost (port mapped out of the db container), not
  # the Docker-internal "db" hostname used by other compose services.
  sed -i "s|^POSTGRES_HOST=.*|POSTGRES_HOST=localhost|" "$ENV_FILE"

  # HOST_PROJECT_ROOT must be the absolute host path so secbench container
  # volume mounts resolve correctly under Docker-out-of-Docker.
  echo "HOST_PROJECT_ROOT=${REPO_DIR}" >> "$ENV_FILE"

  success ".env written"
fi

# Verify required values are present
source "$ENV_FILE"
[[ -n "${POSTGRES_PASSWORD:-}" ]] || { echo "ERROR: POSTGRES_PASSWORD is empty in $ENV_FILE"; exit 1; }
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || echo "WARNING: ANTHROPIC_API_KEY is not set — A/B cells will fail at runtime"

# ─── Step 7: Start Postgres + build secbench images ───────────────────────────

section "7/7  Postgres + secbench Docker images"

info "Starting Postgres (docker compose db)..."
cd "$REPO_DIR"
sudo docker compose -f deployment/docker-compose.yml --profile local up -d db
info "Waiting for Postgres health check..."
for i in $(seq 1 30); do
  if sudo docker exec arise-db pg_isready -U "${POSTGRES_USER:-arise}" -d "${POSTGRES_DB:-arise_events}" &>/dev/null; then
    success "Postgres is ready"
    break
  fi
  sleep 2
  if [[ $i -eq 30 ]]; then
    echo "ERROR: Postgres did not become healthy after 60s"
    sudo docker logs arise-db --tail 30
    exit 1
  fi
done

if [[ -n "$STUDY_ID" ]]; then
  DATASET="$REPO_DIR/experiments/${STUDY_ID}/dataset.yaml"
  if [[ ! -f "$DATASET" ]]; then
    echo "ERROR: dataset not found at $DATASET"
    exit 1
  fi

  mapfile -t CVE_IDS < <(grep '^ *- ' "$DATASET" | awk '{print $2}' | grep -v '/')
  if [[ ${#CVE_IDS[@]} -eq 0 ]]; then
    echo "ERROR: no CVEs found in $DATASET"
    exit 1
  fi

  info "Building secbench Docker images for ${#CVE_IDS[@]} CVEs in $STUDY_ID..."
  # newgrp not available in non-login shells; use sg to run docker without sudo
  # after the group membership was just added. Fall back to sudo docker if sg fails.
  _run_build() {
    cd "$REPO_DIR"
    deployment/build-secbench-tools.sh "${CVE_IDS[@]}"
  }
  if sg docker -c "$(declare -f _run_build); _run_build" 2>/dev/null; then
    success "All secbench images built"
  else
    info "sg docker failed (group not yet active in this shell); falling back to sudo docker"
    # Patch PATH so uv is visible inside sudo
    sudo env PATH="$PATH" bash -c "
      cd '$REPO_DIR'
      deployment/build-secbench-tools.sh ${CVE_IDS[*]}
    "
    success "All secbench images built (via sudo)"
  fi
else
  echo
  echo "  No --study given; skipping secbench image build."
  echo "  Build manually before running the matrix:"
  echo "    grep '^ *- ' experiments/<study>/dataset.yaml \\"
  echo "      | awk '{print \$2}' \\"
  echo "      | xargs deployment/build-secbench-tools.sh"
fi

# ─── Final validation ──────────────────────────────────────────────────────────

echo
section "Validation"

PASS=true

check() {
  local label="$1"; shift
  if "$@" &>/dev/null; then
    success "$label"
  else
    echo "✗ $label — FAILED"
    PASS=false
  fi
}

check "uv"              uv --version
check "docker daemon"   sudo docker info
check "claude CLI"      claude --version
check "Python env"      uv run python -c "import core"
check "Postgres"        sudo docker exec arise-db pg_isready -U "${POSTGRES_USER:-arise}" -d "${POSTGRES_DB:-arise_events}"

echo
if [[ "$PASS" == "true" ]]; then
  echo "════════════════════════════════════════════════════"
  echo "  Setup complete. Next steps:"
  echo ""
  echo "  1. Re-login (or run 'newgrp docker') so docker"
  echo "     commands work without sudo."
  echo ""
  if [[ -z "$STUDY_ID" ]]; then
    echo "  2. Build secbench images for your study CVEs:"
    echo "     grep '^ *- ' experiments/<study>/dataset.yaml \\"
    echo "       | awk '{print \$2}' \\"
    echo "       | xargs deployment/build-secbench-tools.sh"
    echo ""
    echo "  3. Run the experiment matrix:"
  else
    echo "  2. Run the experiment matrix:"
  fi
  echo "     cd ${REPO_DIR}"
  echo "     set -a && source deployment/.env && set +a"
  echo "     uv run python -m experiments.shared.scripts.run_matrix \\"
  if [[ -n "$STUDY_ID" ]]; then
    echo "       --study ${STUDY_ID} --cells A1,A2 --parallel 8"
  else
    echo "       --study <study-id> --cells A1,A2 --parallel 8"
  fi
  echo "════════════════════════════════════════════════════"
else
  echo "Setup finished with errors above. Fix them before running experiments."
  exit 1
fi
