#!/usr/bin/env bash
# Run the N1, B3, and B4 studies concurrently on the validation instances for one
# optimization cycle. Usage: experiments/run-cycle.sh <cycle-label>
# Logs land in temp/<cycle-label>/<study>.log. N1 uses run_batch.py directly;
# B3/B4 retain their study-local smoke wrappers.
set -euo pipefail
cd "$(dirname "$0")/.."
label="${1:?usage: run-cycle.sh <cycle-label>}"
mkdir -p "temp/${label}"

studies=(n1-secbench-full b3-direct-compact b4-boss-manager-worker)
pids=()
for study in "${studies[@]}"; do
  if [[ "${study}" == "n1-secbench-full" ]]; then
    uv run python experiments/n1-secbench-full/run_batch.py \
      --instances openexr.cve-2020-16589,faad2.cve-2018-20196 \
      --batch-size 2 --parallel 2 > "temp/${label}/${study}.log" 2>&1 &
  else
    "experiments/${study}/smoke.sh" > "temp/${label}/${study}.log" 2>&1 &
  fi
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
