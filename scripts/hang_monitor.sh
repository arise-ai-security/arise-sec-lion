#!/usr/bin/env bash
set -euo pipefail

# Lightweight run hang monitor for Arise SEC-bench orchestration.
# Usage:
#   scripts/hang_monitor.sh <run_id> [stale_sec]
# Example:
#   scripts/hang_monitor.sh d18c048d-e1b9-4ed6-bee2-cab11a71b3b2 600

RUN_ID="${1:-}"
STALE_SEC="${2:-600}"

if [[ -z "${RUN_ID}" ]]; then
  echo "usage: $0 <run_id> [stale_sec]" >&2
  exit 1
fi

PSQL=(PGPASSWORD='Arise' psql -h localhost -U arise -d arise_events -P pager=off)

echo "=== Run Heartbeat ==="
/bin/bash -lc "${PSQL[*]} -c \"with recursive tree as ( select '${RUN_ID}'::uuid as id union all select e.aggregate_id from events e join tree t on (e.payload->>'parent_id')::uuid=t.id where e.event_type='AgentCreated' ) select now() at time zone 'utc' as now_utc, max(occurred_at) as last_event_at, extract(epoch from ((now() at time zone 'utc') - max(occurred_at)))::int as lag_sec, count(*) as events from events where aggregate_id in (select distinct id from tree);\""

echo
echo "=== Recent Events (tail 30) ==="
/bin/bash -lc "${PSQL[*]} -c \"with recursive tree as ( select '${RUN_ID}'::uuid as id union all select e.aggregate_id from events e join tree t on (e.payload->>'parent_id')::uuid=t.id where e.event_type='AgentCreated' ) select e.aggregate_id,e.event_type,e.occurred_at,left(coalesce(e.payload->>'reason',e.payload->>'error',e.payload->>'new_status',e.payload->>'role',''),120) as detail from events e where e.aggregate_id in (select distinct id from tree) order by e.occurred_at desc limit 30;\""

echo
echo "=== Start-Loop Suspects (>=2 starts, 0 thoughts) ==="
/bin/bash -lc "${PSQL[*]} -c \"with recursive tree as ( select '${RUN_ID}'::uuid as id union all select e.aggregate_id from events e join tree t on (e.payload->>'parent_id')::uuid=t.id where e.event_type='AgentCreated' ), spans as ( select aggregate_id, count(*) filter (where event_type='AgentExecutionStarted') as starts, count(*) filter (where event_type='ThoughtCaptured') as thoughts, max(occurred_at) as last_event_at from events where aggregate_id in (select distinct id from tree) group by aggregate_id ) select aggregate_id,starts,thoughts,last_event_at from spans where starts >= 2 and thoughts = 0 order by starts desc, last_event_at desc;\""

echo
echo "=== Stale Agents (>${STALE_SEC}s since last event) ==="
/bin/bash -lc "${PSQL[*]} -c \"with recursive tree as ( select '${RUN_ID}'::uuid as id union all select e.aggregate_id from events e join tree t on (e.payload->>'parent_id')::uuid=t.id where e.event_type='AgentCreated' ), latest as ( select aggregate_id, max(occurred_at) as last_event_at from events where aggregate_id in (select distinct id from tree) group by aggregate_id ) select aggregate_id,last_event_at,extract(epoch from ((now() at time zone 'utc') - last_event_at))::int as lag_sec from latest where extract(epoch from ((now() at time zone 'utc') - last_event_at)) > ${STALE_SEC} order by lag_sec desc;\""
