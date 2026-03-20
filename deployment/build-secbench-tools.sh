#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="$ROOT_DIR/deployment/secbench-tools.Dockerfile"

usage() {
  cat <<'EOF'
Usage:
  deployment/build-secbench-tools.sh <cve-json-or-base-image> [more inputs...]

Examples:
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
  printf '%s\n' "$input"
}

resolve_target_image() {
  local base_image="$1"
  uv run python - "$base_image" <<'PY'
import sys
from plugins.security import resolve_secbench_image

print(resolve_secbench_image(sys.argv[1], security_tools_enabled=True))
PY
}

for input in "$@"; do
  base_image="$(resolve_base_image "$input")"
  target_image="$(resolve_target_image "$base_image")"

  if docker image inspect "$target_image" >/dev/null 2>&1; then
    printf 'Skipping existing image %s\n' "$target_image"
    continue
  fi

  printf 'Building %s from %s\n' "$target_image" "$base_image"
  docker build \
    -f "$DOCKERFILE" \
    --build-arg "BASE_IMAGE=$base_image" \
    -t "$target_image" \
    "$ROOT_DIR"
done
