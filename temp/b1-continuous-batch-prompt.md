# B1 Continuous Batch Experiment Prompt

Use this prompt with Claude Code from the repository root.

```text
BLUF: Run the B1 experiment continuously in 10-CVE batches until 200 CVE instances are attempted. Do not ask for operator confirmation between batches. After each batch, audit anomalies. If a run is anomalous, kill/clean only that run's resources, delete only that run hierarchy's DB events, remove only that run directory, rerun that task, then continue.

Scope:
- Cell: B1 only.
- Config source: experiments/b1-batch-autogen/configs/B1-ours-claude-noverifier.yaml (already generated).
- B1 means hierarchical boss -> manager -> worker, skip_judge=true, worker.tool=claude_code.
- In hierarchical B1, claude_code uses the Claude SDK adapter path, not the flat Claude CLI A-cell path.
- This is not a smoke test. Use full SEC-bench tasks in batches of 10.
- Use host-side invocation with POSTGRES_HOST=localhost unless you are running inside the Compose app container.
- Do not run A, B2, C, C2, or worker-only smoke unless debugging explicitly requires a local repro for one anomalous run.

Runtime knobs (this round):
- --parallel 5 on run_matrix (5 simultaneous root tasks per batch).
- batch_size = 10 tasks per batch (state file already on this cadence).
- LLM concurrency: orchestration.concurrency.max_concurrent_llm_calls = 8 (already in config/config.yaml; do not lower for this study).
- LLM gateway: default litellm. If LiteLLM 429 storms appear (>=5 RateLimitError events in any 5-minute window OR >=3 distinct runs failing with RateLimitError back-to-back), STOP the batch and switch the gateway to OpenRouter for subsequent batches by exporting ARISE_LLM_GATEWAY=openrouter (see "OpenRouter cutover" below). Do not flip back without an explicit instruction.
- Timeouts: keep the minimum that still lets a normal hierarchical run finish. Override only if a baseline regression is observed:
  * worker.timeout: 3600  (down from 5400)
  * orchestration.max_run_duration_seconds: 5400  (down from 7200)
  * orchestration.concurrency.max_agent_step_seconds: 600 (unchanged; watchdog = 1.5x)
  Do NOT increase timeouts past these values mid-study. Watchdog timeouts on hard CVEs are expected and counted as failed, not retried.

Model configuration notes:
- B1 boss.model and manager.model can use Sonnet (same as worker) for cost savings. Opus is not required.
- A-cell configs (A1, A2) use flat mode where boss.model/manager.model are OBSOLETE and ignored.
- Only B-cells (hierarchical) actually use the boss/manager model settings.

Read first:
- agent-docs/concurrency-invariants.md
- experiments/b1-batch-autogen/configs/B1-ours-claude-noverifier.yaml
- experiments/shared/scripts/run_matrix.py
- experiments/shared/harness.py
- deployment/build-secbench-tools.sh
- infrastructure/adapters/worker/claude_sdk_adapter.py
- infrastructure/adapters/worker/shared/container_session.py
- infrastructure/adapters/worker/shared/container_exec.py
- plugins/security/docker_runtime.py
- prompts/domains/secbench/manager/exploiter.j2  (Exploit-Validator deliverable contract)
- bootstrap/infrastructure.py  (ARISE_LLM_GATEWAY env switch)

Pre-flight:
1. Confirm branch and dirty state. Do not stage/commit anything.
2. Confirm Docker daemon works:
   docker info >/dev/null
3. Confirm Compose Postgres is reachable from host:
   set -a; . deployment/.env; set +a
   POSTGRES_HOST=localhost uv run python - <<'PY'
from config.settings import Settings
s = Settings.from_yaml("experiments/b1-batch-autogen/configs/B1-ours-claude-noverifier.yaml")
print(s.database.host, s.database.port, s.database.name)
PY
4. Confirm the B1 effective config:
   - orchestration.mode == hierarchical
   - orchestration.skip_judge == true
   - worker.tool == claude_code
   - worker.model == claude-sonnet-4-5-20250929
   - SEC-bench boss/pending/manager recon max_iterations == 20
   - orchestration.concurrency.max_concurrent_llm_calls == 8
   - worker.timeout <= 3600 (override in config if needed; current effective value should be 3600)
5. Confirm the active LLM gateway: echo "${ARISE_LLM_GATEWAY:-litellm}". Confirm Anthropic Sonnet is reachable with a tiny call. If a single ping returns 429, sleep 60s and retry up to 3 times before starting a batch. If 3 retries all fail with 429 against litellm, perform the OpenRouter cutover BEFORE launching the batch.
6. Confirm batch state and resume point:
   - Read temp/b1-batch-state.json; identify the first batch with status != "completed".
   - Cross-reference runs/*/run_manifest.json (study_id == "b1-batch-autogen", cell == "B1") against the batch task list. Any task with a non-failed run_manifest and exit_status in {success, failed} is already attempted; skip re-running it unless flagged anomalous below.

SEC-bench image tag semantics:
- :latest = full reference state (source + patch + PoC)
- :patch = source at vulnerable commit + PoC files, NO gold patch (model_patch.diff removed)
- :poc = source only, no PoC artifacts
IMPORTANT: :patch images do NOT contain pre-built binaries. The Builder worker MUST compile
during the run. Binaries are installed to /work/bin/ by the Builder, not pre-baked in the image.

Image prerequisite per batch:
For each 10-task batch, build missing secb-tools images before running:
  deployment/build-all-images.sh -j 2 --continue-on-error \
    plugins/security/tests/fixtures/<task1>.json \
    ...
If an image build fails:
- Retry that image once with:
  deployment/build-secbench-tools.sh plugins/security/tests/fixtures/<task>.json
- If it still fails, mark the task as blocked_image_build in temp/b1-batch-state.json and exclude it from this batch run.
- Continue with the remaining tasks. Do not treat a missing image build as a run anomaly because no run exists yet.

Run one batch:
Use exactly one cell and one 10-task set:
  set -a; . deployment/.env; set +a
  POSTGRES_HOST=localhost ARISE_LLM_GATEWAY="${ARISE_LLM_GATEWAY:-litellm}" \
    uv run python -m experiments.shared.scripts.run_matrix \
      --study b1-batch-autogen \
      --cells B1 \
      --tasks task1,task2,...,task10 \
      --replicates 1 \
      --parallel 5 \
      --no-render

Parallelism notes:
- --parallel 5 is the target. If 429 storms emerge on litellm, do NOT lower parallelism — perform the OpenRouter cutover instead.
- Active workers will be <= min(parallel, pending_tasks).
- Monitor with: docker ps --filter "name=secbench" --format "{{.Names}}: {{.Status}}"

After batch completion, audit every run in that batch before moving to the next batch.

Run identification:
- Get run IDs from runs/<uuid>/run_manifest.json where study_id == "b1-batch-autogen", cell == "B1", task is in the current batch, replicate == 0.
- If run_matrix failed before producing a run_id for a task, record no_run_id and rerun that task once after checking image availability and API status.

Anomaly checks:
For each run_id, inspect run_manifest.json, DB events, Docker containers, and event payload text. Treat any of these as anomalies:
1. Wrong identity:
   - cell != B1
   - worker tool not claude_code
   - skip_judge not true
   - config path not B1
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
   - Valid worker paths are container paths such as /src, /testcase, /work.
4. Tool event corruption:
   - Read/Bash/Glob/Grep/Edit/Write/MultiEdit events appear as tool_name "unknown".
   - B1 worker tool_use exists but inputs are host paths instead of container paths.
5. Event store:
   - duplicate (aggregate_id, sequence_number)
   - root/parent terminal propagation incoherent: child terminal exists but parent/root remains nonterminal after process exit
6. Docker cleanup:
   - any exited/running secbench-worker container remains for the run after process exit
   - container lacks labels arise.root_id, arise.agent_id, arise.session_pid, arise.created_at while active
7. Stall:
   - no new DB events for 10 minutes while the run process is alive
   - process alive but no worker events and no active external API request
8. Exploiter deliverable (NEW, this round):
   - run_manifest.deliverables.exploit_validation_results.txt must be true (the Exploit-Validator worker is required to drop /testcase/exploit_validation_results.txt). If missing on a non-failed run, treat as anomaly and rerun the task.
   - The file (after copy-out under runs/<run_id>/testcase/exploit_validation_results.txt) MUST start with the 8-key machine-readable block (VERDICT, REASON, EXPECTED_SANITIZER_ERROR, OBSERVED_SANITIZER_ERROR, CRASH_FUNCTION_EXPECTED, CRASH_FUNCTION_OBSERVED, DETERMINISM_RUNS, CORRUPTION_ORIGIN_FUNCTION). If the block is missing or malformed, mark anomaly and rerun once.
   - Also expect /testcase/repro.sh, /testcase/poc.* and the per-role artifacts (poc_operation_map.txt, data_flow_findings.txt, forward_instrumentation.log). Their absence on a "success" run is a soft anomaly — record it in temp/b1-batch-state.json under anomalies but do NOT rerun solely on that basis.

Known failure modes (not bugs, infrastructure limits):
- Watchdog timeout: "Agent step exceeded watchdog budget 900s" - single step took >15 min
- LLM timeout: "LLM request timed out after 120s" - single API call hung
- OOM kill: exit code 137 - container killed by memory limit
These are expected for complex CVEs; mark as failed and continue, do not debug endlessly.

Health monitoring during batch (poll every 60-120s while a batch is running):
- Event activity: docker exec -i arise-db psql -U arise -d arise_events -t -c \
    "SELECT event_type, count(*) FROM events WHERE occurred_at > now() - interval '5 minutes' GROUP BY event_type ORDER BY count(*) DESC LIMIT 10;"
- API errors: docker exec -i arise-db psql -U arise -d arise_events -t -c \
    "SELECT count(*) FROM events WHERE event_type = 'ErrorOccurred' AND occurred_at > now() - interval '10 minutes';"
- LiteLLM 429 watchdog (NEW): docker exec -i arise-db psql -U arise -d arise_events -t -c \
    "SELECT count(*) FROM events WHERE event_type='ErrorOccurred' AND payload::text ~* '(RateLimitError|429|rate.?limit)' AND occurred_at > now() - interval '5 minutes';"
  If this returns >= 5 inside any 5-minute window, OR if >= 3 distinct run_manifest.exit_status='failed' runs in the current batch reference RateLimitError in their event payloads, trigger the OpenRouter cutover below.
- Exploiter deliverable spot-check: for completed runs in this batch, confirm runs/<id>/testcase/exploit_validation_results.txt exists and grep -E '^VERDICT: (PASS|FAIL)$' is present.

OpenRouter cutover (triggered only by the 429 watchdog above):
1. Drain in-flight runs: wait for already-launched run_matrix processes to finish naturally — do NOT kill them just because of 429.
2. For all subsequent batches, prepend the gateway env to every invocation:
   ARISE_LLM_GATEWAY=openrouter POSTGRES_HOST=localhost uv run python -m experiments.shared.scripts.run_matrix ...
3. Record cutover_to_openrouter_at (ISO timestamp) and cutover_reason (e.g., "litellm 429 storm: 7 RateLimitError events in 5-min window") in temp/b1-batch-state.json.
4. Confirm OPENROUTER_API_KEY is set in deployment/.env. If absent, STOP and report blocked_openrouter_missing_key.
5. Do not flip back to litellm without explicit operator instruction.

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
   POSTGRES_HOST=localhost ARISE_LLM_GATEWAY="${ARISE_LLM_GATEWAY:-litellm}" \
     uv run python -m experiments.shared.scripts.run_matrix \
       --study b1-batch-autogen \
       --cells B1 \
       --tasks "$TASK" \
       --replicates 1 \
       --parallel 1 \
       --no-render
6. Audit the rerun. If the rerun is still anomalous, clean it once more, mark task as failed_after_rerun in temp/b1-batch-state.json, and continue to the next task/batch. Do not loop forever.

Continue policy:
- After a batch has no unresolved anomalies, immediately continue to the next 10-task batch.
- Do not ask for user confirmation between batches.
- Stop only after all batches up to 200 attempted tasks have been attempted, Docker/DB cleanup has been verified, and temp/b1-batch-state.json has final status.

Final report:
- BLUF: completed / completed_with_blocked_tasks / stopped_due_to_systemic_failure / stopped_after_openrouter_cutover.
- Batches attempted and task counts.
- Successful tasks.
- Image-build blocked tasks.
- Tasks rerun and outcome.
- Remaining anomalies with exact run_id/task evidence.
- LiteLLM 429 events observed, cutover timestamp if any, gateway used per batch.
- Exploit-Validator deliverable coverage: count of runs with /testcase/exploit_validation_results.txt present + count with a valid VERDICT line.
- Confirm no secbench-worker containers remain for failed/cleaned runs.
- Confirm no broad DB delete was used; all deletes were scoped by root run_id hierarchy.
```
