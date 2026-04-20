#!/usr/bin/env bash
# Usage: ./scripts/run_exp_secbench.sh <config.yaml> <output_dir> fixture1.json fixture2.json ...
#
# Runs SEC-bench instances sequentially through arise-sec-lion with --domain security.
# Each instance: load CVE fixture -> run system -> log result.
set -uo pipefail
cd /Users/garfield/PycharmProjects/arise-sec-lion

set -a; source deployment/.env; set +a
export POSTGRES_HOST=localhost

CONFIG="$1"; OUTPUT_DIR="$2"; shift 2
mkdir -p "${OUTPUT_DIR}/patches"
RESULTS_FILE="${OUTPUT_DIR}/patches/batch_results_$$.log"

TASK="Analyze the security vulnerability described in the provided CVE context. \
Reproduce the bug using the sanitizer, identify the root cause in the source code, \
and implement a minimal fix. Verify the fix eliminates the sanitizer report without \
breaking existing functionality."

for FIXTURE in "$@"; do
    INSTANCE_ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['instance_id'])" "$FIXTURE")
    echo "=== Starting: ${INSTANCE_ID} (config=${CONFIG}) ==="

    EXIT_STATUS=0
    perl -e 'alarm shift; exec @ARGV' 2700 \
        python main.py -c "$CONFIG" \
        run "$TASK" --domain security --domain-context-file "$FIXTURE" 2>&1 \
        || EXIT_STATUS=$?

    STATUS="OK"
    [ "$EXIT_STATUS" -ne 0 ] && STATUS="FAIL(${EXIT_STATUS})"

    echo "${INSTANCE_ID}|${STATUS}" >> "$RESULTS_FILE"
    echo "=== Done: ${INSTANCE_ID} | ${STATUS} ==="
done

echo "--- Batch complete. Results: ---"
cat "$RESULTS_FILE"
