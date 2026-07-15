#!/usr/bin/env bash
# Smoke run for the active B3 direct-compact ablation.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set in the process environment}"

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-arise}"
export POSTGRES_DB="${POSTGRES_DB:-arise_events}"
export ARISE_SUBPROCESS_TIMEOUT_SECONDS="${ARISE_SUBPROCESS_TIMEOUT_SECONDS:-15600}"
exec uv run python -m experiments.shared.scripts.run_matrix \
  --study b3-direct-compact \
  --tasks openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --parallel 2 \
  --replicates 1 "$@"
