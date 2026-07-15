#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT_DIR/deployment/secbench-tools.Dockerfile"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-secbench-tools.sh <input> [more inputs...]

Each input may be:
  - a CVE JSON file path                      (e.g. path/to/cve.json)
  - a full hwiwonlee base image name          (e.g. hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838)
  - a <project>.<cve> shorthand               (e.g. openjpeg.cve-2016-7445) — expanded to
                                              hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch

After each build, the script smokes the image:
  - claude --version
  - /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
  - valgrind --version
A failed smoke aborts so a broken image never sits in the local cache.

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

smoke_image() {
  local image="$1"
  printf 'Smoking %s ...\n' "$image"
  docker run --rm --entrypoint /bin/bash "$image" -lc '
    set -e
    claude --version
    /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print(\"MCP OK\")"
    valgrind --version | head -1
  '
}

resolve_target_image() {
  local base_image="$1"
  uv run python - "$base_image" <<'PY'
import sys
from plugins.security import resolve_secbench_image

print(resolve_secbench_image(sys.argv[1], security_tools_enabled=True))
PY
}

FORCE_REBUILD="${FORCE_REBUILD:-0}"
SKIP_SMOKE="${SKIP_SMOKE:-0}"

for input in "$@"; do
  base_image="$(resolve_base_image "$input")"
  target_image="$(resolve_target_image "$base_image")"

  if [ "$FORCE_REBUILD" = "0" ] && docker image inspect "$target_image" >/dev/null 2>&1; then
    printf 'Skipping existing image %s (set FORCE_REBUILD=1 to override)\n' "$target_image"
  else
    printf 'Building %s from %s\n' "$target_image" "$base_image"
    docker build \
      --platform linux/amd64 \
      -f "$DOCKERFILE" \
      --build-arg "BASE_IMAGE=$base_image" \
      -t "$target_image" \
      "$ROOT_DIR"
  fi

  if [ "$SKIP_SMOKE" = "0" ]; then
    smoke_image "$target_image"
  fi
done
