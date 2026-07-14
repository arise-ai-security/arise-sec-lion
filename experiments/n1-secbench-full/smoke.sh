#!/usr/bin/env bash
# Sequential N1 smoke over two SEC-bench eval instances.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set in the process environment}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in the process environment}"

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-arise}"
export POSTGRES_DB="${POSTGRES_DB:-arise_events}"
exec uv run python -m experiments.shared.scripts.run_matrix \
  --study n1-secbench-full \
  --tasks openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --parallel 1 \
  --replicates 1 "$@"

