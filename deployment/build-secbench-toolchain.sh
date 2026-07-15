#!/usr/bin/env bash
# Build and push the heavy SEC-bench security tool layers exactly once.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT_DIR/deployment/secbench-tools.Dockerfile"
REFERENCE_BASE="${REFERENCE_BASE:-hwiwonlee/secb.eval.x86_64.openjpeg.cve-2024-56827@sha256:d17f6788f2915d31b21c8af1b3fa9b68ee71447247a1f97d4cec1217a06b0c5a}"
TOOLCHAIN_TAG="${TOOLCHAIN_TAG:-toolchain-focal-amd64-v1}"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-secbench-toolchain.sh REGISTRY

Build the security tools once on the pinned Ubuntu 20.04/amd64 reference base,
smoke it, and push REGISTRY/secb-tools:toolchain-focal-amd64-v1.

Run `docker login` for REGISTRY first. Override TOOLCHAIN_TAG or REFERENCE_BASE
only when intentionally publishing a new immutable toolchain generation.

Example:
  deployment/build-secbench-toolchain.sh cheshire0814
EOF
}

if [ "$#" -ne 1 ]; then
  usage >&2
  exit 2
fi

REGISTRY="${1%/}"
IMAGE="$REGISTRY/secb-tools:$TOOLCHAIN_TAG"

printf 'Building one shared toolchain: %s\n' "$IMAGE"
docker build \
  --platform linux/amd64 \
  --provenance=false \
  -f "$DOCKERFILE" \
  --build-arg "BASE_IMAGE=$REFERENCE_BASE" \
  -t "$IMAGE" \
  "$ROOT_DIR"

docker run --rm --platform linux/amd64 --entrypoint /bin/bash "$IMAGE" -lc '
  set -e
  test "$(claude --version | awk "{print \$1}")" = "2.1.173"
  test "$(node --version)" = "v20.20.2"
  /opt/arise-mcp/venv/bin/pip show mcp | grep -q "^Version: 1.27.2$"
  test "$(/opt/arise-mcp/venv/bin/python -c "import sys; print(sys.base_prefix)")" = "/opt/arise-python"
  /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
  valgrind --version
'

docker push "$IMAGE"
printf '\nPublished %s\n' "$IMAGE"
printf 'Use this tag as --tool-image for deployment/publish-secbench-tools.sh.\n'
