#!/bin/bash
# SEC-bench Sequential Runner with Worker Limit Enforcement
# Runs instances one at a time, kills if workers exceed limit

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Config
WORKER_MODEL="${WORKER_MODEL:-claude-sonnet-4-20250514}"
WORKER_TOOL="${WORKER_TOOL:-claude_code}"
MAX_WORKERS="${MAX_WORKERS:-24}" # change to MAX_WORKERS="${MAX_WORKERS:-35}" for gpac instances
POLL_INTERVAL="${POLL_INTERVAL:-10}"
STALE_THRESHOLD="${STALE_THRESHOLD:-30}"  # Stop monitoring if worker count unchanged for N polls
LOG_DIR="./logs"

# Default instance list (override with arguments)
INSTANCES=(
#    "openjpeg.cve-2021-3575"
#    "exiv2.cve-2017-14857"
#    "exiv2.cve-2017-14859"
#    "exiv2.cve-2017-14864"
#    "exiv2.cve-2017-17669"
#    "exiv2.cve-2017-17723"
    ####
#    "exiv2.cve-2017-18005"
#    "exiv2.cve-2018-17229"
#    "exiv2.cve-2018-17230"
#    "exiv2.cve-2018-19607"
##    "exiv2.cve-2020-18899"
#    "libarchive.cve-2017-14503"
#    "libarchive.cve-2019-11463"
#    "libarchive.cve-2020-21674"
#    "matio.cve-2019-20017"
#    "matio.cve-2019-20018"
#    "matio.cve-2019-9032"
#    "matio.cve-2019-9035"

#    "matio.cve-2020-19497"

#    "md4c.cve-2018-11545"
#    "md4c.cve-2020-26148"
#    "md4c.cve-2021-30027"
#    "yara.cve-2016-10211"
#    "yara.cve-2017-5924"
#    "yara.cve-2023-40857"
#    "upx.cve-2017-15056"
#    "upx.cve-2020-27787"
#    "upx.cve-2023-23457"

    # Delegate
    "jq.cve-2023-50246"
    "openjpeg.cve-2016-7445"
    "openjpeg.cve-2016-10507"
    "openjpeg.cve-2017-14041"
    "openjpeg.cve-2024-56827"
    "libplist.cve-2017-5545"
    "libsndfile.cve-2018-19432"
    "qpdf.cve-2021-36978"
    "readstat.cve-2018-5698"
    "yaml-cpp.cve-2018-20574"
    "njs.cve-2022-32414"
    "njs.cve-2019-13617"
    "njs.cve-2020-24348"
    "mruby.cve-2018-10199"
    "mruby.cve-2022-0570"
    "libredwg.cve-2020-21814"
)

log()   { echo "[$(date '+%H:%M:%S')] $1"; }
error() { echo "[ERROR] $1" >&2; }

# Get leaf (worker) node count for a BOSS agent
get_worker_count() {
    local boss_id=$1
    docker compose --profile local exec -T db psql -U arise -d arise_events -t -c "
        WITH RECURSIVE tree AS (
            SELECT aggregate_id FROM events
            WHERE aggregate_id = '${boss_id}' AND event_type = 'AgentCreated'
            UNION ALL
            SELECT e.aggregate_id FROM events e
            JOIN tree t ON (e.payload->>'parent_id')::uuid = t.aggregate_id
            WHERE e.event_type = 'AgentCreated'
        )
        SELECT COUNT(*) FROM tree t
        WHERE NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.event_type = 'AgentCreated'
              AND (e.payload->>'parent_id')::uuid = t.aggregate_id
        );
    " 2>/dev/null | tr -d ' \n' || echo "0"
}

# Get BOSS agent ID for instance
get_boss_id() {
    local instance=$1
    docker compose --profile local exec -T db psql -U arise -d arise_events -t -c "
        SELECT DISTINCT ac_boss.aggregate_id
        FROM events ta
        JOIN events ac_child ON ac_child.aggregate_id = ta.aggregate_id
                            AND ac_child.event_type = 'AgentCreated'
        JOIN events ac_boss ON ac_boss.aggregate_id::text = ac_child.payload->>'parent_id'
                           AND ac_boss.event_type = 'AgentCreated'
        WHERE ta.event_type = 'TaskAssigned'
          AND ta.payload->>'task_description' LIKE '%${instance}%'
          AND ac_boss.payload->>'parent_id' IS NULL
        ORDER BY ac_boss.aggregate_id DESC
        LIMIT 1;
    " 2>/dev/null | tr -d ' \n'
}

# Check if BOSS is still running
is_running() {
    local boss_id=$1
    local status=$(docker compose --profile local exec -T db psql -U arise -d arise_events -t -c "
        SELECT event_type FROM events
        WHERE aggregate_id = '${boss_id}'
        ORDER BY sequence_number DESC LIMIT 1;
    " 2>/dev/null | tr -d ' \n')
    [[ "$status" != "WorkCompleted" && "$status" != "WorkFailed" ]]
}

# Delete all events for an instance
delete_events() {
    local instance=$1
    local boss_id=$(get_boss_id "$instance")

    if [ -z "$boss_id" ]; then
        log "No BOSS found for $instance"
        return 1
    fi

    log "Deleting events for $instance (BOSS: $boss_id)..."
    docker compose --profile local exec -T db psql -U arise -d arise_events -c "
        WITH RECURSIVE tree AS (
            SELECT aggregate_id FROM events
            WHERE aggregate_id = '${boss_id}' AND event_type = 'AgentCreated'
            UNION ALL
            SELECT e.aggregate_id FROM events e
            JOIN tree t ON (e.payload->>'parent_id')::uuid = t.aggregate_id
            WHERE e.event_type = 'AgentCreated'
        )
        DELETE FROM events WHERE aggregate_id IN (SELECT aggregate_id FROM tree);
    " 2>/dev/null
    log "Events deleted"
}

# Clean up containers and artifacts for instance
cleanup_instance() {
    local instance=$1

    # Stop ALL SEC-bench containers for this instance (may have multiple: builder, final, etc.)
    local cids
    cids=$(docker ps -a --format '{{.ID}} {{.Image}}' | grep "hwiwonlee/secb.eval.x86_64.${instance}" | awk '{print $1}')
    if [ -n "$cids" ]; then
        for cid in $cids; do
            # Clean worker isolation directories inside container before stopping
            # (Created by prompts/secbench/worker/builder.j2 - commit 088b54a)
            log "Cleaning /tmp/src-* in container $cid"
            docker exec "$cid" bash -c "rm -rf /tmp/src-*" 2>/dev/null || true
            log "Removing SEC-bench container $cid"
            docker stop "$cid" 2>/dev/null || true
            docker rm "$cid" 2>/dev/null || true
        done
    fi

    # Stop worker containers spawned by Claude Code (DooD pattern)
    # These are named like: claude-code-worker-*, arise-worker-*, or secb-worker-*
    local worker_cids
    worker_cids=$(docker ps -a --format '{{.ID}} {{.Names}}' | grep -E "(claude-code|arise-worker|secb-worker)" | awk '{print $1}')
    if [ -n "$worker_cids" ]; then
        for cid in $worker_cids; do
            log "Removing worker container $cid"
            docker stop "$cid" 2>/dev/null || true
            docker rm "$cid" 2>/dev/null || true
        done
    fi

    # Clean host artifacts
    rm -rf ../src/ 2>/dev/null || true
    rm -rf ../tmp/src-* 2>/dev/null || true  # Fixed: was missing glob wildcard
}

# Full cleanup: delete events + containers
full_cleanup() {
    local instance=$1
    delete_events "$instance" || true
    cleanup_instance "$instance"
}

# Run single instance
run_instance() {
    local instance=$1
    local log_file="${LOG_DIR}/${instance}.log"
    mkdir -p "$LOG_DIR"

    log "=== Starting: $instance ==="
    log "Max workers: $MAX_WORKERS, Poll: ${POLL_INTERVAL}s"

    # Start execution in background
    docker compose --profile local exec -T app python main.py run \
        "Run SEC-bench evaluation: setup environment, create exploit PoC, and develop patch" \
        --cve-file "/app/data/sec-bench/instances/${instance}.json" \
        --worker-model "$WORKER_MODEL" \
        --worker-tool "$WORKER_TOOL" > "$log_file" 2>&1 &
    local pid=$!

    # Wait for BOSS to be created
    sleep 5
    local boss_id=$(get_boss_id "$instance")
    local retries=0
    while [ -z "$boss_id" ] && [ $retries -lt 6 ]; do
        sleep 5
        boss_id=$(get_boss_id "$instance")
        retries=$((retries + 1))
    done

    if [ -z "$boss_id" ]; then
        error "Could not find BOSS agent for $instance"
        kill "$pid" 2>/dev/null || true
        return 1
    fi
    log "BOSS: $boss_id, PID: $pid"

    # Monitor loop
    local killed=false
    local prev_count=-1
    local stale_count=0
    while kill -0 "$pid" 2>/dev/null; do
        local count
        count=$(get_worker_count "$boss_id")

        # Track stale worker count
        if [ "$count" -eq "$prev_count" ]; then
            stale_count=$((stale_count + 1))
        else
            stale_count=0
            prev_count=$count
        fi

        log "Workers: $count / $MAX_WORKERS (stale: $stale_count/$STALE_THRESHOLD)"

        if [ "$count" -gt "$MAX_WORKERS" ]; then
            log "KILLING: Worker limit exceeded ($count > $MAX_WORKERS)"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 2
            kill -KILL "$pid" 2>/dev/null || true
            killed=true
            break
        fi

        # If worker count hasn't changed for STALE_THRESHOLD polls, stop monitoring
        if [ "$stale_count" -ge "$STALE_THRESHOLD" ]; then
            log "Worker count stable for $STALE_THRESHOLD polls, letting execution continue..."
            break
        fi

        sleep "$POLL_INTERVAL"
    done

    wait "$pid" 2>/dev/null || true

    if [ "$killed" = true ]; then
        log "Cleaning up killed run..."
        full_cleanup "$instance"
        log "=== KILLED: $instance ==="
        return 1
    fi

    log "Execution completed, running verification..."
    cat "$log_file"

    # Get container ID for verification
    local cid=$(grep -oP 'Container ID: \K[a-f0-9]+' "$log_file" 2>/dev/null | head -1)
    if [ -z "$cid" ]; then
        cid=$(docker ps -a --format '{{.ID}} {{.Image}}' | grep "hwiwonlee/secb.eval.x86_64.${instance}" | awk '{print $1}' | head -1)
    fi

    if [ -n "$cid" ]; then
        log "Verifying with container: $cid"
        python "$SCRIPT_DIR/verify_secbench.py" "$cid" --instance-id "$instance" || true
    else
        log "No container found for verification"
    fi

    # Cleanup after verification
    cleanup_instance "$instance"
    log "=== COMPLETED: $instance ==="
}

# Usage
usage() {
    cat <<EOF
Usage: $0 [COMMAND] [OPTIONS]

Commands:
    run [INSTANCE...]    Run instances (defaults to built-in list)
    delete INSTANCE      Delete events for instance and cleanup
    cleanup INSTANCE     Remove containers/artifacts only

Options:
    -w, --max-workers N  Kill if workers exceed N (default: $MAX_WORKERS)
    -h, --help           Show this help

Environment:
    MAX_WORKERS      Worker limit (default: 24)
    POLL_INTERVAL    Check interval in seconds (default: 10)
    STALE_THRESHOLD  Stop monitoring after N unchanged polls (default: 30)
    WORKER_MODEL     Model (default: claude-sonnet-4-20250514)
    WORKER_TOOL      Tool (default: claude_code)

Examples:
    $0 run openjpeg.cve-2021-3575 exiv2.cve-2017-14857
    $0 delete openjpeg.cve-2021-3575
    MAX_WORKERS=10 $0 run openjpeg.cve-2021-3575
EOF
}

# Signal handler
trap 'echo ""; log "Interrupted, cleaning up..."; jobs -p | xargs -r kill 2>/dev/null; exit 1' SIGINT SIGTERM

# Main
case "${1:-run}" in
    run)
        shift || true
        if [ $# -gt 0 ]; then
            INSTANCES=("$@")
        fi
        for instance in "${INSTANCES[@]}"; do
            run_instance "$instance" || true
            echo ""
        done
        log "All instances processed"
        python "$SCRIPT_DIR/verify_secbench.py" --summary 2>/dev/null || true
        ;;
    delete)
        [ -z "$2" ] && { error "Instance required"; exit 1; }
        full_cleanup "$2"
        ;;
    cleanup)
        [ -z "$2" ] && { error "Instance required"; exit 1; }
        cleanup_instance "$2"
        ;;
    -h|--help|help)
        usage
        ;;
    -w|--max-workers)
        MAX_WORKERS="$2"
        shift 2
        INSTANCES=("$@")
        for instance in "${INSTANCES[@]}"; do
            run_instance "$instance" || true
        done
        ;;
    *)
        error "Unknown command: $1"
        usage
        exit 1
        ;;
esac
