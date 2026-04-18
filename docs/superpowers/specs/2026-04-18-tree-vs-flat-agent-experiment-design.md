# Tree-vs-Flat Agent Orchestration Experiment: Design Document

**Status:** Pre-registered (locked before any run).
**Author:** nearKim (garfield@snu.ac.kr).
**Date:** 2026-04-18.
**Scope:** Experiment 1 (observational). Experiment 2 (manager tool-calling intervention) is a conditional follow-up explicitly out of scope for this document.

---

## 0. Executive Summary

This design describes a scientific experiment comparing two agent systems on 10 stratified SEC-bench CVE instances:

- **System A — Flat baseline:** Claude Code CLI (`claude`), invoked with a single upfront prompt inside the SEC-bench Docker container. Subagents (Task tool) toggled on/off.
- **System B — Tree orchestration:** arise-sec-lion's multi-agent tree (BOSS / MANAGER / WORKER) wrapping the Claude Agent SDK.

Both systems use **Claude Sonnet 4.6** as the worker LLM, run inside the same `secb-tools:{project}.{cve}` container, have access to the same security tools (Valgrind + KLEE), and operate under the same $3 / 10-minute budget cap per run.

**Structure:** The experiment is divided into two pillars.

- **Pillar A** generates a versioned, self-describing dataset artifact from 63 runs (6 cells × 10 CVEs + 3 anchor replicates).
- **Pillar B** consumes the dataset, computes metrics, performs pre-registered comparisons, and produces a report.

This document specifies Pillar A in full. Pillar B's metrics and analysis plan are specified here as the consumer contract (§5, §6); Pillar B's implementation is deferred to a separate follow-up document.

---

## 1. Systems Under Test

### 1.1 System A — Flat Claude Code CLI (baseline)

| Property | Value |
|---|---|
| Binary | `@anthropic-ai/claude-code` (npm), installed in a derived Docker image |
| Docker image | `secb-tools:{project}.{cve}` + Node.js + Claude Code CLI |
| Invocation | `claude --print --output-format stream-json --model claude-sonnet-4-6 --permission-mode bypassPermissions` |
| Working directory | `/src` (inside container) |
| Model | Claude Sonnet 4.6, temperature=0, extended thinking 8K |
| Tools | CLI defaults — Bash, Read, Edit, Write, Glob, Grep, WebFetch, TodoWrite, Task (subagents), Plan mode |
| System prompt | CLI default (not overridden) |
| Subagent toggle | ON (default) vs. OFF (`--disallowedTools Task`) |
| Network policy | `--network=none` after image build (blocks external fetches) |
| Capture | `stream-json` parsed → normalized `events.jsonl` via `flat_cli_harness.py` |
| Termination | Task completion OR `$3` cost cap OR 10-min wall-clock cap |

### 1.2 System B — Tree (arise-sec-lion)

| Property | Value |
|---|---|
| Engine | Claude Agent SDK via `claude_sdk_adapter.py` |
| Worker config | `worker.tool: claude` (switched from current OpenHands) |
| Docker image | Same `secb-tools:{project}.{cve}` via existing `DockerSecBenchRuntime` |
| Models | BOSS = Claude Opus 4.7; MANAGER = Claude Opus 4.7; WORKER = Claude Sonnet 4.6 |
| Temperature | 0 all roles |
| Extended thinking | 8K budget all roles |
| Prompt strategy | `NullPromptStrategy` (new, §4.3) vs. `SecBenchPromptStrategy` |
| Worker tool set | Bash, Read, Edit, Write, Glob, Grep — **no TodoWrite, no Task** (orchestrated externally by the tree) |
| System prompt | Role-optimized per §4.8 |
| Topology | `max_depth=2`, `max_children_per_node=5`, `max_total_agents=15`, `max_concurrent_workers=1` |
| Verification | `skip_judge=true` (judge cost still measured; skip to remove confound from pass rate) |
| Termination | Task completion OR `$3` cost cap OR 10-min wall-clock cap |

### 1.3 Methodological note on asymmetry

Both arms use **Claude Sonnet 4.6** as the worker LLM — same model, same API, same Docker execution environment, same security-tool access, same budget caps. The CLI bundles its own self-orchestration tools (TodoWrite, Task/subagents, Plan mode); the tree provides external orchestration with a minimal per-worker tool set. This is the *intended* experimental contrast — *external structured orchestration vs. Claude's own internal self-management* — not a confound.

---

## 2. Factorial Design & Cells

**6 cells × 10 CVEs = 60 primary runs; + 3 anchor-CVE replicates on A1 = 63 total runs.**

|   | Flat CLI, subagents ON | Flat CLI, subagents OFF | Tree (System B) |
|---|---|---|---|
| **No domain prompt** | A1 | A2 | B1 |
| **Canonical briefing** | A3 | A4 | B2 |

**Primary comparisons (pre-registered):**

1. **A1 vs. B2** — headline: "Claude Code as a user gets it" vs. "your system at full power"
2. **A3 vs. B2** — isolates orchestration-vs-self-orchestration at matched domain-knowledge
3. **A1 vs. A3** — value of domain briefing to flat baseline
4. **B1 vs. B2** — value of domain briefing to tree
5. **A2 vs. A1** — value of Claude's self-orchestration (subagents)
6. **A2 vs. B1** — "both systems stripped of extras": pure tree vs. pure single Claude

**Cell order randomization:** cells shuffled within each CVE; fixed seed for reproducibility.

**Budget per cell per CVE:** $3 USD; 10-min wall-clock. Total expected cost $150-200.

**Anchor-CVE variance estimate:** 3 replicates of A1 on CVE #5 (`openjpeg.cve-2016-7445`) for within-cell variance.

---

## 3. CVE Instance Selection

### 3.1 Selection dimensions

1. Difficulty stratum — SEC-bench SECVERIFIER PASS (easy) / FAIL (medium) / ERROR (hard)
2. Project diversity — span ≥ 5 distinct projects
3. Sanitizer diversity — include address + at least one of {memory, undefined}
4. Bug class diversity — cover ≥ 4 CWEs
5. Image availability — eval image exists on Docker Hub
6. CVE-data completeness — `bug_description` populated, `has_gold_patch=True`

### 3.2 Label source

SEC-bench HuggingFace dataset `SEC-bench/Seed`. Fetched during Phase 0; if unavailable, proxied by `lines_of_code × sanitizer_strictness` (flagged as limitation).

### 3.3 Locked primary list (tentative — labels verified in Phase 0)

| # | Instance | Project | Sanitizer | CWE | Stratum | Source |
|---|---|---|---|---|---|---|
| 1 | `njs.cve-2022-32414` | njs | address | CWE-476 | EASY | `deployment/` |
| 2 | `njs.cve-2022-38890` | njs | address | CWE-125 | EASY | HF |
| 3 | `faad2.cve-2021-32272` | faad2 | address | CWE-125 | EASY | `fixtures/` |
| 4 | `faad2.cve-2018-20196` | faad2 | address | CWE-787 | EASY | `fixtures/` |
| 5 | `openjpeg.cve-2016-7445` | openjpeg | address | CWE-476 | MEDIUM | `deployment/` |
| 6 | `gpac.cve-2021-40575` | gpac | address | CWE-787 | MEDIUM | `fixtures/` |
| 7 | `mruby.cve-2022-0240` | mruby | address | CWE-416 | MEDIUM | `deployment/` |
| 8 | `gpac.cve-2022-1795` | gpac | address | CWE-125 | MEDIUM | `fixtures/` |
| 9 | `exiv2.cve-2017-14859` | exiv2 | address | CWE-416 | HARD | `fixtures/` |
| 10 | `imagemagick.cve-2019-13309` | imagemagick | address | CWE-125 | HARD | `deployment/` |

### 3.4 Backup list

In priority order: `njs.cve-2022-31307`, `faad2.cve-2021-32278`, `gpac.cve-2021-32437`, `exiv2.cve-2017-14857`, one available `openjpeg.*`.

### 3.5 Anchor CVE

`#5 openjpeg.cve-2016-7445` — median difficulty, mid-size repo, well-documented bug.

### 3.6 Locking protocol

Final locked list shipped as `experiments/locked_instances.yaml`, committed before any run. Every run records the locked file's SHA in `meta.json`. If an instance proves non-runnable, substitute from backup list, documented in the dataset changelog.

---

## 4. Pillar A Pre-work

All items below must land before the first run executes.

### 4.1 Instrumentation gap fixes (~200 LoC, 1 day)

Based on the metric-capture audit, the following gaps must be closed:

| # | File | Change | Operation label |
|---|---|---|---|
| 1 | `infrastructure/adapters/worker/claude_sdk_adapter.py:215-239` | Parse `cache_creation_input_tokens`, `cache_read_input_tokens`, reasoning/thinking tokens from `ResultMessage.usage`. Pass all 5 fields to `cost_recorded()`. | `worker_execution` (existing) |
| 2 | `core/application/services/orchestration/verification_pipeline.py:217, 225` | Replace direct `llm_port.query_with_usage()` with `_query_llm()`-equivalent that emits `TokensConsumed`. | `operation="verification"` (new) |
| 3 | `core/application/services/orchestration/context_condenser.py:189-192` | Change `query()` → `query_with_usage()`; emit `TokensConsumed`. | `operation="context_condense"` (new) |
| 4 | `infrastructure/adapters/worker/claude_sdk_adapter.py` | Add `PreToolUse` hook; record `call_id` + start timestamp; in `PostToolUse`, compute `duration_ms`. | Extend `ThoughtCaptured`: add optional `call_id`, `duration_ms` |
| 5 | `core/domain/events/events.py` + both adapters | Add optional `was_truncated: bool`, `result_bytes: int \| None` to `ThoughtCaptured`. Populate in adapters. | — |
| 6 | `core/domain/events/events.py` + both adapters | Add optional `tool_input_json: dict \| None` to `ThoughtCaptured` for full structured args. Raise `tool_result` cap to 10KB. | — |

All changes are backward-compatible: new fields are optional, operation labels are additive.

### 4.2 Flat CLI harness (~300 LoC, 1 day)

**New file:** `experiments/baselines/flat_cli_harness.py`

**Responsibilities:**

- Spawn container from `secb-tools:{project}.{cve}` with workspace bind-mount
- `docker exec` the CLI: `claude --print --output-format stream-json --model claude-sonnet-4-6 --permission-mode bypassPermissions --disallowedTools <list>`
- Parse stdout line-by-line (JSONL stream-json)
- Normalize each line → unified `events.jsonl` schema (§7.6)
- Emit synthetic `run_started` / `run_completed` events at stream boundaries
- Parse final `result` message's `usage` → emit `tokens_consumed` with full cache breakdown
- Enforce $3 cost cap and 10-min wall-clock cap; kill and emit `run_completed(status="timed_out")` if exceeded
- Prepend security-tool preamble to task prompt (§4.9)
- Write all records to `dataset/runs/<cve>/<cell>/<replicate>/events.jsonl`

**Schema module:** `experiments/schema.py` — Pydantic models matching the normalized event schema, used by both harness and tree path.

**Auth:** CLI reads `ANTHROPIC_API_KEY` env var passed via `docker run -e`.

### 4.3 NullPromptStrategy (~30 LoC)

**New file:** `plugins/security/null_prompt_strategy.py`

Implements `PromptStrategy` protocol; all `extend_*_prompt` methods return `None`. Core falls back to default `roles/{boss,manager,worker}.j2` templates.

**Wiring:** add `settings.security.prompt_strategy: "default" | "null"`. `bootstrap/composition.py` picks which to instantiate. Backward-default = `"default"`.

### 4.4 Canonical domain-briefing doc

**New file:** `experiments/domain_briefing.md`

**Sections:**

1. Task context (what SEC-bench is, what an instance contains)
2. Phase definitions (Builder / Exploiter / Fixer deliverables + mechanical success criteria)
3. Anti-cheat rules (forbidden actions enumerated)
4. Available tools (`secb` harness commands, Valgrind / KLEE hints, workspace paths)
5. Output format contract (where to write `repro.sh`, `model_patch.diff`)

**Usage:**

- Flat arm (cells A3, A4): prepended to task prompt verbatim
- Tree arm (cells B2): Jinja includes pull content from this file; single source of truth

**Template refactor:** `prompts/domains/secbench/cve.j2` and phase templates `{% include 'experiments/domain_briefing.md' %}` replace duplicated text.

### 4.5 Anti-cheat audit script (~80 LoC)

**New file:** `experiments/audit_cheating.py`

Reads `events.jsonl`; greps tool-use events for:

- `git checkout` with non-base-commit hash
- `git log --all`, `git branch -a`, `git show` of non-candidate_fixes commits
- `curl` / `wget` with external URLs
- `WebFetch` to external URLs
- References to known patch sources (github.com/*/commit/*, nvd.nist.gov)

Outputs `audit.json` per run with `{violated: bool, violations: [...]}`. Runs flagged but not excluded.

### 4.6 Experiment runner (~200 LoC)

**New file:** `experiments/run_experiment.py`

**Entry point:**

```bash
python experiments/run_experiment.py \
  --locked-instances experiments/locked_instances.yaml \
  --cells all \
  --output-dir dataset/runs/ \
  --seed 42
```

**Responsibilities:**

- Load locked CVE list; shuffle cell assignment per CVE with fixed seed
- For each (CVE, cell, replicate):
  1. Pull + build `secb-tools` image if missing
  2. Dispatch to `flat_cli_harness.py` (A*) or `main.py run ...` (B*) with appropriate config
  3. Capture JSONL output
  4. Run Track-1 mechanical evaluator (`secb build/repro/patch`) → `mechanical.json`
  5. Run `audit_cheating.py` → `audit.json`
  6. Write `meta.json`
- Emit progress log
- Resume-safe: skip combos with existing `events.jsonl`

### 4.7 Pre-registration lock

Before the first run, commit:

- `experiments/locked_instances.yaml`
- `experiments/pre_registration.yaml` (frozen primary hypothesis, primary metric, analysis plan, Exp-2 trigger thresholds — all from §6)
- Design-doc commit SHA recorded in every run's `meta.json`

### 4.8 Role prompt optimization (~1-2 hours, manual)

**Target files** (tree system only — flat CLI uses its built-in defaults):

- `prompts/system.j2`
- `prompts/roles/boss.j2`
- `prompts/roles/manager.j2`
- `prompts/roles/worker.j2`
- `prompts/operations/assess.j2`
- `prompts/operations/evaluate.j2` (or equivalent)

**Not touched:** `prompts/context/*.j2` (structured data templates), `prompts/domains/secbench/*.j2` (factorial variable).

**Process:**

1. Archive current prompts to `experiments/role_prompts/pre_opt/`
2. For each target:
   - Open `platform.openai.com/chat/edit?models=gpt-5&optimize=true`
   - Paste current prompt + contextual blurb (role, upstream/downstream context, required Jinja variables, output format)
   - Accept optimized output
   - Re-insert Jinja placeholders exactly where they appeared
3. Validate rendering via `python main.py prompts --last`
4. Dry-run smoke test: one cell-B2 run on anchor CVE; inspect decomposition, worker execution, parser correctness
5. **One iteration allowed** on broken prompts; then lock
6. Commit with message `Lock optimized role prompts for experiment`; archive to `experiments/role_prompts/post_opt/`

**Methodology note (goes into report):** Role prompts optimized via OpenAI GPT-5 Prompt Optimizer; cross-model optimization patterns documented; pre-opt baseline preserved in git.

### 4.9 Security tool parity (common infrastructure, not a factor)

Both systems must advertise Valgrind + KLEE; both must run in `secb-tools:*` images.

**Changes:**

1. `SecurityDomainPlugin.__init__`: add `inject_tool_guidance_always: bool = True`. `enrich_prompt` injects `tools.j2` regardless of `PromptStrategy`. Wiring in `bootstrap/composition.py`.
2. `flat_cli_harness.py`: prepend identical boilerplate to task prompt:

   ```
   AVAILABLE SECURITY TOOLS

   You have access to the following tools via Bash:
   - valgrind: memory-error detector (buffer overflows, leaks, use-after-free)
     Example: valgrind --error-exitcode=1 ./target_binary <args>
   - klee: symbolic execution engine for automatic test-input generation
     Example: klee --only-output-states-covering-new target.bc

   You MUST use valgrind to verify your exploit reproduces the memory error, and
   to verify your patch eliminates it. Use klee only if symbolic execution is
   warranted by the task.
   ```

**Force level:** soft prompt ("You MUST") + evaluation criterion in manual rubric (§5.9). Not runtime-enforced.

**Metric additions:** `security_tool_adoption` (binary), `security_tool_invocations` (count). Reported descriptively, not tested as hypothesis.

### 4.10 Timeline

| Day | Pillar | Work |
|---|---|---|
| 1 | A | §4.1 items 1-6 + §4.3 NullPromptStrategy |
| 2 | A | §4.2 flat CLI harness + smoke run |
| 3 | A | §4.4 briefing + §4.5 audit + §4.6 runner |
| 3.5 | A | §4.8 prompt optimization + dry-run validation |
| 4 | A | §4.7 pre-registration lock + integration test |
| 5-8 | A | Execute 63 runs (resume-safe) |
| 9 | A | Dataset finalization: tarball, SHA, versioning |
| 10-14 | B | Pillar B: metric computation + report (separate doc) |

---

## 5. Metrics Catalog (Pillar B consumer contract)

Every metric defined formally and computed identically for both systems except where asymmetry is noted. Primary metrics **bolded**.

### 5.1 Mechanical success (Track 1)

| Metric | Definition | Unit |
|---|---|---|
| `builder_pass` | `secb build` exits 0 in final workspace | {0, 1} |
| `exploiter_pass` | `secb repro` triggers same sanitizer-error-class as `expected_sanitizer_error` | {0, 1} |
| `fixer_pass` | `secb patch` applies, rebuild succeeds, `repro.sh` no longer triggers error | {0, 1} |
| **`end_to_end_pass`** | All three above = 1 | {0, 1} |

### 5.2 Token consumption

| Metric | Applies to | Definition |
|---|---|---|
| `tokens_total` | Both | Sum of `input_tokens + cache_read + cache_create + output_tokens + thinking_tokens` across all LLM calls |
| `tokens_input_fresh` | Both | `input_tokens` (uncached) |
| `tokens_input_cached` | Both | `cache_read + cache_creation` |
| `tokens_output` | Both | `output_tokens` |
| `tokens_thinking` | Both | `reasoning_tokens` / extended-thinking tokens |
| **`cost_usd_total`** | Both | SDK-reported `total_cost_usd` (accurate) |
| `tokens_by_role` | **Tree only** | Split by {BOSS, MANAGER, WORKER, PENDING, JUDGE, CONDENSER} |
| `tokens_by_operation` | **Tree only** | Split by {assess, decompose, worker_execution, verification, context_condense} |

### 5.3 Tool-call metrics

Computed from `tool_use` events (tree) or parsed blocks from stream-json (flat).

| Metric | Applies to | Definition |
|---|---|---|
| `tool_calls_total` | Both | Count across run |
| `tool_calls_by_tool` | Both | Histogram by tool name |
| `tool_calls_by_agent` | Tree only | Count per agent |
| **`tool_call_redundancy_intra`** | Both | (tool, target) pairs repeated within a single agent / session |
| **`tool_call_redundancy_sibling`** | Tree (0 for flat by construction) | (tool, target) pairs in ≥2 sibling agents |
| **`tool_call_redundancy_hierarchy`** | Tree (0 for flat by construction) | (tool, target) pairs appearing in child AFTER any ancestor |
| `redundancy_rate_total` | Both | Sum of three above / `tool_calls_total` |
| `security_tool_adoption` | Both | 1 if ≥1 call contains `valgrind` or `klee`; else 0 |
| `security_tool_invocations` | Both | Count |

**Target definition for redundancy matching:**

- `Read`: file path (ignore line range)
- `Bash`: command first word + first 2 arg tokens
- `Grep` / `Glob`: pattern
- `Edit`: file path

### 5.4 Duration metrics

| Metric | Applies to | Definition |
|---|---|---|
| **`wall_clock_seconds`** | Both | `run_completed - run_started` |
| `time_per_role` | Tree only | Sum of `time_in_status=EXECUTING` per role |
| `tool_call_duration_p50` / `p95` | Both | Per-call duration percentiles |
| `llm_calls_total` | Both | Count of `tokens_consumed` events (tree) or round-trips (flat) |

### 5.5 Structural metrics

| Metric | Applies to | Definition |
|---|---|---|
| `agents_total` | Tree (=1 flat by construction) | Distinct `agent_id` count |
| `max_depth_reached` | Tree (=0 flat by construction) | Longest ancestry chain |
| `agents_per_role` | Tree only | Histogram |
| `subtasks_per_decomposition` | Tree only | Mean per `subtasks_defined` event |
| `redecomposition_count` | Tree only | Count |
| `retry_count` | Tree only | Count |
| `iterations_per_session` | Both | Count of LLM round-trips |

### 5.6 Failure modes (unified taxonomy)

Single bin per run; mutually exclusive; `end_to_end_success` if none:

- `infeasibility_declared` (tree-specific)
- `verification_failed` (tree-specific)
- `retry_exhausted` (tree-specific)
- `llm_error` (both)
- `container_error` (both)
- `tool_misuse` (both)
- `budget_exceeded` (both)
- `timeout` (both)
- `produced_artifact_but_failed_mechanical` (both)

### 5.7 Context-Novelty Ratio (CNR)

For any prompt `P`, let `History(P)` be the concatenation of all prior prompt/context content the same thread has seen.

`CNR(P) = |tokens(P) \ tokens(History(P))| / |tokens(P)|`

Range [0, 1]; higher = more novel content per prompt.

- **Tree:** per agent's LLM call, `History` = concatenation of all `prompt_sent.prompt_text` in ancestry chain.
- **Flat:** per LLM round-trip, `History` = concatenation of all earlier round-trip prompts + tool-results.

**Tokenizer:** `anthropic/claude-3-tokenizer` (Anthropic SDK). Claude Code CLI's system prompt treated as "prior context at round 0" for both systems.

**Primary metric:** **`cnr_mean`** — per-prompt mean across the run.

### 5.8 LLM-judge context quality rubric

**Judge model:** latest OpenAI SOTA at run time (documented in `meta.json`). Temperature=0. Single judge run; reliability sub-study on 10% subset with 5× resampling for variance.

**Template:** `experiments/rubrics/context_quality.j2`.

**Dimensions (1-5 each):**

- **Relevance** — 1 = mostly unrelated; 5 = every section supports subtask
- **Sufficiency** — 1 = missing critical info; 5 = all needed info present
- **Non-redundancy** — 1 = >50% duplicates earlier context; 5 = no duplication
- **Specificity** — 1 = vague; 5 = concrete paths/lines/commands

**Primary metric:** **`cq_mean`** — mean across 4 dimensions × sampled prompts.

**Sampling:** if run has > 50 prompts, sample 50 uniformly; else all.

### 5.9 Manual code review rubric (Track 2)

Reviewer = author (not blinded — documented limitation). Anchored with SEC-bench gold `patch` field as 5-anchor + 3 synthetic bad patches as 1-anchors. Two rating passes, ≥30-min cool-off, random order per CVE.

**Patch dimensions** (1-5 each): `patch_correctness`, `patch_minimality`, `patch_safety`, `patch_readability`.
**PoC dimensions** (1-5 each): `poc_precision`, `poc_determinism`, `poc_minimality`.
**Build dimensions** (1-5 each): `build_correctness`, `build_reproducibility`.
**Binary criterion:** `effective_tool_use` — did the agent use Valgrind / KLEE output to inform its solution?

**Reliability:** Spearman ρ between rating passes reported.

### 5.10 Anti-cheat audit

Per-run binary flag + enumeration of violations. Runs flagged but not excluded. Results reported as "all runs" and "excluding violations."

### 5.11 Primary metric summary

| ID | Metric | Rationale |
|---|---|---|
| **P1** | `end_to_end_pass` | Ultimate success |
| **P2** | `cost_usd_total` (on jointly-successful runs) | Efficiency conditional on success |
| **P3** | `tool_call_redundancy_sibling + _hierarchy` | Duplication hypothesis (tree-only) |
| **P4** | `cnr_mean` | Context quality headline |
| **P5** | `cq_mean` (LLM-judge) | Context quality corroboration |

---

## 6. Analysis Plan (Pillar B consumer contract; pre-registered)

All analyses and stopping rules frozen before any run. Any comparison added after seeing data = labeled "exploratory."

### 6.1 Sample characteristics

n = 10 CVEs; 6 cells; 1 run per (CVE, cell) = 60 runs; +3 anchor replicates on A1 = 63.
**Framing:** descriptive + effect sizes, not p-values. n=10 is underpowered for formal testing.

### 6.2 Primary comparison — headline

**P1-a: `end_to_end_pass` — A1 vs. B2**

- Per-CVE paired binary outcomes (10 pairs)
- McNemar's exact test for paired binary; Cliff's delta as effect size
- 2×2 contingency + per-CVE outcome table
- **Success criterion:** B2 wins ≥3 of 4 non-tied pairs AND effect-direction holds across all 3 difficulty strata

**P1-b: `cost_usd_total` among jointly-successful runs**

- Per-CVE paired continuous outcome (subset where both A1 and B2 pass)
- Wilcoxon signed-rank; median diff + 95% bootstrap CI (10k resamples)
- No directional prediction

### 6.3 Secondary comparisons (pre-registered)

All paired by CVE; McNemar for binary, Wilcoxon for continuous, bootstrap CIs.

| ID | Comparison | Metric | Purpose |
|---|---|---|---|
| S1 | B1 vs. B2 | end_to_end_pass, cost | Briefing effect in tree |
| S2 | A1 vs. A3 | end_to_end_pass, cost | Briefing effect in flat |
| S3 | A1 vs. A2 | end_to_end_pass, cost | Effect of Claude's Task-subagents |
| S4 | A2 vs. B1 | end_to_end_pass, cost | Pure orchestration effect (stripped of both extras) |
| S5 | A3 vs. B2 | end_to_end_pass, cost | Apples-to-apples at matched domain-knowledge |

### 6.4 Context-duplication analysis (within-tree)

- `tool_call_redundancy_sibling + _hierarchy` per run in B1, B2
- `cnr_mean` per run, stratified by role depth (BOSS → MANAGER → WORKER)
- Comparison vs. flat: `tool_call_redundancy_intra` only

### 6.5 Context-quality analysis

- Per-dimension means (relevance / sufficiency / non-redundancy / specificity) across cells
- Descriptive table, no formal test

### 6.6 Manual rubric analysis

- Descriptive: mean ± std per dimension per cell
- Reliability: Spearman ρ between rating passes; if ρ < 0.7, rubric claims flagged as exploratory

### 6.7 Exploratory analyses

- Cost breakdown: stacked bar per cell by role / operation (tree only for role split)
- Tool-use distribution: treemap
- Trajectory visualization: Sankey (tree) / conversation-history-length (flat) for 1-2 CVEs

### 6.8 Missing-data handling

- **Budget / timeout** runs → `end_to_end_pass=0`; partial artifacts still rated
- **Container / LLM errors** excluded from headline; reported separately in "run status" table. If >10% of runs error, re-run the cell.
- **Cheating violations** NOT excluded; results shown "all" and "excluding."

### 6.9 Anchor-CVE variance estimate

3 replicates of A1 on CVE #5; compute std of {end_to_end_pass, cost, cnr_mean}. Reported as methodology footnote.

### 6.10 Exp-2 trigger thresholds (pre-registered)

Exp-2 (manager tool-calling) is greenlit iff **both**:

- `tool_call_redundancy_sibling + _hierarchy` ≥ **20%** of total tool calls in tree cells, averaged
- `cnr_mean` at MANAGER depth ≤ **0.4**

If either threshold not met, Exp-2 is postponed.

### 6.11 Hypotheses (formal)

**H1 (primary):** Given identical budget, model, and execution environment, the tree-orchestrated system (B2) achieves higher end-to-end mechanical success rate on stratified SEC-bench instances than the flat Claude Code CLI baseline (A1).

- Supported iff P1-a passes §6.2 success criterion.
- Falsified if A1 matches or beats B2 overall AND the advantage doesn't concentrate in any difficulty stratum.

**H2 (secondary):** The tree-orchestrated system exhibits measurable context duplication across sibling/hierarchy boundaries, motivating follow-up work on manager-level tool-calling.

- Supported iff §6.10 thresholds met.

**H3 (secondary):** Context quality (LLM-judge `cq_mean`) is higher for the tree's prompts than for the flat CLI's accumulated conversation context.

- Supported iff tree `cq_mean` > flat `cq_mean` on ≥7 of 10 CVEs.

---

## 7. Dataset Artifact Specification

### 7.1 Directory layout

```
dataset/
├── dataset_version.yaml
├── pre_registration.yaml
├── locked_instances.yaml
├── domain_briefing.md
├── role_prompts/
│   ├── pre_opt/
│   └── post_opt/
├── config/
│   ├── config.exp-secbench-tree-B1.yaml
│   ├── config.exp-secbench-tree-B2.yaml
│   └── ...
├── runs/
│   └── <cve_id>/<cell_id>/<replicate_num>/
│       ├── meta.json
│       ├── events.jsonl
│       ├── artifacts/
│       │   ├── repro.sh
│       │   ├── model_patch.diff
│       │   ├── build.sh
│       │   └── base_commit_hash
│       ├── mechanical.json
│       ├── audit.json
│       └── stdout_stderr.log
└── INDEX.jsonl
```

### 7.2 Per-run `meta.json` schema

```yaml
run_id: "<cve>-<cell>-<replicate>"
cve_id: "openjpeg.cve-2016-7445"
cell: "A1"
replicate: 0
system: "flat_cli" | "tree"
domain_briefing_enabled: bool
subagent_enabled: bool | null
prompt_strategy: "secbench" | "null" | "cli_default"
docker_image: "secb-tools:<project>.<cve>"
budget_usd_cap: 3.0
wallclock_sec_cap: 600
models:
  boss: "claude-opus-4-7" | null
  manager: "claude-opus-4-7" | null
  worker: "claude-sonnet-4-6"
  judge: "gpt-5"
started_at: ISO8601
ended_at: ISO8601
wallclock_seconds: float
termination_reason: "completed" | "budget_cap" | "wallclock_cap" | "llm_error" | "container_error" | "tree_timeout"
code_sha:
  arise_sec_lion: "<git-sha>"
  flat_cli_harness: "<git-sha>"
  claude_code_cli_version: "<npm version>"
  claude_sdk_version: "<pypi version>"
env:
  claude_api: "anthropic-<version>"
  date: ISO8601
dataset_schema_version: "1.0"
notes: ""
```

### 7.3 `events.jsonl` normalized schema

Each line = one event. Time-ordered. Same schema for both systems.

**Core fields on every event:**

```yaml
event_id: "<uuid>"
run_id: "<same as meta>"
occurred_at: ISO8601
event_type: "run_started" | "run_completed" | "agent_created" | "prompt_sent" | "llm_response" |
            "tokens_consumed" | "tool_use" | "tool_result" | "worker_cost_recorded" |
            "status_changed" | "subtasks_defined" | "child_spawned" | "work_completed" |
            "work_failed" | "retry_scheduled" | "verification_failed" | "redecomposition_triggered"
source: "tree" | "flat_cli"
agent_id: "<uuid>" | null
parent_agent_id: "<uuid>" | null
role: "BOSS" | "MANAGER" | "WORKER" | "PENDING" | "JUDGE" | "CONDENSER" | "FLAT"
depth: int
sequence_number: int
```

**Event-type-specific payloads:**

```yaml
# prompt_sent
payload:
  prompt_text: string        # full, not truncated
  prompt_type: "assess" | "decompose" | "worker_execution" | "verification" | "context_condense"
  model: "claude-opus-4-7" | "claude-sonnet-4-6"

# tokens_consumed
payload:
  input_tokens: int
  output_tokens: int
  cache_read_input_tokens: int
  cache_creation_input_tokens: int
  thinking_tokens: int | null
  cost_usd: float
  operation: "assess" | "decompose" | "worker_execution" | "verification" | "context_condense"

# tool_use
payload:
  call_id: string
  tool_name: "Bash" | "Read" | "Edit" | "Write" | "Glob" | "Grep" | "WebFetch" | "Task" | "TodoWrite"
  tool_input: dict           # full structured args
  duration_ms: int | null

# tool_result
payload:
  call_id: string
  result_text: string        # up to 10KB
  was_truncated: bool
  result_bytes: int
  is_error: bool
```

**Design principle:** richest-superset schema. Events unused by flat are absent from flat runs; no forced fabrication.

### 7.4 `INDEX.jsonl` schema

One line per run:

```yaml
run_id: "..."
cve_id: "..."
cell: "A1"
replicate: 0
path: "runs/openjpeg.cve-2016-7445/A1/0/"
termination_reason: "..."
wallclock_seconds: float
total_cost_usd: float
event_count: int
artifacts_produced: ["repro.sh", "model_patch.diff"]
mechanical_pass:
  builder: bool
  exploiter: bool
  fixer: bool
  end_to_end: bool
audit_violations: int
```

### 7.5 Versioning & integrity

- Semver in `dataset_version.yaml`
- Tarball: `dataset-v1.0-<date>.tar.zst`, SHA-256 recorded
- Immutability: append-only once tarballed; re-runs produce new version with changelog
- Privacy: sanitization pass at tarball time (no API keys, no host-specific paths)

### 7.6 Exit criterion

Pillar A complete when `dataset-v1.0-<date>.tar.zst` exists and all 63 runs are either `completed` or terminally-failed with `termination_reason` recorded.

---

## 8. Limitations, Threats to Validity, Exp-2 Rationale

### 8.1 Pre-registered limitations

| # | Limitation | Severity | Mitigation |
|---|---|---|---|
| 1 | n=10 underpowered for formal hypothesis testing | High | Descriptive + effect sizes, not p-values |
| 2 | Single reviewer for manual rubric | Medium | Two rating passes + Spearman ρ |
| 3 | Reviewer is the system's author (not blinded) | Medium | Explicit in report; rubric-based claims secondary |
| 4 | Role prompts optimized by GPT-5 but used with Claude | Medium | Documented; pre-opt baseline archived |
| 5 | Judge is GPT-5; systems use Claude | Low | Third-family judge avoids self-grading bias |
| 6 | Difficulty labels from SEC-bench SECVERIFIER | Medium | Stratum reported descriptively |
| 7 | Custom instrumentation could contain bugs | Medium | Smoke tests before lock; pre-registration SHA pins code |
| 8 | Single-family engine (both arms Claude) | Medium | Documented; future-work candidate |
| 9 | `--network=none` prevents WebFetch but not LLM-memorization of training-data patches | Medium | Post-hoc audit flags gold-identical patches |
| 10 | Flat CLI's internal TodoWrite state not structured-captured | Low | Reconstructed best-effort from stream-json |

### 8.2 Threats to validity

- **Internal:** model, budget, container, tool parity held constant. Remaining: stochasticity (temp=0 + anchor replicates), API-weather (randomized cell order).
- **External:** SEC-bench is one benchmark; findings may not generalize to Python / web / non-security tasks.
- **Construct:** CNR and redundancy taxonomy are operationalized choices, not gold-standard definitions.

### 8.3 Exp-2 rationale (pre-drafted)

**If §6.10 thresholds met:** the report's Discussion will argue:

> "Our tree-orchestrated system produces X% higher end-to-end success than flat Claude Code CLI. However, Y% of worker tool calls are redundant with parent/sibling context. This suggests: MANAGER agents cannot tool-call during decomposition, so domain-relevant context (file structures, symbol tables, repository layout) is rediscovered at each leaf. Adding manager-level tool-calling to populate shared context pre-decomposition should reduce this duplication. We propose Exp-2: add tool-calling to MANAGER role, re-run the same 10 CVEs, measure cost reduction."

**If thresholds not met:** report pivots to whatever mechanism the data actually shows.

### 8.4 Future work

- Feature-level ablation (judge, retry-escalation, SharedStore)
- Engine replication (SWE-agent, OpenHands)
- Non-security benchmark expansion (SWE-bench)
- Deeper trees for hardest CVEs
- Hybrid orchestration (tree decomposition + flat-with-subagents at leaves)

### 8.5 Report outline (Pillar B deliverable)

1. Abstract
2. Introduction
3. Related work
4. Method
5. Results
6. Discussion (including Exp-2 motivation)
7. Limitations
8. Future work
9. Reproducibility (dataset artifact, code SHAs)

---

## 9. Open Items / Decisions Needed Before Lock

- Verify SEC-bench HuggingFace labels for 10 tentative CVEs before locking (§3.3)
- Decide exact GPT judge model at run time (§5.8)
- Confirm `claude-opus-4-7` availability / pricing for BOSS/MANAGER (§1.2)
- Confirm extended-thinking availability on Sonnet 4.6 worker path (§1.1, §1.2)

---

*End of design document. Pillar A implementation plan to be produced by the `writing-plans` skill in a follow-up step.*
