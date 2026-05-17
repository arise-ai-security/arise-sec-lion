# B1 Batch Autogen — Experiment Findings

**Study ID:** `b1-batch-autogen`
**Completed:** 2026-05-17
**Cell:** B1 (hierarchical, skip\_judge=true, claude\_code worker)

---

## 1. Configuration

### Architecture

| Layer | Role |
|---|---|
| Boss | Assesses top-level task, decomposes into 4 phase managers |
| Manager | Phases: Builder → Exploiter → Fixer → Reporter |
| Worker | Leaf executor (claude\_code SDK adapter) |

Orchestration mode: `hierarchical`
Judge stage: **disabled** (`skip_judge: true`)

### Model

All three levels use the same model:

```
boss.model:    claude-sonnet-4-5-20250929
manager.model: claude-sonnet-4-5-20250929
worker.tool:   claude_code
worker.model:  claude-sonnet-4-5-20250929
```

`max_tokens: 16000` at all levels (boss, manager, worker).
Temperature: not overridden (base config default).

### Timeouts and Limits

| Parameter | Value | Source |
|---|---|---|
| `orchestration.max_run_duration_seconds` | **5400 s (90 min)** | B1 cell config override |
| `worker.timeout` | 3600 s (60 min) | B1 cell config override |
| `worker.max_iterations_per_run` | 250 | B1 cell config override |
| `orchestration.max_iterations` (boss/manager/pending) | 20 | B1 cell config override |
| `orchestration.max_retries` | 3 | base config |
| `orchestration.max_redecompositions` | 2 | base config |
| `orchestration.topology.max_depth` | 3 | base config |

### Worker Tool Permissions

```yaml
worker.allowed_tools:    [Read, Write, Edit, MultiEdit, Bash, Glob, Grep]
worker.disallowed_tools: [WebSearch, WebFetch]
```

### Execution Parameters (batch runner)

| Parameter | Value |
|---|---|
| `--parallel` | 6 concurrent runs |
| `--replicates` | 1 |
| Batch size | 12 tasks/batch |

---

## 2. Dataset

**Name:** `secbench-b1-no-gpac-no-imagemagick-no-songtli`
**Total CVEs:** 122 in dataset definition; **124 runs executed** (11 credit-failure reruns counted separately)
**Unique CVEs run:** 120 (target met)

Libraries covered (23 total):

| Library | CVEs run | Success | Timeout | Failed |
|---|---|---|---|---|
| exiv2 | 10 | 10 | 0 | 0 |
| faad2 | 12 | 10 | 0 | 2 |
| gpac | 10 | 7 | 0 | 3 |
| jq | 1 | 0 | 1 | 0 |
| libarchive | 4 | 0 | 3 | 1 |
| libheif | 2 | 2 | 0 | 0 |
| libiec61850 | 2 | 2 | 0 | 0 |
| libjpeg-turbo | 1 | 1 | 0 | 0 |
| liblouis | 1 | 0 | 1 | 0 |
| libmodbus | 1 | 1 | 0 | 0 |
| libplist | 1 | 1 | 0 | 0 |
| libredwg | 25 | 9 | 11 | 5 |
| libsndfile | 1 | 1 | 0 | 0 |
| libxls | 1 | 1 | 0 | 0 |
| matio | 7 | 5 | 2 | 0 |
| md4c | 3 | 3 | 0 | 0 |
| mruby | 12 | 3 | 1 | 8 |
| njs | 17 | 15 | 2 | 0 |
| openexr | 3 | 2 | 1 | 0 |
| openjpeg | 5 | 5 | 0 | 0 |
| qpdf | 1 | 0 | 1 | 0 |
| readstat | 1 | 1 | 0 | 0 |
| upx | 3 | 2 | 1 | 0 |
| **TOTAL** | **124** | **81** | **24** | **19** |

---

## 3. Overall Results

| Outcome | Count | Rate |
|---|---|---|
| success | 81 | 65.3% |
| timeout | 24 | 19.4% |
| failed | 19 | 15.3% |
| **total** | **124** | |

---

## 4. Success Criteria (per run)

A run is judged `success` / `timeout` / `failed` by the system based on whether the hierarchical agent completes all 4 phases (Builder → Exploiter → Fixer → Reporter) within the 90-minute wall-clock limit and produces required deliverables.

### Expected testcase artifacts

| Artifact | Present in | Notes |
|---|---|---|
| `patch_validation_results.txt` | 95 / 115 manifest runs | Written by `[Patch-Validator]` worker via `worker/fixer.j2` |
| `exploit_validation_results.txt` | **1 / 115** manifest runs | See §5 — systemic bug |
| `repro.sh` | majority of success runs | Written by `[Repro-Creator]` worker |
| `model_patch.diff` | majority of success runs | Written by `[Patch-Creator]` worker |

---

## 5. Known Issues Discovered During Run

### 5.1 `exploit_validation_results.txt` never generated (systemic)

**Symptom:** Only 1 of 115 runs produced `/testcase/exploit_validation_results.txt`. Instead workers use ad-hoc names: `exploit_validation.txt` (21×), `validation_summary.txt` (19×), `exploit_validation_report.txt` (10×), and 15+ other variants.

**Root cause:** The filename specification lives in `prompts/domains/secbench/manager/exploiter.j2`, which is dead code on the happy path. The `[Exploit-Validator]` worker receives its task from `assess.j2`, which names the role but specifies no output filename. `worker/exploiter.j2` (the only template the worker actually sees) lists `repro.sh` and `poc.*` as deliverables but omits `exploit_validation_results.txt`.

**Contrast with patch:** `patch_validation_results.txt` is specified explicitly in `worker/fixer.j2:130` and is present in 95/115 runs.

**Fix:** Add the exact filename and machine-readable block format to `worker/exploiter.j2` deliverables section (and/or to the `[Exploit-Validator]` role description in `assess.j2`).

### 5.2 `manager/*.j2` templates are dead code on the happy path

**Finding:** The rendering pipeline never calls `evaluate_task` for MANAGER-role agents on the happy path. The PENDING agent runs `assess_task`, the LLM returns `action="decompose"`, and `_apply_assessment_result` (`agent_orchestrator.py:878-887`) spawns children and transitions the agent to `WAITING` — bypassing `evaluate_task` entirely. `manager/exploiter.j2`, `manager/builder.j2`, and `manager/fixer.j2` are only reachable via `trigger_redecomposition` (child returns infeasible → parent re-enters `ANALYZING`).

**Implication:** All decomposition guidance must live in `assess.j2`. Role-specific deliverable contracts belong in the corresponding `worker/*.j2` templates.

### 5.3 libredwg timeout rate (44%)

25 libredwg CVEs run; 11 timed out (44%), 5 failed, only 9 succeeded. libredwg tasks are significantly harder than other libraries — likely due to large codebase size, complex DWG format parsing, and deep call chains that cause the Exploiter phase to exhaust its time budget.

### 5.4 mruby failure rate (67%)

12 mruby CVEs run; 8 failed, 1 timeout, 3 success. High failure rate; failures occurred in the later 2022-era CVEs (`cve-2022-*`). Likely image or runtime environment issues, or the Ruby VM's dynamic nature making exploit/patch harder.

### 5.5 Batch 11 credit exhaustion

11 of 12 batch 11 tasks failed due to Anthropic API credit balance depletion. These were re-run as batch 11b after credits were restored. The credit failures are infrastructure failures, not task failures — re-running was appropriate.

---

## 6. Prompt Rendering Pipeline (reference)

```
BOSS      → assess.j2 + boss.j2                    (always)
PENDING   → assess.j2                               (always — only prompt the manager sees)
MANAGER   → manager.j2 + manager/{role}.j2          (dead on happy path; redecomp only)
WORKER    → worker.j2 + worker/{role}.j2 + tools.j2 (always)
```

All levels share: `system.j2` + `roles/{role}.j2` + `operations/{assess|decompose|execution}.j2` + `domains/secbench/cve.j2`.

---

## 7. Infrastructure Notes

- Runs stored under `runs/{run_id}/`; source trees under `runs/{run_id}/src/` (63 GB total across 162 src dirs)
- Total disk used by experiment: ~103 GB (`runs/`: 40 GB + `src/` dirs: 63 GB)
- Disk available at conclusion: ~366 GB free of 926 GB
- Database: PostgreSQL `arise_events`, table `events`; ~124K total events at study end
- Batches 1–10 ran at `--parallel 4` (batches 1–8) then `--parallel 6` (batches 9–11)
