#!/usr/bin/env bash
# Run all four cost-family cells concurrently on the smoke instances for one
# optimization cycle. Usage: experiments/run-cycle.sh <cycle-label>
# Logs land in temp/<cycle-label>/<study>.log. Each study uses --parallel 2.
set -euo pipefail
cd "$(dirname "$0")/.."
label="${1:?usage: run-cycle.sh <cycle-label>}"
mkdir -p "temp/${label}"

studies=(n1-openhands-linear n2-openhands-subagents b3-boss-bef-direct b4-boss-manager-worker)
pids=()
for study in "${studies[@]}"; do
  "experiments/${study}/smoke.sh" > "temp/${label}/${study}.log" 2>&1 &
  pid="$!"; pids+=("$pid")
  echo "launched ${study} (pid ${pid})"
done

status=0
for i in "${!studies[@]}"; do
  if wait "${pids[$i]}"; then echo "DONE  ${studies[$i]}"
  else echo "FAIL  ${studies[$i]} (see temp/${label}/${studies[$i]}.log)"; status=1; fi
done
echo "cycle ${label} finished (status ${status})"
exit "$status"
