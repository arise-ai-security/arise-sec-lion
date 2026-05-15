# C1 Continuous Batch Experiment Prompt

Use this prompt with Claude Code from the repository root.

```text
BLUF: Run the C1 experiment continuously in 20-CVE batches until 200 CVE instances are attempted. Do not ask for operator confirmation between batches. After each batch, audit anomalies. If a run is anomalous, kill/clean only that run's resources, delete only that run hierarchy's DB events, remove only that run directory, rerun that task, then continue.

Scope:
- Cell: C1 only.
- Config source: experiments/2026-05-13-my-study/configs/C1-qwen-noverifier.yaml.
- C1 means hierarchical boss -> manager -> worker, skip_judge=true, worker.tool=openhands, worker.model=ollama_chat/qwen3:8b.
- This is not a smoke test. Use full SEC-bench tasks in batches of 20.
- Use host-side invocation with POSTGRES_HOST=localhost unless you are running inside the Compose app container.
- Do not run A, B, B1, B2, C2, or worker-only smoke unless debugging explicitly requires a local repro for one anomalous run.

Read first:
- agent-docs/concurrency-invariants.md
- experiments/2026-05-13-my-study/configs/C1-qwen-noverifier.yaml
- experiments/shared/scripts/run_matrix.py
- experiments/shared/harness.py
- deployment/build-secbench-tools.sh
- infrastructure/adapters/worker/openhands_adapter.py
- infrastructure/adapters/worker/shared/container_session.py
- plugins/security/docker_runtime.py
- plugins/security/mcp/security_tools_server.py

Pre-flight:
1. Confirm branch and dirty state. Do not stage/commit anything.
2. Confirm Docker daemon works:
   docker info >/dev/null
3. Confirm Compose Postgres is reachable from host:
   set -a; . deployment/.env; set +a
   POSTGRES_HOST=localhost uv run python - <<'PY'
from config.settings import Settings
s = Settings.from_yaml("experiments/2026-05-13-my-study/configs/C1-qwen-noverifier.yaml")
print(s.database.host, s.database.port, s.database.name)
PY
4. Confirm the C1 effective config:
   - orchestration.mode == hierarchical
   - orchestration.skip_judge == true
   - worker.tool == openhands
   - worker.model == ollama_chat/qwen3:8b
   - worker.allowed_tools == [file_editor, glob, grep]
   - worker.tool_params.openhands.mcp_tools contains shell_in_container, valgrind_run, klee_run
   - SEC-bench boss/pending/manager recon max_iterations == 20
5. Confirm local Ollama/Qwen availability before starting:
   curl -fsS http://localhost:11434/api/tags
   ollama list | grep -E 'qwen3:8b|qwen3'
   If qwen3:8b is missing, pull it once with ollama pull qwen3:8b.
6. Confirm Opus is reachable with a tiny Anthropic API call for boss/manager. If Opus returns 529, sleep 60s and retry up to 3 times before starting a batch.

Generate an isolated C1 batch study:
1. Do not mutate experiments/2026-05-13-my-study/dataset.yaml.
2. Create an uncommitted generated study under experiments/c1-batch-autogen.
3. Use the first 200 sorted CVE fixture slugs from plugins/security/tests/fixtures/*.json, filtering to names containing ".cve-".
4. Write manifest.yaml with only C1:
   study_id: c1-batch-autogen
   dataset: dataset.yaml
   replicates: 1
   cells:
     C1: {group: C, runner: arise, config: configs/C1-qwen-noverifier.yaml}
   headline_cells: [C1]
5. Copy the C1 config into experiments/c1-batch-autogen/configs/C1-qwen-noverifier.yaml.
6. Write dataset.yaml with:
   - name: secbench-200cves-c1
   - default_cves: all 200 selected slugs
   - per_cell_overrides: {}
   - source.kind: deployment-json
   - source.paths: plugins/security/tests/fixtures/<slug>.json for every selected slug
7. Store batch state under temp/c1-batch-state.json with selected tasks, batch size 20, completed batches, reruns, anomalies, blocked image builds, and timestamps.

Image prerequisite per batch:
For each 20-task batch, build missing secb-tools images before running:
  deployment/build-all-images.sh -j 2 --continue-on-error \
    plugins/security/tests/fixtures/<task1>.json \
    ...
If an image build fails:
- Retry that image once with:
  deployment/build-secbench-tools.sh plugins/security/tests/fixtures/<task>.json
- If it still fails, mark the task as blocked_image_build in temp/c1-batch-state.json and exclude it from this batch run.
- Continue with the remaining tasks. Do not treat a missing image build as a run anomaly because no run exists yet.

Run one batch:
Use exactly one cell and one 20-task set:
  set -a; . deployment/.env; set +a
  POSTGRES_HOST=localhost uv run python -m experiments.shared.scripts.run_matrix \
    --study c1-batch-autogen \
    --cells C1 \
    --tasks task1,task2,...,task20 \
    --replicates 1 \
    --parallel 1 \
    --no-render

After batch completion, audit every run in that batch before moving to the next batch.

Run identification:
- Get run IDs from runs/<uuid>/run_manifest.json where study_id == "c1-batch-autogen", cell == "C1", task is in the current batch, replicate == 0.
- If run_matrix failed before producing a run_id for a task, record no_run_id and rerun that task once after checking image availability, Ollama status, and API status.

Anomaly checks:
For each run_id, inspect run_manifest.json, DB events, Docker containers, and event payload text. Treat any of these as anomalies:
1. Wrong identity:
   - cell != C1
   - worker tool not openhands
   - worker model not ollama_chat/qwen3:8b
   - skip_judge not true
   - config path not C1
2. Failed/timeout:
   - run_manifest.exit_status is failed or timeout
   - WorkFailed exists in the hierarchy
   - no WorkCompleted/WorkFailed terminal signal for a started worker
3. Worker locality/path leak:
   - Worker-visible ThoughtCaptured, WorkCompleted, WorkFailed, PromptSent, or WorkerCostRecorded payload contains:
     /Users/
     /private/var/
     /var/folders/
     absolute host repo path
     runs/<run_id>
     /runs/<run_id>
   - Valid worker-visible paths are container paths such as /src, /testcase, /work.
4. OpenHands tool routing:
   - Native terminal/browser tools appear in worker events.
   - Shell work bypasses MCP shell_in_container.
   - file_editor/glob/grep operations expose host paths to the model instead of container paths.
   - OpenHands observations leak bind-mounted host paths back to the model.
5. Event store:
   - duplicate (aggregate_id, sequence_number)
   - root/parent terminal propagation incoherent: child terminal exists but parent/root remains nonterminal after process exit
6. Docker cleanup:
   - any exited/running secbench-worker container remains for the run after process exit
   - container lacks labels arise.root_id, arise.agent_id, arise.session_pid, arise.created_at while active
7. Stall:
   - no new DB events for 10 minutes while the run process is alive
   - process alive but no worker events and no active Ollama/API request
8. Local model health:
   - repeated Ollama connection errors
   - qwen3:8b missing after pre-flight
   - worker attempts to call a remote model instead of local Ollama for C1 worker

Useful DB inspection snippets:
Set RUN_ID=<root uuid>, then use dockerized psql so host psql is not required:
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
SELECT aggregate_id, sequence_number, count(*)
FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM targets)
GROUP BY aggregate_id, sequence_number
HAVING count(*) > 1;
SQL

Cleanup for one anomalous run only:
Never run DELETE FROM events without a run_id-scoped target table. Never delete all events.
Set RUN_ID=<root uuid> and TASK=<task slug>, then:
1. Kill/remove only containers for that run:
   docker rm -f $(docker ps -aq --filter "label=arise.root_id=$RUN_ID") 2>/dev/null || true
2. Delete only that run's event hierarchy:
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
  SELECT aggregate_id
  FROM events
  WHERE event_type = 'SharedContextCreated'
    AND payload->>'root_id' = :'root_id'
)
INSERT INTO run_delete_targets(aggregate_id)
SELECT agent_id FROM hierarchy
UNION
SELECT aggregate_id FROM shared
ON CONFLICT DO NOTHING;
SELECT count(*) AS events_to_delete
FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM run_delete_targets);
DELETE FROM events
WHERE aggregate_id IN (SELECT aggregate_id FROM run_delete_targets);
COMMIT;
SQL
3. Remove only that run artifact directory:
   rm -rf "runs/$RUN_ID"
4. Confirm cleanup:
   docker ps -a --filter "label=arise.root_id=$RUN_ID"
   test ! -e "runs/$RUN_ID"
5. Rerun only that task once:
   set -a; . deployment/.env; set +a
   POSTGRES_HOST=localhost uv run python -m experiments.shared.scripts.run_matrix \
     --study c1-batch-autogen \
     --cells C1 \
     --tasks "$TASK" \
     --replicates 1 \
     --parallel 1 \
     --no-render
6. Audit the rerun. If the rerun is still anomalous, clean it once more, mark task as failed_after_rerun in temp/c1-batch-state.json, and continue to the next task/batch. Do not loop forever.

Continue policy:
- After a batch has no unresolved anomalies, immediately continue to the next 20-task batch.
- Do not ask for user confirmation between batches.
- Stop only after all 10 batches have been attempted, Docker/DB cleanup has been verified, and temp/c1-batch-state.json has final status.

Final report:
- BLUF: completed / completed_with_blocked_tasks / stopped_due_to_systemic_failure.
- Batches attempted and task counts.
- Successful tasks.
- Image-build blocked tasks.
- Tasks rerun and outcome.
- Remaining anomalies with exact run_id/task evidence.
- Confirm no secbench-worker containers remain for failed/cleaned runs.
- Confirm no broad DB delete was used; all deletes were scoped by root run_id hierarchy.
```
