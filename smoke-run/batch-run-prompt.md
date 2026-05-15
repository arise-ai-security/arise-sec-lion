# Batch Run Prompt — B1 Full Run

## Usage

Pass the batch number as the first thing you tell me, e.g.:

> Run batch 03

I will substitute `<NN>` with the zero-padded batch number throughout.

---

## Prerequisites

- `smoke-run/batch-manifest.json` exists (created by `batch-setup-prompt.md`)
- `experiments/2026-05-15-b1-batch-<NN>/configs/B1.yaml` exists
- All `secb-tools:<instance_id>-patch` images for this batch are built
- PostgreSQL `arise_events` is reachable at `localhost:5432` (container `arise-db`)
- `deployment/.env` contains `POSTGRES_PASSWORD`, `ANTHROPIC_API_KEY`

---

## Step 1 — Pre-flight

```bash
BATCH=<NN>
STUDY="2026-05-15-b1-batch-${BATCH}"

# Load CVE list for this batch
TASKS=$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
print(','.join(m['batches']['${BATCH}']))
")
echo "Tasks: $TASKS"
echo "Count: $(echo $TASKS | tr ',' '\n' | wc -l)"

# Verify all images present
python3 -c "
import json, pathlib, subprocess, sys
m = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
missing = []
for cve in m['batches']['${BATCH}']:
    tag = f'secb-tools:{cve}-patch'
    r = subprocess.run(['docker','image','inspect',tag], capture_output=True)
    if r.returncode != 0:
        missing.append(tag)
if missing:
    print('MISSING IMAGES:')
    for t in missing: print(' ', t)
    sys.exit(1)
else:
    print(f'All images present for batch ${BATCH}')
"

# DB baseline
source deployment/.env
PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U arise -d arise_events \
  -c "SELECT COUNT(*) as baseline_events FROM events;"

# No leftover secbench containers
docker ps -a --filter "name=secbench-worker" --format "{{.ID}} {{.Names}}"
```

If images are missing, run `deployment/build-secbench-tools.sh <instance_id>` for each.
If leftover containers exist, `docker rm -f <id>`.

---

## Step 2 — Launch

Parallelism of **3** is the recommended default (3 concurrent CVEs, each spawning 1
secbench container). Raise to 4 on a machine with ≥16 cores; lower to 2 if memory is
tight (each container uses ~1–2 GB).

```bash
BATCH=<NN>
STUDY="2026-05-15-b1-batch-${BATCH}"
TASKS=$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
print(' '.join(m['batches']['${BATCH}']))
")

set -a && source deployment/.env && set +a
POSTGRES_HOST=localhost \
uv run python -m experiments.shared.scripts.run_matrix \
  --study "$STUDY" \
  --cells B1 \
  --tasks $TASKS \
  --replicates 1 \
  --parallel 3 \
  --no-render \
  2>&1 | tee /tmp/b1-batch-${BATCH}.log &

echo "PID=$!  BATCH=$BATCH  STUDY=$STUDY"
echo "Tail: tail -f /tmp/b1-batch-${BATCH}.log"
```

Record the root_id(s) — each CVE gets its own root agent printed as:
`🤖 Agent <uuid>... created as boss`

---

## Step 3 — Monitor loop

Poll every **90 seconds** until `matrix complete` appears in the log or **180 minutes**
elapse. At each tick, run all five checks below.

### A. Per-node event summary

```sql
WITH roles AS (
  SELECT DISTINCT ON (aggregate_id) aggregate_id,
    COALESCE(payload->>'role','?') as role
  FROM events
  WHERE event_type IN ('AgentExecutionStarted','AgentCreated')
  ORDER BY aggregate_id,
    CASE event_type WHEN 'AgentExecutionStarted' THEN 0 ELSE 1 END
),
stats AS (
  SELECT aggregate_id,
    COUNT(*) as events,
    COUNT(*) FILTER (
      WHERE event_type='ThoughtCaptured' AND payload->>'output_type'='tool_use'
    ) as tool_calls,
    MAX(occurred_at)::time(0) as last_seen,
    MAX(CASE WHEN event_type IN
      ('WorkCompleted','WorkFailed','AgentExecutionFinished') THEN event_type END
    ) as terminal
  FROM events GROUP BY aggregate_id
)
SELECT s.aggregate_id::text, COALESCE(r.role,'shared_ctx') as role,
  s.events, s.tool_calls, s.last_seen, COALESCE(s.terminal,'-') as terminal
FROM stats s LEFT JOIN roles r USING (aggregate_id)
ORDER BY MIN(s.last_seen);
```

### B. Host path leak scan (flag immediately if any rows returned)

```sql
SELECT aggregate_id::text, event_type, sequence_number,
  LEFT(payload::text, 300) as excerpt
FROM events
WHERE (payload::text LIKE '%/Users/%'
    OR payload::text LIKE '%/private/var/%'
    OR payload::text LIKE '%/var/folders/%'
    OR payload::text LIKE '%arise-sec-lion/runs/%')
  AND event_type IN (
    'ThoughtCaptured','WorkCompleted','WorkFailed','PromptSent','WorkerCostRecorded'
  )
ORDER BY occurred_at DESC LIMIT 10;
```

### C. Unknown tool name check (flag if any rows returned)

```sql
SELECT aggregate_id::text, sequence_number, payload->>'tool_name' as tool_name,
  LEFT(payload->>'content', 100) as content
FROM events
WHERE event_type='ThoughtCaptured'
  AND payload->>'output_type'='tool_use'
  AND (payload->>'tool_name' IS NULL OR payload->>'tool_name' = 'unknown')
ORDER BY occurred_at DESC LIMIT 10;
```

### D. Duplicate sequence check (must always return 0 rows)

```sql
SELECT aggregate_id, sequence_number, COUNT(*)
FROM events
GROUP BY aggregate_id, sequence_number
HAVING COUNT(*) > 1;
```

### E. Active containers and host process tree

```bash
# Containers — check labels are complete
docker ps --filter "name=secbench-worker" -q | \
  xargs -I{} docker inspect {} \
    --format '{{.Name}}  root={{index .Config.Labels "arise.root_id"}}  agent={{index .Config.Labels "arise.agent_id"}}' \
  2>/dev/null

# No model-triggered builds on host
ps aux | grep -E "make|gcc|clang|ninja|cmake" | grep -v "grep\|vscode\|jetbrains"
```

---

## Step 4 — Anomaly classification

Record each anomaly with: `event_type | aggregate_id | seq | time | excerpt`.

| Category | Trigger | Severity |
|---|---|---|
| Host path leak | Any `/Users/`, `/private/var/`, repo absolute path in worker event payload | CRITICAL |
| Unknown tool name | `tool_name=null` or `"unknown"` in tool_use ThoughtCaptured | HIGH |
| Duplicate (agg,seq) | Any row from check D | CRITICAL |
| Orphan worker | Worker has `AgentExecutionStarted` but no terminal after run ends | HIGH |
| Non-container Bash | Build/compile command visible in host `ps` during worker execution | CRITICAL |
| skip_judge violated | Any `JudgementRecorded` event appears | MEDIUM |
| Non-claude_sdk stream | ThoughtCaptured with `stream != 'claude_sdk'` on a worker | HIGH |
| Shared context mis-typed | SharedContextCreated aggregate also has AgentCreated/AgentExecutionStarted | MEDIUM |

---

## Step 5 — Post-run checks

Once `matrix complete` appears:

```bash
BATCH=<NN>

# 1. Final per-node table (same query as Step 3A)
# 2. Orphan workers — workers without terminal events
source deployment/.env
PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U arise -d arise_events -c "
SELECT aggregate_id::text, MAX(occurred_at)::time(0) as last_event
FROM events
WHERE aggregate_id IN (
  SELECT DISTINCT aggregate_id FROM events WHERE event_type='AgentExecutionStarted'
    AND payload->>'role'='worker'
)
AND aggregate_id NOT IN (
  SELECT DISTINCT aggregate_id FROM events
  WHERE event_type IN ('WorkCompleted','WorkFailed')
)
GROUP BY aggregate_id;"

# 3. Artifact spot-check for 3 random CVEs in the batch
python3 -c "
import json, pathlib, random
m = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
sample = random.sample(m['batches']['${BATCH}'], min(3, len(m['batches']['${BATCH}'])))
for cve in sample:
    # Find run dir — look for RunStarted event with this instance_id
    print(f'Check runs/ for CVE: {cve}')
"
# Then for each root_id:
# ls runs/<root_id>/testcase/
# Expected files: base_commit_hash, (exploit|poc).*, patch.diff or similar

# 4. matrix-summary.md
cat "experiments/2026-05-15-b1-batch-${BATCH}/reports/matrix-summary.md" 2>/dev/null || echo "not yet rendered"
```

---

## Step 6 — Final report

```
## BLUF: PASS / FAIL / PARTIAL  — Batch <NN>

**Study**: 2026-05-15-b1-batch-<NN>
**CVEs**: <count>  (<first> .. <last>)
**Duration**: ...
**Total events**: ...
**Total worker tool calls**: ...

### Outcome summary
| CVE | root_id | Workers | Tool Calls | Result |
| ... | ... | ... | ... | WorkCompleted / WorkFailed / Orphan |

### Anomalies
(each: category | aggregate_id | seq | time | excerpt)
None / [list]

### Invariant results
| Invariant | Result |
| No host path leaks | PASS/FAIL |
| All tool_names named | PASS/FAIL |
| No duplicate (agg,seq) | PASS/FAIL |
| All workers have terminal | PASS/FAIL |
| stream=claude_sdk on all workers | PASS/FAIL |
| No host-side builds detected | PASS/FAIL |
| skip_judge respected | PASS/FAIL |
| Shared context not mis-typed | PASS/FAIL |
```

---

## Step 7 — Cleanup

```bash
BATCH=<NN>

# Remove secbench containers for this batch
docker ps -a --filter "name=secbench-worker" -q | xargs -r docker rm -f
docker ps -a --filter "name=secbench-worker" --format "{{.ID}} {{.Names}}"  # should be empty

# Remove stale run-result temp files
rm -f runs/.run-result-B1-*.json

# Confirm
echo "Cleanup done for batch $BATCH"
```

Do **not** remove `runs/<root_id>/` directories — they are the durable output of the
experiment and are needed for rendering and analysis.
