# Full B1 Run — Anomaly Watch Prompt

## Context

- **Repo**: arise-sec-lion
- **Branch**: feature/experiments-rearchitecture
- **Cell**: B1 — hierarchical boss → manager → worker, `skip_judge=true`, `worker.tool=claude_code`, `worker.model=claude-sonnet-4-5-20250929`
- **Config**: `experiments/2026-05-13-my-study/configs/B1-ours-claude-noverifier.yaml`
- **DB**: PostgreSQL on `localhost:5432`, database `arise_events`, user `arise`. Load creds from `deployment/.env` then override `POSTGRES_HOST=localhost`.
- **Prior smoke**: Verified B1 worker path (container exec, container paths, no host leaks) on Build-Setup and Build-Compiler subtasks. Run was killed after verification. DB was truncated after that smoke.

---

## Task

Run one full B1 job end-to-end on `njs.cve-2022-28049` with parallelism and watch for anomalies throughout. Do **not** kill the run early — let it complete or time out naturally.

### Step 1 — Pre-flight

```bash
# Confirm DB is empty (or note existing event count as baseline)
source deployment/.env
PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U arise -d arise_events \
  -c "SELECT COUNT(*) FROM events;"

# Confirm no leftover secbench-worker containers
docker ps -a --filter "name=secbench-worker"

# Confirm SEC-bench image exists for njs
docker image inspect $(cat plugins/security/tests/fixtures/njs.cve-2022-28049.json | python3 -c "import sys,json; print(json.load(sys.stdin)['image'])") 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['Id'][:20], d[0]['RepoTags'])"
```

### Step 2 — Launch

Run from the **host** repo shell (not inside arise-app container):

```bash
set -a && source deployment/.env && set +a
POSTGRES_HOST=localhost \
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-05-13-my-study \
  --cells B1 \
  --tasks njs.cve-2022-28049 \
  --replicates 1 \
  --parallel 2 \
  --no-render \
  2>&1 | tee /tmp/b1-full-run.log &
echo "PID=$!"
```

Note the root_id from the log line `🤖 Agent <root_id>... created as boss`.

### Step 3 — Monitor loop

Poll every 60 seconds until `matrix complete` appears in the log or 120 minutes elapse. At each tick:

**A. Event store health**
```sql
-- Per-node summary
SELECT
  e.aggregate_id::text,
  (SELECT payload->>'role' FROM events
   WHERE aggregate_id=e.aggregate_id AND event_type='AgentExecutionStarted' LIMIT 1) as role,
  COUNT(*) as events,
  COUNT(*) FILTER (
    WHERE event_type='ThoughtCaptured' AND payload->>'output_type'='tool_use'
  ) as tool_calls,
  MAX(occurred_at)::time(0) as last_event
FROM events e
GROUP BY e.aggregate_id
ORDER BY MIN(e.occurred_at);
```

**B. Worker tool path check** (run whenever a new ThoughtCaptured with tool_use appears)
```sql
-- Flag any host path leaks in tool_use inputs
SELECT aggregate_id::text, sequence_number,
       payload->>'tool_name' as tool,
       LEFT(payload->>'content', 200) as content
FROM events
WHERE event_type='ThoughtCaptured'
  AND payload->>'output_type' = 'tool_use'
  AND (payload->>'content' LIKE '%/Users/%'
    OR payload->>'content' LIKE '%/private/var/%'
    OR payload->>'content' LIKE '%/var/folders/%'
    OR payload->>'content' LIKE '%runs/%')
ORDER BY occurred_at DESC LIMIT 10;
```

**C. Duplicate sequence check**
```sql
SELECT aggregate_id, sequence_number, COUNT(*)
FROM events
GROUP BY aggregate_id, sequence_number
HAVING COUNT(*) > 1;
```

**D. Container labels** (once per new secbench-worker container)
```bash
docker ps --filter "name=secbench-worker" -q | \
  xargs -I{} docker inspect {} --format '{{json .Config.Labels}}' | python3 -m json.tool
```

**E. Host process tree** — verify no model-triggered builds running on host
```bash
ps aux | grep -E "make|gcc|clang|npm|cargo|pytest" | grep -v grep
```

### Step 4 — Anomaly categories to watch

For each anomaly found, record: event_type, aggregate_id, sequence_number, payload excerpt, and time.

**B1 Identity**
- `skip_judge=true` → no event of type `JudgementRecorded` or similar should appear
- `worker.tool=claude_code` → all ThoughtCaptured worker events must have `stream=claude_sdk`
- No openhands, no A-cell docker-exec-flat path

**Worker locality — Invariant E(b)**
- Every Bash/shell command in a worker ThoughtCaptured must be routed through `docker exec` (the in-container wrapper) — not execute directly on host
- secbench-worker container must bear labels: `arise.root_id`, `arise.agent_id`, `arise.session_pid`, `arise.created_at`, `arise.instance_id`

**Path visibility**
- Worker tool inputs (Read, Write, Edit, Glob, Grep, Bash) must use `/src`, `/testcase`, `/work` paths
- **Anomalous** (flag immediately): `/Users/`, `/private/var/`, `/var/folders/`, `runs/<root_id>/`, absolute repo path in any tool input or observation
- Check ThoughtCaptured (tool_use AND tool_result), WorkCompleted.result, WorkFailed.reason, PromptSent.prompt

**Event store**
- No duplicate `(aggregate_id, sequence_number)`
- Every worker with `AgentExecutionStarted` must eventually get `WorkCompleted` or `WorkFailed` followed by `AgentExecutionFinished`
- Tool name in ThoughtCaptured.tool_name must not be `"unknown"` for Read/Bash/Glob/Grep/Edit calls
- No host paths in WorkCompleted.result or WorkFailed.reason

**Shared context aggregate** (`SharedContextCreated`)
- Must have exactly one `SharedContextCreated` event (seq 1)
- All other events on it must be `DecisionRecorded` or `ArtifactStored` written by worker agents
- It must NOT have `AgentCreated` or `AgentExecutionStarted` — it is not an agent

**End-to-end mechanical pass** — verify these artifacts exist in the run directory after completion:
```bash
ROOT_ID=<root_id>
ls runs/$ROOT_ID/testcase/
# Expected: base_commit_hash, exploit.js (or PoC script), patch.diff (or similar)
# Check secb test result if available
```

**External IO**
- If Anthropic returns 529 (overloaded) during boss/manager, record it but do NOT count it as a B1 worker path result
- If no new events appear for >10 minutes, report last event time + container status + process tree

### Step 5 — Final report

Format:

```
## BLUF: PASS / FAIL / INCONCLUSIVE

**Root ID**: ...
**Run directory**: runs/<root_id>/
**Container(s)**: ...
**Duration**: ...
**Total events**: ...
**Total worker tool calls**: ...

### Per-node table
| aggregate_id | role | events | tool_calls | terminal |
...

### Shared context aggregate
| key | value | decided_by |
(from DecisionRecorded events)

### Artifacts produced
(files found in runs/<root_id>/testcase/ and runs/<root_id>/src/ after run)

### Anomalies
(each: type, aggregate_id, seq, payload excerpt, time)
None found / [list]

### Invariant checklist
| Invariant | Result | Evidence |
| B1 identity (skip_judge) | PASS/FAIL | ... |
| B1 identity (worker.tool=claude_code) | | |
| Worker locality (container exec) | | |
| Container labels complete | | |
| Path visibility (no host leaks) | | |
| Tool name not "unknown" | | |
| No duplicate (agg_id, seq_num) | | |
| All workers have terminal event | | |
| Shared context not treated as agent | | |
| End-to-end mechanical pass | | |
```

### Step 6 — Cleanup

```bash
# Stop any lingering containers for this root_id
docker ps -a --filter "name=secbench-worker" -q | xargs -r docker rm -f

# Remove stale run-result temp files
rm -f runs/.run-result-B1-njs*.json

# Report whether run directory should be kept or removed
# (keep if mechanical pass is interesting; remove if total failure before worker spawn)
```

---

## Key invariant reminders

- **Invariant E(b)**: every worker tool call must execute inside the SEC-bench container. The in-container Claude executable is invoked via a host wrapper script at `.arise/claude-sdk-in-container` that does `docker exec -i <container_id> claude "$@"`. A Read/Bash/Grep on a host path is an E(b) violation.
- **Shared context aggregate** has no role — it is identified by `SharedContextCreated` at seq 1. Do not flag it as an anomaly; do flag if it receives `AgentCreated` or any LLM-facing event.
- **skip_judge=true** means the verification stage is skipped per subtask. `VerificationPassed` events may still appear if wired unconditionally — check whether they represent a real judge call or a pass-through.
