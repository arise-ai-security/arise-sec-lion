# A1/A2 Batch Experiment Prompt

Use this prompt with Claude Code from the repository root on a GCP Compute Engine VM.

```text
BLUF: Run A1 and A2 cells against the pre-created a12-batch-autogen study in batches of 20 CVEs until all 200 CVEs are attempted. Do not ask for operator confirmation between batches. After each batch, audit runs for code bugs only — kill, fix, and rerun on true code bugs; report infra failures and continue. Runs append to the persistent study; do not recreate it.

Scope:
- Cells: A1 and A2 (both cells per task per batch).
- Study: experiments/a12-batch-autogen (pre-created; DO NOT recreate or overwrite manifest.yaml or dataset.yaml).
- A1: flat Claude Code with subagents (Task tool allowed). Config: experiments/a12-batch-autogen/configs/A1-claude-code-subagent.yaml.
- A2: flat Claude Code without subagents (Task tool disallowed). Config: experiments/a12-batch-autogen/configs/A2-claude-code-nosubagent.yaml.
- orchestration.mode == flat for both cells: single-agent session, no boss→manager→worker decomposition.
- worker.tool == claude_code for both cells: Claude Code CLI runs inside the secbench container.
- This is not a smoke test. Use full SEC-bench CVE tasks.
- Use host-side invocation with POSTGRES_HOST=localhost (GCP VM, Postgres running inside Docker).
- Do not run B or C cells unless debugging an explicit code bug that requires a controlled repro.

Read first:
- experiments/a12-batch-autogen/configs/A1-claude-code-subagent.yaml
- experiments/a12-batch-autogen/configs/A2-claude-code-nosubagent.yaml
- experiments/shared/scripts/run_matrix.py
- experiments/shared/harness.py
- infrastructure/adapters/worker/claude_sdk_adapter.py
- infrastructure/adapters/worker/shared/container_session.py
- infrastructure/adapters/worker/shared/container_exec.py
- plugins/security/docker_runtime.py

Pre-flight:
1. Confirm branch and dirty state. Do not stage or commit anything.
2. Confirm Docker daemon:
   docker info >/dev/null
3. Confirm Postgres reachable from host:
   set -a; . deployment/.env; set +a
   POSTGRES_HOST=localhost uv run python -c "
   from config.settings import Settings
   s = Settings.from_yaml('experiments/a12-batch-autogen/configs/A1-claude-code-subagent.yaml')
   print(s.database.host, s.database.port, s.database.name)
   "
4. Confirm effective config for A1:
   - orchestration.mode == flat
   - orchestration.skip_judge == true
   - worker.tool == claude_code
   - worker.allowed_tools contains Task (A1), does NOT contain Task (A2)
5. Confirm Anthropic API reachable with a tiny call. If 529, sleep 60s and retry up to 3 times.

Image prerequisite per batch:
Build missing secb-tools images before running each batch:
  deployment/build-all-images.sh -j 4 --continue-on-error \
    plugins/security/tests/fixtures/<task1>.json \
    ...
If an image build fails:
- Retry that image once:
  deployment/build-secbench-tools.sh plugins/security/tests/fixtures/<task>.json
- If still fails, mark blocked_image_build in temp/a12-batch-state.json and skip that task.
- Continue with remaining tasks. A build failure is infrastructure, not a code bug.

Run one batch:
200 CVEs total; 10 batches of 20 (batches 0–9). Derive each batch's task list from dataset.yaml default_cves in order: batch 0 = entries 1–20, batch 1 = entries 21–40, …, batch 9 = entries 181–200. Run both A1 and A2 for each batch:
  set -a; . deployment/.env; set +a
  POSTGRES_HOST=localhost uv run python -m experiments.shared.scripts.run_matrix \
    --study a12-batch-autogen \
    --cells A1,A2 \
    --tasks task1,task2,...,task20 \
    --replicates 1 \
    --parallel 8 \
    --no-render

Runs are automatically appended to experiments/a12-batch-autogen/manifest.yaml via register_run.
Do not touch manifest.yaml manually.

After batch completion, audit every run before the next batch.

Run identification:
- Find run IDs from runs/<uuid>/run_manifest.json where study_id == "a12-batch-autogen", cell in [A1, A2], task in current batch.
- If run_matrix exited before producing a run_id for a task, that is an infrastructure failure — record no_run_id and continue; do not treat as a code bug.

Anomaly classification — CRITICAL DISTINCTION:

CODE BUG (kill + fix + rerun):
  1. Host path leak: any worker-visible event payload (ThoughtCaptured, WorkCompleted,
     WorkFailed, PromptSent, WorkerCostRecorded, ToolCallCompleted) contains:
       /Users/
       /private/var/
       /var/folders/
       <absolute host repo path>   -- e.g. /home/<user>/arise-sec-lion or /opt/arise
       runs/<run_id>               -- relative run dir leaked into container context
     Valid worker paths: /src, /testcase, /work, /tmp, container-relative paths only.
  2. Wrong tool in A2: Task tool appears in tool_use events for an A2 run
     (disallowed_tools must contain Task).
  3. Process stall: run process alive but no new DB events for 15 minutes.
  4. Corrupt run_manifest: runs/<uuid>/run_manifest.json is missing or fails JSON parse
     after the process exits cleanly.
  5. Wrong cell identity: run_manifest.json.cell not in [A1, A2].

INFRASTRUCTURE FAILURE (report + continue, no kill):
  - API rate limit (429) or overload (529) response in events.
  - Docker container failed to start (image pull failure, OOM, disk full).
  - Network timeout to Anthropic API.
  - ANTHROPIC_API_KEY expired or quota exceeded.
  - run_matrix subprocess crashed before minting a run_id.
  - exit_status == "failed" with an API/network error in DB events (not a path leak).

DB inspection:
Set RUN_ID=<root uuid>, then:
  docker exec -i arise-db psql -U arise -d arise_events -v ON_ERROR_STOP=1 -v root_id="$RUN_ID" <<'SQL'
WITH RECURSIVE hierarchy(agent_id) AS (
  SELECT :'root_id'::uuid
  UNION
  SELECT (e.payload->>'child_id')::uuid
  FROM events e
  JOIN hierarchy h ON e.aggregate_id = h.agent_id
  WHERE e.event_type = 'ChildSpawned'
), shared AS (
  SELECT aggregate_id
  FROM events
  WHERE event_type = 'SharedContextCreated'
    AND payload->>'root_id' = :'root_id'
), targets AS (
  SELECT agent_id AS aggregate_id FROM hierarchy
  UNION
  SELECT aggregate_id FROM shared
)
SELECT event_type, count(*)
FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM targets)
GROUP BY event_type
ORDER BY event_type;
SQL

Path leak check — scan payload text for host paths:
  docker exec -i arise-db psql -U arise -d arise_events -v ON_ERROR_STOP=1 -v root_id="$RUN_ID" <<'SQL'
WITH RECURSIVE hierarchy(agent_id) AS (
  SELECT :'root_id'::uuid
  UNION
  SELECT (e.payload->>'child_id')::uuid
  FROM events e
  JOIN hierarchy h ON e.aggregate_id = h.agent_id
  WHERE e.event_type = 'ChildSpawned'
), shared AS (
  SELECT aggregate_id FROM events
  WHERE event_type = 'SharedContextCreated' AND payload->>'root_id' = :'root_id'
), targets AS (
  SELECT agent_id AS aggregate_id FROM hierarchy
  UNION SELECT aggregate_id FROM shared
)
SELECT event_type, left(payload::text, 300)
FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM targets)
  AND (
    payload::text ILIKE '%/Users/%'
    OR payload::text ILIKE '%/private/var/%'
    OR payload::text ILIKE '%/var/folders/%'
    OR payload::text LIKE '%runs/' || :'root_id' || '%'
  )
ORDER BY sequence_number;
SQL

Cleanup for one code-bug run only:
Never DELETE FROM events without a run_id-scoped target. Never delete all events.
Set RUN_ID=<root uuid> and TASK=<task slug>, then:
1. Kill containers for that run:
   docker rm -f $(docker ps -aq --filter "label=arise.root_id=$RUN_ID") 2>/dev/null || true
2. Delete that run's events:
   docker exec -i arise-db psql -U arise -d arise_events -v ON_ERROR_STOP=1 -v root_id="$RUN_ID" <<'SQL'
BEGIN;
CREATE TEMP TABLE run_delete_targets(aggregate_id uuid PRIMARY KEY) ON COMMIT DROP;
WITH RECURSIVE hierarchy(agent_id) AS (
  SELECT :'root_id'::uuid
  UNION
  SELECT (e.payload->>'child_id')::uuid
  FROM events e
  JOIN hierarchy h ON e.aggregate_id = h.agent_id
  WHERE e.event_type = 'ChildSpawned'
), shared AS (
  SELECT aggregate_id FROM events
  WHERE event_type = 'SharedContextCreated' AND payload->>'root_id' = :'root_id'
)
INSERT INTO run_delete_targets(aggregate_id)
SELECT agent_id FROM hierarchy UNION SELECT aggregate_id FROM shared
ON CONFLICT DO NOTHING;
SELECT count(*) AS events_to_delete FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM run_delete_targets);
DELETE FROM events WHERE aggregate_id IN (SELECT aggregate_id FROM run_delete_targets);
COMMIT;
SQL
3. Remove the run directory:
   rm -rf "runs/$RUN_ID"
4. Verify cleanup:
   docker ps -a --filter "label=arise.root_id=$RUN_ID"
   test ! -e "runs/$RUN_ID"
5. Fix the root cause in code before rerunning. For path leaks, identify the infrastructure
   layer emitting the host path (container_session.py, container_exec.py, docker_runtime.py,
   or prompt construction in plugins/security/). Fix the source, not the symptom.
6. Rerun that task once:
   set -a; . deployment/.env; set +a
   POSTGRES_HOST=localhost uv run python -m experiments.shared.scripts.run_matrix \
     --study a12-batch-autogen \
     --cells A1,A2 \
     --tasks "$TASK" \
     --replicates 1 \
     --parallel 2 \
     --no-render
7. Audit the rerun. If still anomalous (same code bug), clean once more, record
   failed_after_rerun in temp/a12-batch-state.json, and continue. Do not loop.
   If rerun succeeds, record rerun_success.

Batch state:
Track progress in temp/a12-batch-state.json:
  {
    "study_id": "a12-batch-autogen",
    "total_cves": 200,
    "batch_size": 20,
    "batches": [
      {
        "batch_index": 0,
        "tasks": [...],
        "started_at": "...",
        "completed_at": "...",
        "runs": {
          "<task>": {
            "A1": {"run_id": "...", "exit_status": "...", "anomaly": null},
            "A2": {"run_id": "...", "exit_status": "...", "anomaly": null}
          }
        },
        "blocked_image_builds": [],
        "infra_failures": [],
        "code_bug_reruns": []
      }
    ]
  }

Continue policy:
- After a batch has no unresolved code bugs, immediately start the next batch.
- Do not ask for operator confirmation between batches.
- Infra failures are noted in batch state but never block progression.
- Stop after all batches are attempted and temp/a12-batch-state.json is final.

Final report:
- BLUF: completed / completed_with_issues / stopped_due_to_systemic_failure.
- Batches run and task counts per cell.
- Successful runs (A1 and A2 separately).
- Image-build blocked tasks.
- Infra failures (counted, not investigated).
- Code bug runs: which bug, fix applied, rerun outcome.
- Any remaining unresolved code bugs with run_id and event evidence.
- Confirm no secbench containers remain for cleaned runs.
- Confirm all DB deletes were scoped by root run_id hierarchy.
```
