#!/usr/bin/env bash
# Maintainer-only: build, validate, and publish the one shared N1 tool payload.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAYLOAD_DOCKERFILE="$ROOT_DIR/deployment/secbench-tools-payload.Dockerfile"
CONSUMER_DOCKERFILE="$ROOT_DIR/deployment/secbench-tools.Dockerfile"
PAYLOAD_LOCK="$ROOT_DIR/deployment/secbench-tools-payload.lock"
REFERENCE_BASE="${REFERENCE_BASE:-hwiwonlee/secb.eval.x86_64.openjpeg.cve-2024-56827@sha256:d17f6788f2915d31b21c8af1b3fa9b68ee71447247a1f97d4cec1217a06b0c5a}"
VALIDATION_IMAGE="arise-secbench-payload-validation:local"

# shellcheck disable=SC1090
. "$PAYLOAD_LOCK"

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--push" ]; }; then
  printf 'Usage: deployment/publish-secbench-tools-payload.sh [--push]\n' >&2
  exit 2
fi

printf 'Building one CVE-independent payload: %s\n' "$SHARED_TOOLS_TAG"
docker build \
  --platform linux/amd64 \
  --provenance=false \
  -f "$PAYLOAD_DOCKERFILE" \
  -t "$SHARED_TOOLS_TAG" \
  "$ROOT_DIR"

test "$(docker image inspect --format '{{.Architecture}}' "$SHARED_TOOLS_TAG")" = "amd64"

printf 'Validating the payload on the pinned N1 ABI...\n'
docker build \
  --platform linux/amd64 \
  --provenance=false \
  -f "$CONSUMER_DOCKERFILE" \
  --build-arg "BASE_IMAGE=$REFERENCE_BASE" \
  --build-arg "SHARED_TOOLS_IMAGE=$SHARED_TOOLS_TAG" \
  --build-arg "SHARED_TOOLS_GENERATION=$SHARED_TOOLS_GENERATION" \
  -t "$VALIDATION_IMAGE" \
  "$ROOT_DIR"

docker run --rm --platform linux/amd64 --entrypoint /bin/bash "$VALIDATION_IMAGE" -lc '
  set -e
  test "$(node --version)" = "v20.20.2"
  test "$(claude --version | awk "{print \$1}")" = "2.1.173"
  test "$(/opt/arise-mcp/venv/bin/python -c \
    "import platform; print(platform.python_version())")" = "3.12.5"
  test "$(/opt/arise-mcp/venv/bin/python -c \
    "from importlib.metadata import version; print(version(\"mcp\"))")" = "1.27.2"
  /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
  /opt/arise-mcp/venv/bin/python -c \
    "from cryptography.hazmat.bindings._rust import openssl"
  for command in valgrind gdb cppcheck strace ltrace cflow jq; do
    command -v "$command" >/dev/null
  done
  valgrind --version >/dev/null
  gdb --version >/dev/null
  cppcheck --version >/dev/null
  strace --version >/dev/null
  ltrace --version >/dev/null
  cflow --version >/dev/null
  jq --version >/dev/null
'
test "$(docker image inspect --format \
  '{{index .Config.Labels "io.arise.secbench.tools"}}' "$VALIDATION_IMAGE")" \
  = "$SHARED_TOOLS_GENERATION"
docker image rm "$VALIDATION_IMAGE" >/dev/null

if [ "${1:-}" != "--push" ]; then
  printf 'Payload validated locally. Re-run with --push after docker login.\n'
  exit 0
fi

docker push "$SHARED_TOOLS_TAG"
digest="$(
  docker image inspect --format '{{index .RepoDigests 0}}' "$SHARED_TOOLS_TAG"
)"
printf 'Published: %s\n' "$digest"
printf 'Pin SHARED_TOOLS_IMAGE to this digest in %s before handoff.\n' "$PAYLOAD_LOCK"
