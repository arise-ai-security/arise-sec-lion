#!/usr/bin/env bash
# Smoke run for the active B3 direct-compact ablation.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source deployment/.env; set +a
export POSTGRES_HOST=localhost
export ARISE_SUBPROCESS_TIMEOUT_SECONDS=15600
exec uv run python -m experiments.shared.scripts.run_matrix \
  --study b3-direct-compact \
  --tasks openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --parallel 2 \
  --replicates 1 "$@"
