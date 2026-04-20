#!/usr/bin/env bash
# Usage: ./scripts/run_exp_swebench.sh <config.yaml> <basepath> <output_dir> id1 id2 ...
#
# Runs SWE-bench instances sequentially through arise-sec-lion.
# Each instance: read task file -> run system -> collect git diff patch.
set -uo pipefail
cd /Users/garfield/PycharmProjects/arise-sec-lion

set -a; source deployment/.env; set +a
export POSTGRES_HOST=localhost

CONFIG="$1"; BASEPATH="$2"; OUTPUT_DIR="$3"; shift 3
mkdir -p "${OUTPUT_DIR}/patches"
RESULTS_FILE="${OUTPUT_DIR}/patches/batch_results_$$.log"

for INSTANCE_ID in "$@"; do
    TASK_FILE="${OUTPUT_DIR}/tasks/${INSTANCE_ID}.txt"
    PATCH_FILE="/Users/garfield/PycharmProjects/arise-sec-lion/${OUTPUT_DIR}/patches/${INSTANCE_ID}.diff"

    echo "=== Starting: ${INSTANCE_ID} (config=${CONFIG}) ==="

    if [ ! -f "$TASK_FILE" ]; then
        echo "${INSTANCE_ID}|ERROR|task_not_found|0" >> "$RESULTS_FILE"
        echo "SKIP: task file not found: ${TASK_FILE}"
        continue
    fi

    TASK_CONTENT=$(cat "$TASK_FILE")

    EXIT_STATUS=0
    perl -e 'alarm shift; exec @ARGV' 2700 \
        python main.py -c "$CONFIG" run "$TASK_CONTENT" 2>&1 \
        || EXIT_STATUS=$?

    PATCH_SIZE=0
    if [ -d "${BASEPATH}/${INSTANCE_ID}" ]; then
        cd "${BASEPATH}/${INSTANCE_ID}"
        git diff > "$PATCH_FILE" 2>/dev/null || true
        if [ -f "$PATCH_FILE" ]; then
            PATCH_SIZE=$(wc -c < "$PATCH_FILE" | tr -d ' ')
        fi
        cd /Users/garfield/PycharmProjects/arise-sec-lion
    else
        echo "WARNING: ${BASEPATH}/${INSTANCE_ID} not found"
    fi

    STATUS="OK"
    [ "$EXIT_STATUS" -ne 0 ] && STATUS="FAIL(${EXIT_STATUS})"

    echo "${INSTANCE_ID}|${STATUS}|${PATCH_SIZE}" >> "$RESULTS_FILE"
    echo "=== Done: ${INSTANCE_ID} | ${STATUS} | patch=${PATCH_SIZE}b ==="
done

echo "--- Batch complete. Results: ---"
cat "$RESULTS_FILE"
