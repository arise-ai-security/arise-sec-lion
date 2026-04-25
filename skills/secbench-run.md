---
description: Run a SEC-bench CVE instance, monitor progress, and diagnose issues
argument-hint: <cve-file-path> [--no-restart]
allowed-tools: [Read, Glob, Grep, Bash, Agent, TaskCreate, TaskUpdate]
---

# SEC-bench Run & Monitor

An **optional shortcut** that chains together the steps a user would otherwise run by hand: build the per-instance tools image, launch `python main.py run …` inside `arise-app`, poll the event DB, sweep a known-issue checklist, and summarise artifacts.

> **New to this repo?** Read `README.md` first — §4 Path A covers the manual flow (the primary path). This skill is Path B: same work, bundled into one command. Always defer to the manual flow if the user prefers stepping through it themselves.

## Arguments

The user provided: $ARGUMENTS

If no CVE file path is given, **do NOT assume a default**. Instead, list the available CVE instances below and ask the user which one to run before proceeding.

### Available CVE instances

Primary source: `plugins/security/tests/fixtures/*.json` (in-repo, authoritative).
Additional: `deployment/*.json`, `/home/songli/data/CVE_Instances/*.json`.

To list fixtures currently in-repo:
```bash
ls /home/songli/arise-sec-lion/plugins/security/tests/fixtures/*.json
```
Run the `ls` before asking the user to pick — do not rely on a stale hardcoded list.

Ask: "Which CVE instance would you like to run?" and wait for the user's response before continuing to Phase 1.

## Procedure

### Phase 1: Pre-flight checks

1. Verify containers are running:
   ```bash
   cd /home/songli/arise-sec-lion/deployment && docker compose --profile local ps
   ```
2. Unless `--no-restart` is passed, restart containers:
   ```bash
   cd /home/songli/arise-sec-lion/deployment && docker compose --profile local restart
   ```
3. Wait for health checks to pass (db and api must be "healthy").
4. Verify the CVE instance JSON file exists and read it to understand the vulnerability.
5. Check that the required SEC-bench tools Docker image exists. If not, build it (see Phase 1b).

### Phase 1b: Build SEC-bench tools image

Before starting the run, ensure the SEC-bench Docker image exists for this CVE.

**Preferred method** — manual pull + build (no venv required). If the fixture JSON has a
`docker_image_override` field, use it as the base image directly (it's already on DockerHub):
```bash
# 1. Pull the base image (docker_image_override from the fixture)
docker pull <BASE_IMAGE>
# 2. Build the tools layer on top
cd /home/songli/arise-sec-lion && docker build -f deployment/secbench-tools.Dockerfile \
  --build-arg BASE_IMAGE=<BASE_IMAGE> \
  -t secb-tools:<instance_id>-patch .
```

**Alternative** — build script (requires working venv):
```bash
cd /home/songli/arise-sec-lion && bash deployment/build-secbench-tools.sh <CVE_FILE_PATH>
```
This builds the `secb-tools:<instance_id>-patch` image from the CVE's base Docker image. If the image already exists, it will be skipped.

### Phase 2: Start the run

Run the instance inside the arise-app container:
```bash
docker exec arise-app bash -c "python main.py run \
  --domain-context-file <CVE_FILE_PATH> \
  --domain security \
  '<task description from CVE data>' \
  2>&1"
```
Run this in the background with `run_in_background: true`.

The task description should follow this format:
"Analyze <CVE-ID> in <project>: <bug_description summary>, perform static analysis, build the project, reproduce the bug with the PoC, develop and verify a patch"

### Phase 3: Monitor progress (repeat every 2-5 minutes)

Run these diagnostic queries against the database. The boss_id will be the latest RunStarted event.

#### Live review surfaces (tell the user about these)

At the start of Phase 3, remind the user they have three complementary views into the run:

1. **This skill's monitoring loop** (below) — rigorous: DB queries + known-issue checklist.
2. **Dashboard** at `http://localhost:8000` — intuitive:
   - **Agent Tree** (center): live hierarchy, click a node to focus right-hand panels.
   - **Summary** tab: task, supervisor context, worker output, insights.
   - **Events** tab: raw event stream (SSE-merged).
   - **Costs** tab: tokens + $ per run (live).
   - **Prompts** tab: exact rendered prompts the worker saw.
   - **Prompt Trace** page (purple button top-right → `/prompt-trace/<rootId>`): full composed system prompt per agent, including the security-tools block and supervisor insights.
3. **Artifacts on disk** at `runs/<BOSS_ID>/testcase/` — appears incrementally as workers complete; safe to `tail -f` individual files.

Use the dashboard for intuition, this skill for rigour.

#### 3a. Agent count and hierarchy
```sql
SELECT count(*) as total_agents FROM events
WHERE event_type='AgentCreated' AND occurred_at > '<RUN_START_TIME>';
```
**Check**: Should be 15-20 agents. Over 25 means over-decomposition.

#### 3b. Event timeline (filtered)
```sql
SELECT
    substring(e.aggregate_id::text from 1 for 8) as agent,
    e.event_type,
    e.occurred_at,
    CASE
        WHEN e.event_type = 'VerificationFailed' THEN payload->>'failed_stage' || ': ' || substring(payload->>'feedback' from 1 for 180)
        WHEN e.event_type = 'VerificationPassed' THEN substring(payload->>'feedback' from 1 for 150)
        WHEN e.event_type = 'RetryScheduled' THEN 'attempt ' || (payload->>'attempt_number')
        WHEN e.event_type = 'ChildCompleted' THEN substring(payload->>'child_id' from 1 for 8)
        WHEN e.event_type = 'ChildFailed' THEN substring(payload->>'child_id' from 1 for 8)
        WHEN e.event_type = 'WorkCompleted' THEN 'done'
        WHEN e.event_type = 'WorkFailed' THEN substring(payload->>'error' from 1 for 100)
        WHEN e.event_type = 'RunCompleted' THEN payload->>'final_status'
        ELSE ''
    END as detail
FROM events e
WHERE e.occurred_at > '<RUN_START_TIME>'
AND e.event_type IN ('AgentExecutionStarted', 'WorkCompleted', 'WorkFailed',
    'VerificationPassed', 'VerificationFailed', 'RetryScheduled',
    'ChildCompleted', 'ChildFailed', 'RunCompleted')
ORDER BY e.occurred_at
LIMIT 60;
```

#### 3c. Agent state summary
```sql
WITH agent_roles AS (
    SELECT aggregate_id, payload->>'role' as initial_role,
           substring(payload->>'parent_id' from 1 for 8) as parent
    FROM events WHERE event_type='AgentCreated' AND occurred_at > '<RUN_START_TIME>'
),
agent_final_role AS (
    SELECT DISTINCT ON (aggregate_id) aggregate_id, payload->>'determined_role' as role
    FROM events WHERE event_type='ComplexityEvaluated' AND occurred_at > '<RUN_START_TIME>'
    ORDER BY aggregate_id, occurred_at DESC
),
latest_status AS (
    SELECT DISTINCT ON (aggregate_id) aggregate_id,
        CASE
            WHEN event_type = 'WorkCompleted' THEN 'completed'
            WHEN event_type = 'WorkFailed' THEN 'failed'
            WHEN event_type = 'RetryScheduled' THEN 'retrying'
            WHEN event_type = 'StatusChanged' THEN payload->>'new_status'
            ELSE 'unknown'
        END as status
    FROM events
    WHERE event_type IN ('StatusChanged','WorkCompleted','WorkFailed','RetryScheduled')
    AND occurred_at > '<RUN_START_TIME>'
    ORDER BY aggregate_id, occurred_at DESC
)
SELECT substring(a.aggregate_id::text from 1 for 8) as agent,
       COALESCE(r.role, a.initial_role) as role, a.parent,
       COALESCE(l.status, 'pending') as status
FROM agent_roles a
LEFT JOIN agent_final_role r ON a.aggregate_id = r.aggregate_id
LEFT JOIN latest_status l ON a.aggregate_id = l.aggregate_id
ORDER BY agent;
```

### Phase 4: Issue detection checklist

After each monitoring cycle, check for these known issues:

#### Missing SEC-bench tools image
**Check**: Run fails at startup with "Missing SEC-bench image secb-tools:<instance_id>-patch. Build it first with deployment/build-secbench-tools.sh."

**Base image sources** (in order of preference):
1. If the fixture JSON has a `docker_image_override` field, that is the authoritative source — use it directly.
2. User's personal DockerHub: `songtli/secb.eval.x86_64.<project>.<cve-id>:patch` (fixtures the user has rebuilt/pushed).
3. Upstream SEC-bench: `hwiwonlee/secb.eval.x86_64.<project>.<cve-id>:patch`.

**Fix**: Build the image manually:
```bash
# 1. Pull the base image (prefer docker_image_override from the fixture; otherwise try songtli/ then hwiwonlee/)
docker pull <BASE_IMAGE>
# 2. Build the tools layer
docker build -f deployment/secbench-tools.Dockerfile \
  --build-arg BASE_IMAGE=<BASE_IMAGE> \
  -t secb-tools:<instance_id>-patch .
```
Or use the build script (requires working venv): `bash deployment/build-secbench-tools.sh <cve-json-path>` — it honors `docker_image_override` automatically.

#### Scheduling bug (cross-hierarchy depends_on)
**Check**: Are workers from Fixer/Reporter running BEFORE Builder/Exploiter complete?
Look at AgentExecutionStarted timestamps. If a Fixer worker starts before Builder's last ChildCompleted, the scheduling bug is active.
**Fix**: `core/application/services/query/query_service.py` - `_ancestors_deps_satisfied()` method.

#### Over-decomposition
**Check**: Total agents > 25, or sub-phase tasks assessed as MANAGER (creating 3-level trees).
**Fix**: `prompts/domains/secbench/assess.j2` - strengthen EXECUTE default for sub-phase tasks.

#### Duplicate build workers
**Check**: Workers named [Builder], [Instrumented-Builder], [Build-Setup], etc. under Exploiter or Fixer trees.
**Fix**: `prompts/domains/secbench/manager/exploiter.j2` and `fixer.j2` - strengthen role name enforcement.

#### Role name non-compliance
**Check**: Workers with names that don't match the 5 template-prescribed roles per phase.
Expected Exploiter roles: PoC-Researcher, Data-Flow-Analyst, PoC-Tester, Repro-Creator, Exploit-Validator.
Expected Fixer roles: Root-Cause-Analyst, Candidate-Reviewer, Patch-Creator, Patch-Validator, Fix-Aggregator.
**Fix**: Strengthen role enforcement in manager templates.

#### File editor path errors
**Check**: Worker logs contain "path should be an absolute path" or "does not exist" for /src/ or /testcase/ paths.
**Fix**: `infrastructure/adapters/worker/shared/container_session.py` - verify absolute host paths in task prefix.

#### Verification judge failures
**Check**: Judge rejects workers for artifact naming (different filename than expected). vs. legitimate quality failures.
**Fix**: `core/application/services/orchestration/verification_pipeline.py` - judge prompt flexibility.

#### Missing deliverables
**Check**: After run completes, verify these files exist in the run testcase directory:
```bash
ls runs/<BOSS_ID>/testcase/
```
Required: `base_commit_hash`, `repro.sh`, `model_patch.diff`, `security_report.md`

### Phase 5: Post-run analysis

Artifacts land in `runs/<BOSS_ID>/testcase/`. Required deliverables:

| File | Meaning |
|------|--------|
| `security_report.md` | Human-readable summary: CVE, root cause, fix, verification |
| `model_patch.diff` | The proposed patch (git-apply-able against `base_commit_hash`) |
| `repro.sh` | Standalone reproducer that triggers the original sanitizer error |
| `base_commit_hash` | Vulnerable commit the patch applies to |

Plus per-worker tool output (cppcheck, cflow, gdb, ltrace, strace, valgrind, ASan logs, PoC candidate variants). A clean run has 50–120 files in `testcase/`.

After the run completes (RunCompleted event):

1. **Timing breakdown**: Calculate elapsed time per phase from first AgentExecutionStarted to last ChildCompleted.
2. **Verification stats**: Count passed vs failed, identify most-retried workers.
3. **Phase completion**: Which phases completed vs timed out.
4. **Artifact quality**: Read security_report.md and model_patch.diff to assess output quality.
5. **Compare with previous runs**: Check if agent count, pass rates, and timing improved.

### Historical Run Data

| Run | Agents | Phases Done | Duration | Key Issue |
|-----|--------|-------------|----------|-----------|
| 1   | ~20    | 1/4         | timeout  | Cascade failures |
| 2   | ~20    | 2/4         | timeout  | Cascade failures |
| 3   | ~27    | 3/4         | timeout  | Partial success helped |
| 4   | 37     | 1/4         | timeout  | Over-decomposition |
| 5   | 17     | 4/4         | ~43min   | First full success |
| 6   | 17     | TBD         | TBD      | Scheduling bug found |

### Prompt Improvement Notes

When diagnosing prompt-level issues from a run (e.g., judge rejecting valid work, workers using wrong tools),
security-related prompt fixes should be written to `prompts/domains/secbench/` templates — NOT to the generic
`prompts/roles/` or `prompts/operations/` templates which are domain-agnostic.

Key secbench prompt files:
- `prompts/domains/secbench/boss.j2` — Boss-level security decomposition (4-phase process, tool prescriptions)
- `prompts/domains/secbench/manager.j2` — General manager security tool research guidance
- `prompts/domains/secbench/worker.j2` — General worker security research mindset, tool iteration, artifact naming
- `prompts/domains/secbench/manager/builder.j2` — Builder-specific manager decomposition
- `prompts/domains/secbench/manager/exploiter.j2` — Exploiter-specific manager decomposition
- `prompts/domains/secbench/manager/fixer.j2` — Fixer-specific manager decomposition
- `prompts/domains/secbench/assess.j2` — Assessment prompt for complexity evaluation

### Available CVE instances (end-of-skill reference)

Primary: `plugins/security/tests/fixtures/*.json`. See the top of this skill for listing commands.
