# SEC-bench Experiment Plan

Experiment plan for validating arise-sec-lion's tree-topology multi-agent system against SEC-bench cybersecurity benchmarks.

> **Superseded for the current N1/B4 confirmatory design.** Use
> [`docs/n1-b4-experiment-report.html`](../docs/n1-b4-experiment-report.html) for the
> current control/treatment boundary, common artifact contract, four-gate success
> definition, paired analysis, and release status. The historical material below remains
> useful only as planning context.

> **Forward-looking plan.** This document is a plan, not a record of the current system.
> For as-built truth (architecture, runtime, criteria wiring, current models, with
> `file:line` citations) see [`SYSTEM_REFERENCE.md`](../SYSTEM_REFERENCE.md). Live
> experiment cells and their configs are under `experiments/<cell>/configs/*.yaml`
> (e.g. `experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml`).

## Goal

1. **Reproduce SEC-bench results** using our tree-topology system for direct comparison.
2. **Validate the hypothesis** that hierarchical decomposition (abstract -> concrete) outperforms flat 3-agent pipelines.
3. **Ultimately**: generate best patches that fix given CVE instances.

## SEC-bench Baseline (Reference Numbers)

- **Best patching**: 33.8% (SWE-agent + Claude 3.7 Sonnet, $1.29/instance avg)
- **Best PoC generation**: 12.5% (SWE-agent + Claude 3.7 Sonnet)
- **SECVERIFIER multi-agent**: 26.0% overall (Builder 90.0%, Exploiter 35.6%, Fixer 81.2%)
- **Single-agent CODEACT**: 14.0% overall
- **200 verified CVE instances** across 29 C/C++ projects
- **Max 75 iterations**, cost limit $1.00-$1.50/instance
- **Pipeline**: Builder -> Exploiter -> Fixer (flat, no recursive decomposition)

## Core Hypothesis

Our tree topology MUST decompose abstract big tasks into multiple smaller granule concrete tasks that even foolish AI agents can do.

- **Closer to root node** = more abstract and big
- **Closer to worker node** = more concrete and small
- Workers MUST receive granular, self-contained subtasks WITH optimal context from parent nodes
- Manager nodes should focus on concretizing parent tasks and providing good context for workers
- Workers should communicate with predecessors (sibling context) to know what prior work was done

### Known Issues

- Task decomposition currently allocates vague/abstract tasks (e.g. "Analyze...", "Investigate..." -- NOT coding tasks)
- Hypothesis is not working right now (everywhere vague, abstract tasks)
- System costs much higher than SEC-bench

---

## Preconditions (Apply to ALL Experiments)

These must be in place before ANY experiment run. No exceptions.

### P1. Anti-Cheating Enforcement

Workers must NOT cheat. Forbidden actions:
- Web search for CVE patches
- `git checkout` or `git reset` to any commit other than the base commit
- `curl`/`wget` to fetch patches from external URLs
- `git log` to find fix commits (may only use `git show` on candidate_fixes provided in CVE context)
- Searching GitHub, NVD, or any external source for the patch

**Implementation**: Add explicit deny rules in worker prompts. Build a post-run audit script that greps `ThoughtCaptured` events for violations (`curl`, `wget`, `git checkout`, `git log --all`, `git branch -a`, web URLs).

**Denylist consideration**: The system uses whitelist-based `allowed_tools` (not denylist). Since worker tools (openhands, claude_code) have full shell access, restrictions must be enforced via prompt instructions. If prompt-only enforcement proves insufficient, consider adding a command-filtering wrapper in the container.

### P2. Security Tools (KLEE + Valgrind) Enabled

Every worker container must have Valgrind and KLEE installed.

**How it works** (two-layer build):
```
hwiwonlee/secb.eval.x86_64.{project}.{cve}   <-- pull from Docker Hub (eval image)
        |
        v  (secbench-tools.Dockerfile: apt install valgrind klee)
secb-tools:{project}.{cve}                    <-- local build, used for runs
```

- `deployment/build-secbench-tools.sh` builds the tools layer
- `plugins/security/image_resolver.py` routes to `secb-tools:*` when `security_tools_enabled=True`
- `prompts/domains/secbench/tools.j2` injects Valgrind/KLEE usage guidance into worker prompts

**Pre-build step** (for each CVE instance):
```bash
docker pull hwiwonlee/secb.eval.x86_64.{project}.{cve}
./deployment/build-secbench-tools.sh deployment/{instance}.json
docker run secb-tools:{project}.{cve} valgrind --version   # verify
```

### P3. Post-Run Audit (Automatic)

After every experiment run, automatically:
1. Grep worker events for anti-cheat violations
2. Verify KLEE/Valgrind were available in the container
3. Log cost breakdown (orchestration vs worker)
4. Record task concreteness metrics per depth level

---

## Docker Image Selection

| Image Pattern | Purpose | Use For |
|---------------|---------|---------|
| `hwiwonlee/secb.x86_64.{id}` | Verification pipeline | NOT for our experiments |
| `hwiwonlee/secb.eval.x86_64.{id}` | Evaluation harness (has secb, compile, PoC) | Base for our experiments |
| `hwiwonlee/secb.base` | Base layer | Dockerfile FROM target |
| `secb-tools:{project}.{cve}` | Eval + Valgrind + KLEE | **Actual image used for runs** |

Our `CVEInstance.docker_image` already constructs `hwiwonlee/secb.eval.x86_64.{project}.{cve}` -- the eval image. This is correct for reproducing SEC-bench evaluation results.

## CVE Instance Data

CVE instance data exists on HuggingFace (`SEC-bench/Seed` dataset). We do NOT create JSON files manually.

**What we need**: A conversion script that pulls from HuggingFace CSV and converts to `CVEInstance` JSON schema (adds `project_name`, `lang`, `work_dir`, `build_sh`, `secb_sh` which are extracted from the Docker images).

**Existing JSON files** in `deployment/`:
- `gpac-cve-2023-5586.json` (fully populated)
- `njs-cve-2022-32414.json` (fully populated)

---

## Execution Order

| Step | Phase | What | Prerequisite |
|------|-------|------|--------------|
| 1 | 0.1-0.2 | Docker + DB setup | None |
| 2 | P1+P2 | Anti-cheat prompts + Security tools (KLEE/Valgrind) + Pre-build secb-tools images | Step 1 |
| 3 | 0.3 | Smoke test (verify anti-cheat active, valgrind/klee work in container) | Steps 1-2 |
| 4 | 0.4 | HuggingFace -> CVEInstance JSON conversion script | SecVerifier data |
| 5 | 1 | Flat baseline (5 easy instances) | Steps 2-4 |
| 6 | 2 | Tree topology (5 easy instances) | Steps 2-4 |
| 7 | 4 | Cost analysis | Steps 5-6 |
| 8 | -- | Fix prompt issues if hypothesis fails | Step 7 |
| 9 | 5 | Full benchmark (60 runs) | Step 8 |
| 10 | 6 | Analysis and reporting | Step 9 |

---

## Phase 0: Infrastructure Validation

### 0.1 Docker Image Availability
- Pull eval images for test instances
- Build secb-tools images via `build-secbench-tools.sh`
- Verify `secb build`, `secb repro`, `secb patch` work inside container manually

### 0.2 Database and Runtime
- PostgreSQL event store running (`docker compose up -d postgres`)
- Verify arise-sec-lion starts: `python main.py summary`

### 0.3 Smoke Test (Single Instance)
- Run with `max_depth=1` (forces BOSS -> 3 WORKERs, mimics SEC-bench flat structure):
  ```
  python main.py run "<task>" --cve-file deployment/cve-instances/njs-cve-2022-32414.json --domain security
  ```
- Verify: events stored, workers spawned, Docker containers start, `secb` commands execute
- Verify: anti-cheat constraints are in prompts, Valgrind/KLEE available
- **Success gate**: System completes without crash. Workers attempt all 3 phases.

### 0.4 CVE Instance Conversion Script
- Write script: HuggingFace `SEC-bench/Seed` -> `CVEInstance` JSON files
- Extract `build_sh`, `secb_sh`, `work_dir` from Docker images
- Generate JSON files for all 20 selected test instances

---

## Phase 1: Flat Baseline (Direct SEC-bench Comparison)

**Purpose**: Establish our system's baseline when operating in SEC-bench-equivalent mode (no deep decomposition).

### Config: SEC-bench Equivalent Mode

Lives at `experiments/<flat-cell>/configs/*.yaml` (e.g. the B3 boss-direct cell). The live
N/B cells use the dotted-key override form (`orchestration.topology.max_depth: 1`, etc.);
the nested YAML below is shown for readability.

```yaml
# experiments/b3-boss-bef-direct/configs/B3-boss-bef-direct.yaml (shape)
orchestration:
  topology:
    max_depth: 1          # BOSS(0) -> WORKERs(1) only, no managers
    max_children_per_node: 3
    max_total_agents: 4   # 1 BOSS + 3 workers
  skip_judge: true
  max_run_duration_seconds: 7200  # Match SEC-bench timeout

worker:
  model: gpt-5.4-mini    # current worker model (B3/B4)
  tool: openhands        # Match SEC-bench scaffold
  timeout: 2400
  max_iterations_per_run: 250   # current B-cell value (B3:33, B4:46); was 75 to match SEC-bench
```

### Run on 5 "Easy" Instances
- Instance candidates (PASS in SecVerifier validation): `njs.cve-2022-32414`, `njs.cve-2022-38890`, `gpac.cve-2024-0321`, `libredwg.cve-2020-21816`, `njs.cve-2022-31307`
- **Metrics per run**:
  - Per-phase success (build exits 0, repro has sanitizer error, patch removes error)
  - Total cost (input/output tokens x model pricing)
  - Wall-clock time
  - Number of worker iterations per phase
  - Event count per agent

### Expected Output

| Instance | SEC-bench Result | Our Flat Result | Our Cost | SEC-bench Cost |
|----------|-----------------|-----------------|----------|----------------|
| njs.cve-2022-32414 | PASS | ? | ? | ~$0.87 |
| ... | ... | ... | ... | ... |

---

## Phase 2: Tree-Topology Hypothesis Validation

**Purpose**: Test whether deeper decomposition produces more concrete worker tasks and better results.

### Config: Tree Mode

Lives at `experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml`
(the live deep-tree cell; current values: `max_depth: 3`, `max_children_per_node: 7`,
`max_total_agents: 40`). Shape:

```yaml
# experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml (shape)
orchestration:
  topology:
    max_depth: 3          # BOSS(0) -> MANAGER(1) -> PENDING(2) -> WORKER(3)
    max_children_per_node: 7
    max_total_agents: 40
  concurrency:
    max_concurrent_workers: 1  # Sequential for sibling context
```

### Hypothesis Test Metrics

Run the same 5 instances with tree topology. For each run, extract from event store:

| Metric | How to Measure | Expected |
|--------|---------------|----------|
| Task description length | Chars in `TaskAssigned.task_description` | Shorter at deeper levels |
| Task concreteness | Manual coding: abstract(0)/mixed(1)/concrete(2) | Higher score at deeper levels |
| Action verbs | Count "Analyze/Investigate" vs "Write/Create/Run" | More action verbs at deeper levels |
| Worker iteration count | Count `ThoughtCaptured` events per worker | Fewer iterations = more concrete task |
| Worker success rate | `WorkCompleted` vs `WorkFailed` | Higher with concrete tasks |
| Sibling context usage | `ChildCompleted.result` length passed to next sibling | Non-trivial content passed |

### Task Concreteness Analysis (Critical)

For each event in the tree, categorize:
- **Level 0 (BOSS)**: "Reproduce and patch CVE-XXXX" -- most abstract
- **Level 1 (MANAGER)**: "[Builder] Build the vulnerable project" -- phase-scoped
- **Level 2 (sub-MANAGER or WORKER)**: "Run `./src/build.sh`, capture stderr, fix any `-std=c++14` errors" -- concrete
- **Level 3 (WORKER)**: Single-command or single-file tasks -- atomic

**Query**:
```sql
SELECT agent_id, depth, role, task_description, status
FROM agent_events WHERE run_id = ? ORDER BY depth, created_at;
```

### Prompt Gap Diagnosis

If tasks remain vague at deeper levels, root cause is in prompt chain:
1. `assess.j2`: Does it give clear criteria for "concrete enough to execute"?
2. `decomposition.j2`: Does it instruct LLM to make subtasks MORE specific than parent?
3. Worker templates (`worker/builder.j2`, `worker/exploiter.j2`, `worker/fixer.j2`, `worker/reporter.j2`): Enough concrete subtask examples?

**Fix direction**: Decomposition prompt must require each subtask is strictly more concrete than parent, with exact commands, file paths, and success criteria.

---

## Phase 3: Anti-Cheating Enforcement

> NOTE: This phase executes as a precondition (Step 2) BEFORE all experiments.

### Threat Model

| Cheat Vector | Risk | Mitigation |
|-------------|------|------------|
| Web search for CVE patches | Finds fix directly | Forbid in prompt, audit for curl/wget/browser |
| `git checkout <fix_commit>` | Gets patched code | Forbid in prompt, audit for git checkout |
| `curl`/`wget` patch URL | Downloads fix | Forbid in prompt, audit for URLs |
| Reading `candidate_fixes` in Builder/Exploiter | Leaks fix info | Only Fixer should access candidate_fixes |

### Implementation
- Add `<constraints>` block to worker prompts with explicit forbidden actions
- Post-run audit script greps `ThoughtCaptured` events for violations
- Flag any run with cheating indicators in results table

### Validation
After each run, grep worker events for:
- `curl`, `wget`, `git checkout`, `git log --all`, `git branch -a`
- External URLs (github.com, nvd.nist.gov, etc.)
- Any `git show` by Builder or Exploiter agents (only Fixer may use it)

---

## Phase 4: Cost Analysis and Optimization

### Cost Breakdown per Run

| Cost Category | How | SEC-bench Equivalent |
|---------------|-----|---------------------|
| BOSS assessment LLM call | `TokensConsumed` events where role=BOSS | N/A (SEC-bench has no BOSS) |
| MANAGER decomposition LLM calls | `TokensConsumed` where role=MANAGER | N/A |
| PENDING assessment LLM calls | `TokensConsumed` where role=PENDING | N/A |
| Worker execution costs | `WorkerCostRecorded` events | Direct comparison |
| **Overhead ratio** | (BOSS + MANAGER + PENDING) / Total | Should be < 20% |

### Cost Optimization Levers

| Lever | Current | Target | Rationale |
|-------|---------|--------|-----------|
| BOSS model | gpt-5.4 | gpt-5.4-mini | Only does the phase split; cheaper model may suffice |
| MANAGER model | gpt-5.4 | gpt-5.4-mini | Managers only decompose |
| Worker model | gpt-5.4-mini | gpt-5.4-mini (keep) | Current worker model for B-cells |
| Worker iterations | 250 | tune down | B-cells run 250; evaluate whether fewer suffice |
| Total agents | 40 | 10-25 | Reduce orchestration overhead |
| max_depth | 3 | 2 | Test if shallower tree is enough |

### Cost Targets
- SEC-bench average: **$0.87/instance** (verification), **$0.61-$1.56/instance** (evaluation)
- Our flat baseline target: **< $1.50/instance**
- Our tree mode target: **< $3.00/instance** (accounting for orchestration overhead)

---

## Phase 5: Systematic Benchmark Runs

### Instance Selection (20 instances)

| Category | Count | Selection Criteria |
|----------|-------|-------------------|
| Easy (SEC-bench PASS) | 5 | `njs` and `faad2` (highest verification success) |
| Medium (SEC-bench FAIL) | 5 | `gpac` and `mruby` (moderate success) |
| Hard (SEC-bench ERROR) | 5 | `imagemagick` and `exiv2` (complex codebases) |
| Diverse CWE | 5 | 1 each of CWE-125, CWE-787, CWE-476, CWE-416, CWE-190 |

### Experiment Configurations (3 configs x 20 instances = 60 runs)

| Config | Topology | Agents | Model | Purpose |
|--------|----------|--------|-------|---------|
| A: Flat | depth=1, 4 agents | openhands | gpt-5.4-mini worker | SEC-bench equivalent baseline |
| B: Shallow tree | depth=2, ~10 agents | openhands | gpt-5.4 boss/mgr, gpt-5.4-mini worker | Moderate decomposition |
| C: Deep tree | depth=3, ~40 agents | openhands | gpt-5.4 boss/mgr, gpt-5.4-mini worker | Full hypothesis test |

### Metrics per Run

```json
{
  "instance_id": "njs.cve-2022-32414",
  "config": "B",
  "builder_success": true,
  "exploiter_success": true,
  "fixer_success": false,
  "total_cost_usd": 2.34,
  "orchestration_cost_usd": 0.45,
  "worker_cost_usd": 1.89,
  "total_agents_spawned": 8,
  "max_depth_reached": 2,
  "wall_clock_seconds": 1200,
  "worker_iterations_total": 42,
  "cheating_flags": [],
  "task_concreteness_scores": [0, 1, 2, 2],
  "events_count": 87
}
```

---

## Phase 6: Hypothesis Refinement

### Statistical Comparison

| Metric | Config A (Flat) | Config B (Shallow) | Config C (Deep) |
|--------|----------------|-------------------|-----------------|
| Builder success % | ? | ? | ? |
| Exploiter success % | ? | ? | ? |
| Fixer success % | ? | ? | ? |
| Overall success % | ? | ? | ? |
| Avg cost/instance | ? | ? | ? |
| Avg wall-clock time | ? | ? | ? |

### Hypothesis Validation Criteria

**Confirmed** if:
1. Config B or C has higher overall success % than Config A
2. Task concreteness scores increase monotonically with depth
3. Workers in deeper configs have fewer iterations per task
4. Orchestration cost overhead < 30% of total cost

**Refuted** if:
1. Config A outperforms B and C
2. Deeper decomposition produces redundant or conflicting worker tasks
3. Orchestration overhead exceeds 50% of total cost

### Potential Gaps in Hypothesis

| Gap | Risk | Mitigation |
|-----|------|------------|
| Over-decomposition | Breaking simple tasks into too many pieces wastes budget | Set min complexity threshold -- force WORKER if task is simple enough |
| Context loss | Deep trees lose parent context at leaf workers | Verify briefing chain carries full context through all levels |
| Sibling coordination failure | Workers don't know what predecessors did | Verify handoff mechanism passes useful data |
| Exploiter is inherently hard | SEC-bench 39.4% bottleneck; decomposition may not help for PoC crafting | Focus tree depth on Fixer (most decomposable), keep Exploiter shallow |
| Phase coupling | Builder output needed by Exploiter/Fixer; cross-phase deps must be sequential | Ensure `depends_on` is wired for cross-phase subtasks |

---

## Critical Fixes Needed Before Experiments

> Two items from the original list have shipped and were removed: **anti-cheating
> constraints** (now in the prompt forbidden-surfaces block — `prompts/domains/secbench/_secb_runtime_contract.j2:11`,
> plus manager-level `secb`-only discipline in `manager.j2:39-41`) and the **worker
> iteration budget** (B-cells already run `max_iterations_per_run: 250`, well above
> SEC-bench's 75). The remaining open items:

1. **Task concreteness in decomposition prompts** -- partially addressed (manager/assess prompts already push `secb`-anchored concrete subtasks). Confirm the generic `decomposition.j2` itself enforces that each subtask is strictly more concrete than its parent (≥1 specific command, file path, or exact criterion).

2. **Cost budget enforcement** -- still missing: no config-level budget guard exists (`config/settings.py` has no `cost_budget`/`max_cost` key). Add an explicit setting and execution-loop check before relying on cost caps.

3. **SEC-bench correctness in the verification pipeline** -- deterministic/execution stages are still placeholders that always pass (`core/application/services/orchestration/verification_pipeline.py:181,186`), and the post-hoc Built/Exploited/Fixed success judges are UNWIRED ([`SYSTEM_REFERENCE.md`](../SYSTEM_REFERENCE.md) §IV.6). To gate on SEC-bench correctness, wire independent host-side re-execution of `secb build`/`secb repro`/`secb patch` and thread the CVE oracle into `RunData` (SYSTEM_REFERENCE §V.2, §V.4).

---

## Reference: System Architecture

```
BOSS (root, depth 0)
  | decomposes into 3 phases
  v
PENDING (depth 1) ------> assess_task() decides EXECUTE vs DECOMPOSE
  |--- EXECUTE --> WORKER (runs secb commands directly)
  |--- DECOMPOSE --> MANAGER
                      | spawns sub-tasks
                      v
                    PENDING (depth 2) --> ...recursive
```

**Current config** (B4 deep-tree cell): gpt-5.4 (BOSS), gpt-5.4 (MANAGER), gpt-5.4-mini + openhands (WORKER)
**Topology**: max_depth=3, max_children_per_node=7, max_total_agents=40
**Concurrency**: Sequential workers (max_concurrent_workers=1)

## Reference: SEC-bench `secb` Tool

Located at `/usr/local/bin/secb` inside Docker containers:
- `secb build` -- Compiles project with sanitizer flags via `/usr/local/bin/compile`
- `secb repro` -- Runs the `repro()` function to trigger vulnerability
- `secb patch` -- Applies `/testcase/model_patch.diff` and rebuilds
