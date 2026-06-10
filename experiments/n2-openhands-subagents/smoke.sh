#!/usr/bin/env bash
# Smoke run for n2-openhands-subagents — the 2 curated smoke instances, --parallel 2.
# Tasks pinned from experiments/shared/datasets/cve50-2026-06-09.lock.yaml (smoke_tasks).
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source deployment/.env; set +a
# deployment/.env points POSTGRES_HOST at the compose-internal name `db`;
# the matrix runs main.py on the host, so force the published port instead.
export POSTGRES_HOST=localhost
exec uv run python -m experiments.shared.scripts.run_matrix \
  --study n2-openhands-subagents \
  --tasks openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --parallel 2 \
  --replicates 1 "$@"
