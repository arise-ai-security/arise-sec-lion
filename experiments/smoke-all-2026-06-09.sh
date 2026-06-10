#!/usr/bin/env bash
# Launch the 2-instance smoke for all four cost-family studies CONCURRENTLY —
# one run_matrix process per study, each with --parallel 2 (8 runs in flight).
# Per-study logs land in temp/smoke-2026-06-09/<study>.log.
#
# Safe to run concurrently: the matrix startup sweep only removes containers
# whose owning session PID is dead, and orphan result files older than 1h.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p temp/smoke-2026-06-09

studies=(
  n1-openhands-linear
  n2-openhands-subagents
  b3-boss-bef-direct
  b4-boss-manager-worker
)

pids=()
for study in "${studies[@]}"; do
  "experiments/${study}/smoke.sh" > "temp/smoke-2026-06-09/${study}.log" 2>&1 &
  pid="$!"
  pids+=("$pid")
  echo "launched ${study} (pid ${pid})"
done

status=0
for i in "${!studies[@]}"; do
  if wait "${pids[$i]}"; then
    echo "DONE  ${studies[$i]}"
  else
    echo "FAIL  ${studies[$i]} (see temp/smoke-2026-06-09/${studies[$i]}.log)"
    status=1
  fi
done
exit "$status"
