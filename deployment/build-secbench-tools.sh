#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT_DIR/deployment/secbench-tools.Dockerfile"
PAYLOAD_LOCK="$ROOT_DIR/deployment/secbench-tools-payload.lock"

if [ ! -f "$PAYLOAD_LOCK" ]; then
  printf 'Missing shared-tools lock: %s\n' "$PAYLOAD_LOCK" >&2
  exit 2
fi
# shellcheck disable=SC1090
. "$PAYLOAD_LOCK"
SHARED_TOOLS_IMAGE="${ARISE_SECBENCH_TOOLS_PAYLOAD_IMAGE:-$SHARED_TOOLS_IMAGE}"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-secbench-tools.sh <input> [more inputs...]

Each input may be:
  - a CVE JSON file path                      (e.g. path/to/cve.json)
  - a full hwiwonlee base image name          (e.g. hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838)
  - a <project>.<cve> shorthand               (e.g. openjpeg.cve-2016-7445) — expanded to
                                              hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch

Every local build automatically pulls the locked shared tools payload. After
assembly, the script validates the image:
  - node --version
  - claude --version
  - /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
  - valgrind --version
A failed validation aborts so a broken image never runs an experiment.

Examples:
  deployment/build-secbench-tools.sh openjpeg.cve-2016-7445
  deployment/build-secbench-tools.sh path/to/cve.json
  deployment/build-secbench-tools.sh hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838
EOF
}

if [ "$#" -eq 0 ]; then
  usage
  exit 1
fi

resolve_base_image() {
  local input="$1"
  if [ -f "$input" ]; then
    uv run python - "$input" <<'PY'
import sys
from plugins.security import CVEInstance

cve = CVEInstance.from_json_file(sys.argv[1])
print(cve.docker_image)
PY
    return
  fi
  # <project>.<cve> shorthand (no slash, contains exactly one dot-separated cve token)
  if [[ "$input" != */* && "$input" == *cve-* ]]; then
    printf 'hwiwonlee/secb.eval.x86_64.%s:patch\n' "$input"
    return
  fi
  printf '%s\n' "$input"
}

validate_image_tools() {
  local image="$1"
  printf 'Validating shared tools in %s ...\n' "$image"
  docker run --rm --platform linux/amd64 --entrypoint /bin/bash "$image" -lc '
    set -e
    test "$(node --version)" = "v20.20.2"
    test "$(claude --version | awk "{print \$1}")" = "2.1.173"
    test "$(/opt/arise-mcp/venv/bin/python -c \
      "import platform; print(platform.python_version())")" = "3.12.5"
    test "$(/opt/arise-mcp/venv/bin/python -c \
      "from importlib.metadata import version; print(version(\"mcp\"))")" = "1.27.2"
    /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print(\"MCP OK\")"
    /opt/arise-mcp/venv/bin/python -c \
      "from cryptography.hazmat.bindings._rust import openssl; print(\"native wheels OK\")"
    valgrind --version | head -1
  '
}

ensure_shared_tools() {
  local architecture
  architecture="$(
    docker image inspect --format '{{.Architecture}}' "$SHARED_TOOLS_IMAGE" 2>/dev/null
  )" || architecture=""
  if [ "$architecture" != "amd64" ]; then
    printf 'Pulling one shared tools payload: %s\n' "$SHARED_TOOLS_IMAGE"
    docker pull --platform linux/amd64 "$SHARED_TOOLS_IMAGE"
    architecture="$(
      docker image inspect --format '{{.Architecture}}' "$SHARED_TOOLS_IMAGE"
    )"
  fi
  if [ "$architecture" != "amd64" ]; then
    printf 'Shared tools payload is not linux/amd64: %s\n' "$SHARED_TOOLS_IMAGE" >&2
    return 1
  fi
}

resolve_target_image() {
  local base_image="$1"
  uv run python - "$base_image" <<'PY'
import sys
from plugins.security import resolve_secbench_image

print(resolve_secbench_image(sys.argv[1], security_tools_enabled=True))
PY
}

local_target_ready() {
  local image="$1"
  local metadata
  metadata="$(
    docker image inspect \
      --format '{{.Architecture}}|{{.Descriptor.MediaType}}|{{index .Config.Labels "io.arise.secbench.tools"}}' \
      "$image" 2>/dev/null
  )" || return 1
  [[ "$metadata" == amd64\|*\|"$SHARED_TOOLS_GENERATION" ]] || return 1
  [[ "$metadata" != *image.index* && "$metadata" != *manifest.list* ]]
}

FORCE_REBUILD="${FORCE_REBUILD:-0}"
SKIP_IMAGE_VALIDATION="${SKIP_IMAGE_VALIDATION:-${SKIP_SMOKE:-0}}"
shared_tools_ready=0

for input in "$@"; do
  base_image="$(resolve_base_image "$input")"
  target_image="$(resolve_target_image "$base_image")"

  if [ "$FORCE_REBUILD" = "0" ] && local_target_ready "$target_image"; then
    printf 'Skipping existing image %s (set FORCE_REBUILD=1 to override)\n' "$target_image"
  else
    if [ "$shared_tools_ready" -eq 0 ]; then
      ensure_shared_tools
      shared_tools_ready=1
    fi
    printf 'Building %s from %s\n' "$target_image" "$base_image"
    docker build \
      --platform linux/amd64 \
      --provenance=false \
      -f "$DOCKERFILE" \
      --build-arg "BASE_IMAGE=$base_image" \
      --build-arg "SHARED_TOOLS_IMAGE=$SHARED_TOOLS_IMAGE" \
      --build-arg "SHARED_TOOLS_GENERATION=$SHARED_TOOLS_GENERATION" \
      -t "$target_image" \
      "$ROOT_DIR"
  fi

  if [ "$SKIP_IMAGE_VALIDATION" = "0" ]; then
    validate_image_tools "$target_image"
  fi
done
