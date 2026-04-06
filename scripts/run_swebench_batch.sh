#!/usr/bin/env bash
# Usage: ./scripts/run_swebench_batch.sh instance_id1 instance_id2 ...
# Runs each instance sequentially, collects patches, logs results.

set -uo pipefail
cd /Users/garfield/PycharmProjects/arise-sec-lion

# Source environment variables
set -a
source deployment/.env
set +a
# Override Docker service name with localhost for local execution
export POSTGRES_HOST=localhost

RESULTS_FILE="output/patches/batch_results_$$.log"

for INSTANCE_ID in "$@"; do
    TASK_FILE="output/tasks/${INSTANCE_ID}.txt"
    PATCH_FILE="/Users/garfield/PycharmProjects/arise-sec-lion/output/patches/${INSTANCE_ID}.diff"

    echo "=== Starting: ${INSTANCE_ID} ==="

    if [ ! -f "$TASK_FILE" ]; then
        echo "${INSTANCE_ID}|ERROR|task file not found|0" >> "$RESULTS_FILE"
        continue
    fi

    TASK_CONTENT=$(cat "$TASK_FILE")

    # Run the system (use perl-based timeout for macOS compatibility)
    EXIT_STATUS=0
    perl -e 'alarm shift; exec @ARGV' 2400 python main.py -c config/experiment-swebench.yaml run "$TASK_CONTENT" 2>&1 || EXIT_STATUS=$?

    # Collect patch
    PATCH_SIZE=0
    if [ -d "/tmp/swebench/${INSTANCE_ID}" ]; then
        cd "/tmp/swebench/${INSTANCE_ID}"
        git diff > "$PATCH_FILE" 2>/dev/null || true
        if [ -f "$PATCH_FILE" ]; then
            PATCH_SIZE=$(wc -c < "$PATCH_FILE" | tr -d ' ')
        fi
        cd /Users/garfield/PycharmProjects/arise-sec-lion
    else
        echo "WARNING: /tmp/swebench/${INSTANCE_ID} not found"
    fi

    STATUS="OK"
    if [ "$EXIT_STATUS" -ne 0 ]; then
        STATUS="FAIL(${EXIT_STATUS})"
    fi

    echo "${INSTANCE_ID}|${STATUS}|${PATCH_SIZE}" >> "$RESULTS_FILE"
    echo "=== Done: ${INSTANCE_ID} | ${STATUS} | patch=${PATCH_SIZE} bytes ==="
done

echo "--- Batch complete. Results in ${RESULTS_FILE} ---"
cat "$RESULTS_FILE"
