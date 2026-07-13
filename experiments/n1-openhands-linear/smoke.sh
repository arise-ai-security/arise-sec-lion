#!/usr/bin/env bash
# Smoke run for n1-openhands-linear — the 2 curated smoke instances, --parallel 2.
# Tasks pinned from experiments/shared/datasets/cve50-2026-06-09.lock.yaml (smoke_tasks).
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set in the process environment}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in the process environment}"

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-arise}"
export POSTGRES_DB="${POSTGRES_DB:-arise_events}"
exec uv run python -m experiments.shared.scripts.run_matrix \
  --study n1-openhands-linear \
  --tasks openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --parallel 2 \
  --replicates 1 "$@"
