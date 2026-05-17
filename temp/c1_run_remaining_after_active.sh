#!/usr/bin/env bash
set -euo pipefail

cd /Users/garfield/PycharmProjects/arise-sec-lion

ACTIVE_PIDS=(25464 25469)

any_active_pid() {
  local pid
  for pid in "${ACTIVE_PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

while any_active_pid; do
  printf '[%s] waiting for active first-wave C1 runs to finish\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sleep 60
done

TASKS=$(
  python - <<'PY'
import json
s = json.load(open("temp/c1-batch-state.json"))
tasks = [s["selected_tasks"][1], *s["selected_tasks"][3:20]]
print(",".join(tasks))
PY
)

set -a
source deployment/.env
set +a

export POSTGRES_HOST=localhost
export ARISE_DEBUG_POLL=1
export ARISE_DEBUG_POLL_INTERVAL_S=60

uv run python -m experiments.shared.scripts.run_matrix \
  --study c1-batch-autogen \
  --cells C1 \
  --tasks "$TASKS" \
  --replicates 1 \
  --parallel 2 \
  --no-render
