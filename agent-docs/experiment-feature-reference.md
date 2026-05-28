# Experiment Feature — Technical Reference (`feature/experiments-rearchitecture`)

Authoritative reference for the SEC-bench experiment matrix on this branch. Built from 5 independent investigations (3 Opus, 2 Codex) plus 2 validators (Opus + Codex) on the one material disagreement. Every load-bearing claim is anchored by an inline `[N]` marker; the citations list is at the bottom of the doc.

The system uses these vocabulary mappings:

| User term                      | Code term                              | Meaning                                                          |
|--------------------------------|----------------------------------------|------------------------------------------------------------------|
| Experiment Setup (A, B, C…)    | **family** (`groups.yaml`)             | Letter prefix — orchestration mode + worker engine archetype     |
| Experiment branch (1, 2, …)    | **variant** (digit suffix, no name in code) | Numeric suffix — treatment toggle inside the same family   |
| Single experiment configuration | **cell** (`A1`, `B2`, …)               | Full family+variant pair; one row of the dispatch matrix         |

`run_matrix` enumerates the cartesian product of (**cell × task × replicate**) and dispatches each job through one universal runner. There are 6 cells in the active study, 10 CVE tasks, 1 replicate → 60 jobs in a full sweep [1, 2].

---

## 1. Dispatch flow

```
manifest.yaml ─┐
dataset.yaml ──┼─► run_matrix.py ─► runner "arise" ─► harness.run_arise ─► python main.py run …
configs/*.yaml ┘                                                                │
                                                                                ▼
                                                                  bootstrap.composition
                                                                                │
                                                                                ▼
                                                                  AgentExecutionService
                                                ┌──────────────────────────────┴───────────────────┐
                                                │ orchestration.mode = hierarchical                │ flat
                                                ▼                                                  ▼
                                BOSS → 4 phase managers → workers                         single WORKER
                                (per-worker secb-tools container, run-scoped workspace)   (one secb-tools container)
```

Every cell — flat or hierarchical, Claude or Qwen — passes through the same `python main.py -c <cell.yaml> run <slug> --domain security --domain-context-file <cve.json>` invocation [3, 4, 5]. Family/variant divergence is achieved purely by YAML overlay on `config/config.yaml`; no per-cell code paths exist [4, 6]. The matrix runner enforces this uniformity by validating the manifest (group registry, runner registry, required keys, prefix/group consistency) before any job is dispatched [7].

Per-run artifacts land in `runs/<run_id>/`; per-study aggregates are then assembled into `experiments/<study>/{reports,artifacts}/` by `collect.py` (see §8).

**Citations.** [1] `experiments/2026-04-23-initial-secbench/manifest.yaml:1-19` · [2] `experiments/2026-04-23-initial-secbench/dataset.yaml:1-29` · [3] `experiments/shared/runners/arise.py:26-58` · [4] `experiments/shared/harness.py:225-336` · [5] `bootstrap/bootstrap.py:66-241` · [6] `experiments/shared/scripts/run_matrix.py:147-321` · [7] `experiments/shared/scripts/validate_manifest.py:41-99`

---

## 2. Study design — families, cells, variants

`groups.yaml` declares three families (letters) [8]:

| Family | Label              | Orchestration | Worker engine                | Variant axis     |
|--------|--------------------|---------------|------------------------------|------------------|
| **A**  | Claude Code CLI    | `flat`        | Claude CLI (in-container)    | Sub-agent on/off |
| **B**  | Our System         | `hierarchical`| Claude Code via SDK adapter  | LLM judge on/off |
| **C**  | Our System + Qwen  | `hierarchical`| OpenHands + Qwen 3.5         | LLM judge on/off |

The six cells of the active study `2026-04-23-initial-secbench` differ only in YAML overlay [9–14]:

| Cell | Family | Variant   | Hypothesis-defining knob                                                                                      |
|------|--------|-----------|---------------------------------------------------------------------------------------------------------------|
| A1   | A      | sub-agent | `orchestration.mode: flat`, `worker.tool: claude_code`, `worker.disallowed_tools: []` (Task agent allowed) [9] |
| A2   | A      | no Task   | identical to A1 except `worker.disallowed_tools: ["Task"]` [10]                                               |
| B1   | B      | no judge  | `orchestration.mode: hierarchical`, `skip_judge: true`, `worker.tool: claude_code` [11]                       |
| B2   | B      | judge     | identical to B1 except `skip_judge: false` [12]                                                                |
| C1   | C      | no judge  | hierarchical, `worker.tool: openhands`, `worker.model: ollama_chat/qwen3.5:397b-cloud`, `skip_judge: true` [13]|
| C2   | C      | judge     | identical to C1 except `skip_judge: false` [14]                                                                |

Hypotheses by axis (per `manifest.yaml` and per-cell comments [1, 9–14]):

- **A1 vs A2** — does Claude Code's *own* `Task` sub-agent affordance add value on top of the flat CLI baseline?
- **A vs B** — does our hierarchical BOSS→MANAGER→WORKER decomposition outperform a flat Claude CLI agent on the same model?
- **B vs C** — does the worker model (Claude Sonnet 4.6 vs Qwen 3.5 via OpenHands) dominate the architecture choice?
- **`*1` vs `*2`** — does adding the LLM Judge (`skip_judge: false`) help within each architecture/model pair?

**Citations.** [8] `experiments/shared/groups.yaml:1-6` · [9] `experiments/2026-04-23-initial-secbench/configs/A1-claude-code-subagent.yaml:1-30` · [10] `…/A2-claude-code-nosubagent.yaml:1-29` · [11] `…/B1-ours-claude-noverifier.yaml:1-23` · [12] `…/B2-ours-claude-verifier.yaml:1-22` · [13] `…/C1-qwen-noverifier.yaml:1-22` · [14] `…/C2-qwen-verifier.yaml:1-21`

---

## 3. Hierarchy & role composition

### 3.1 Tree shape

In hierarchical cells the BOSS prompt mandates **exactly four** top-level subtasks, with sequential dependencies and the first three marked `complex` (decompose), the last `simple` (execute) [15]:

```
BOSS
├── [Builder]    (complex → manager phase)   ┐
├── [Exploiter]  (complex → manager phase)   │  Builder→Exploiter→Fixer→Reporter
├── [Fixer]      (complex → manager phase)   │  is the prompt-mandated order [15]
└── [Reporter]   (simple  → single worker)   ┘
```

> **Heads-up.** The original "invariant" wording in this branch says "the second layer = Builder, Exploiter, Fixer." This is **incomplete**: the BOSS always spawns four children. Reporter is the fourth. Two of the five source agents flagged this; three did not push back. See §11 for the audit row.

### 3.2 Phase decomposition — `assess.j2` is the contract

`assess.j2` is the **source of truth** for how a `[Builder]/[Exploiter]/[Fixer]` PENDING node decomposes into leaf workers. It mandates exact role rosters, sequential ordering, and that every leaf defaults to `EXECUTE` (no further decomposition) [16]:

| Phase node    | Leaf workers (in order)                                                                                            | Final worker      |
|---------------|--------------------------------------------------------------------------------------------------------------------|-------------------|
| `[Builder]`   | `Build-Setup` → `Build-Compiler` → `Build-Verifier`                                                                | `Build-Verifier` [16] |
| `[Exploiter]` | `PoC-Researcher` → `Data-Flow-Analyst` → `PoC-Tester` → `Forward-Instrumentator` → `Repro-Creator` → `Exploit-Validator` | `Exploit-Validator` [16] |
| `[Fixer]`     | `Root-Cause-Analyst` → `Candidate-Reviewer` → `Regression-Tester` → `Patch-Creator` → `Patch-Validator` → `Fix-Aggregator` | `Fix-Aggregator` [16] |

Selected role intents (representative — `assess.j2:9-19, 27-58` carries the full table) [16]:

- **PoC-Researcher** — survey upstream advisories, OSV-DB, vendor write-ups, and the bug report to characterise the trigger surface.
- **Data-Flow-Analyst** — trace tainted inputs from entry to sink, identify the corruption primitive.
- **PoC-Tester** — empirically execute candidate triggers; declare which actually crash under the configured sanitizer.
- **Forward-Instrumentator** — add lightweight logging (`fprintf`, sanitizer hooks) so downstream Repro-Creator can deterministically observe the bug.
- **Repro-Creator** — produce `repro.sh` as the canonical deterministic trigger.
- **Exploit-Validator** — final empirical re-run, holistic sanity check across upstream worker outputs.

### 3.3 Worker prompt composition

Each WORKER prompt is composed in this order (rendered once per worker by the security prompt strategy) [17]:

1. Common preamble: `system.j2` → role descriptor → operation descriptor.
2. Phase-specific worker body: `prompts/domains/secbench/worker/<phase>.j2` selected by bracket-prefix detection on the worker's task description (`worker/builder.j2`, `worker/exploiter.j2`, `worker/fixer.j2`, `worker/reporter.j2`) [17].
3. The shared SEC-bench input block: `inputs/user.j2` + `inputs/cve.j2` + `inputs/control.j2` — identical bytes for Cell A flat-mode persona and every hierarchical BOSS [18, 19].

### 3.4 Removed: per-phase **manager** templates

`prompts/domains/secbench/manager/{builder,exploiter,fixer}.j2` were removed in commit `6264ece` ("remove obsolete manager/{role}.j2 templates and their dispatch"). They were dead on the assess→spawn path — `assess.j2` is the live decomposition contract. Earlier docs claim the concatenated body was reused as the Cell A flat-mode persona, but the corresponding test (`test_single_agent_persona_locks_to_three_managers`) had also been removed in a prior commit; flat-mode persona actually comes from `flat_pipeline.j2` (see `plugins/security/prompt_strategy.py::extend_flat_prompt`).

### 3.5 Known prompt inconsistency — `boss.j2` vs `assess.j2`

`boss.j2:59-60` instructs the BOSS to decompose Exploiter / Fixer into **"4-5 workers"**, while `assess.j2:12,19` mandates **exactly 6 workers** in fixed order [15, 16]. This is a real contradiction; `assess.j2` is documented as the source of truth (per the "EXACT role names" wording [16]). Recommendation: update `boss.j2:59-60` to read "exactly 6". One of the five investigations caught this; it is independently re-verified.

**Citations.** [15] `prompts/domains/secbench/boss.j2:16-65` · [16] `prompts/domains/secbench/assess.j2:1-82` · [17] `plugins/security/prompt_strategy.py:83-239` · [18] `prompts/inputs/{user,cve,control}.j2` · [19] `plugins/security/tests/test_prompt_unification_invariants.py:1-300`

---

## 4. The LLM Judge

The Judge is a **Stage-4 LLM-evaluator** inside `VerificationPipeline`, not a separate agent in the tree. It fires after **every** WORKER's `COMPLETED` status (not only the phase-final ones) [22, 23].

### 4.1 Pipeline (per worker)

```
Stage 1: structural      — output non-empty?                  [verifies result != ""]   [22]
Stage 2: deterministic   — placeholder, always passes                                    [22]
Stage 3: execution       — placeholder, always passes                                    [22]
Stage 4: LLM Judge       — fires iff success_criteria AND not skip_judge                 [22, 24]
```

Stages 2 and 3 are inert today; the pipeline is effectively `structural → judge`.

### 4.2 Skip conditions

The Judge stage is skipped when either:

- `agent.success_criteria` is empty (the parent did not supply a per-worker rubric), or
- `orchestration.skip_judge: true` in the cell YAML — propagated via composition into `AgentOrchestrator._skip_judge` [22, 24].

So **B1 and C1 bypass the Judge entirely**. **A1 and A2 are flat-mode and never invoke the hierarchical pipeline.** Only **B2 and C2** actually run the Judge.

### 4.3 Threshold, parsing, retry budget

The Judge prompt requests JSON `{"score": int 0..100, "feedback": str}` [25]. Parsing falls through three layers: deterministic `json.raw_decode` → optional `FormatRepairerPort` LLM-based fix (default model `qwen3-coder:480b-cloud`) → regex fallback that extracts `"score": <int>` [26].

**Pass threshold: `score >= 60`** (constant `_JUDGE_PASS_THRESHOLD = 60`) [27]:

- **Pass** (`score ≥ 60`): emit `VerificationPassed(feedback, score)`; agent remains `COMPLETED`.
- **Fail** (`score < 60` or all parses exhausted): emit `VerificationFailed(failed_stage="judge", feedback, score)`. The execution service then attempts up to **2 verification retries** (`_MAX_VERIFICATION_RETRIES = 2` [28]); each retry re-prompts the worker with the previous judge feedback injected verbatim into the prompt [29].

Verification retries are a **separate budget** from the orchestration retry policy (`max_retries: 3`). After 2 verification retries fail, control falls through to `_retry_policy.maybe_schedule_retry`; ultimately failure propagates to the parent [30].

### 4.4 What the Judge sees

The Judge LLM is fed: `task`, `agent.success_criteria`, a structured report context (`artifacts`, `decisions`, parent's `<context-update>` blocks), and a truncated `agent.result` (ANSI-stripped, 30 KiB head/tail) [25, 31]. **`agent.result` is not raw stdout** — it is the worker adapter's final result string: `ClaudeAgentSDKAdapter` returns the SDK's terminal `ResultMessage.result`; `OpenHandsAdapter` returns a synthesized "compact command log" of `$ command (exit N)` summaries [32].

**Citations.** [22] `core/application/services/orchestration/verification_pipeline.py:84-187` · [23] `core/application/agent_orchestrator.py:573-575` · [24] `core/application/services/orchestration/verification_pipeline.py:148-152` · [25] `core/application/services/orchestration/verification_pipeline.py:225-340` · [26] `core/application/services/orchestration/verification_pipeline.py:317-374` · [27] `core/application/services/orchestration/verification_pipeline.py:87` · [28] `core/application/execution_service.py:903` · [29] `core/application/agent_orchestrator.py:539-546` · [30] `core/application/execution_service.py:892-932` · [31] `core/application/services/orchestration/verification_pipeline.py:135, 274` · [32] `infrastructure/adapters/worker/{claude_sdk_adapter.py:288, openhands_adapter.py:518-546}`

---

## 5. Docker runtime & "Security information"

### 5.1 Image resolution chain

```
CVEInstance.docker_image                        plugins/security/cve_instance.py:49-52
   = "hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch"   (default)
   ↓
resolve_secbench_image(security_tools_enabled=True)       plugins/security/image_resolver.py:23-42
   ↓
"secb-tools:<project>.<cve>-patch"                        (built by deployment/build-secbench-tools.sh)
```

`docker_image_override` is the only escape hatch [33]. The `:patch` tag is **the only tag wired in the code path** — `grep` confirms no `:recent` or other alternative is referenced anywhere [33].

The local `secb-tools:*-patch` image is built by `deployment/secbench-tools.Dockerfile` on top of the upstream `:patch` image, adding Valgrind, debugging tools, Claude Code CLI, and the MCP server. KLEE is best-effort: the Dockerfile attempts to install it, but the layer is allowed to fail [34, 35].

### 5.2 What "security information" reaches the worker

The CVE JSON parses into `CVEInstance` (frozen Pydantic model) [33]. The full schema:

| Field                  | Required | Renderable to prompts | Notes                                          |
|------------------------|----------|------------------------|------------------------------------------------|
| `instance_id`          | ✓        | ✓                      | e.g. `gpac.cve-2023-5586`                       |
| `repo`                 | ✓        | ✓                      | e.g. `gpac/gpac`                                |
| `project_name`         | ✓        | ✓                      |                                                |
| `lang`                 | ✓        | ✓                      |                                                |
| `work_dir`             | ✓        | ✓                      | e.g. `/src/gpac`                                |
| `sanitizer`            | ✓        | ✓                      | e.g. `AddressSanitizer`                         |
| `bug_description`      | ✓        | ✓                      | free-form                                      |
| `base_commit`          | ✓        | ✓                      | vulnerable commit hash                         |
| `bug_report`           | optional | ✓ (truncated to 8 KiB) |                                                 |
| `sanitizer_report`     | optional | ✓                      |                                                |
| `build_sh`             | optional | ✗ (build context only) | written as `secb build` body                    |
| `secb_sh`              | optional | ✗ (build context only) | written as `/usr/local/bin/secb`                |
| `dockerfile`           | optional | ✗                      | only used when image is rebuilt locally         |
| `patch`                | optional | **FORBIDDEN**          | gold-fix diff — stripped from every prompt [36] |
| `candidate_fixes`      | optional | **FORBIDDEN**          | stripped from every prompt [36]                 |
| `exit_code`            | optional | ✗                      | offline scoring                                 |
| `docker_image_override`| optional | ✗                      | infra-only                                      |

The prompt-render-safe view is produced by `CVEInstance.to_template_context()`, which is the **single, load-bearing gate** for what reaches Jinja templates [36, 37]. A second layer of defence (`_CVE_DISPLAY_FIELDS` allowlist) limits the renderable set further to the columns marked "✓" above [37].

### 5.3 Container lifecycle

**One container per WORKER dispatch**, not per run. `role_dispatch.py:110-131` wraps each WORKER execution in `try/finally`; the security plugin's `prepare_worker_execution` calls `start_session` (which `docker run -d`s a fresh container), and `cleanup_worker_execution` calls `stop_session` (`docker rm -f`) [38, 39, 40]. The container name is agent-keyed: `"{prefix}-{root_id.hex[:8]}-{agent_id.hex[:8]}"` [41].

**Run-scoped workspace bind-mounts persist across worker containers.** The host paths `runs/<run_id>/src/`, `runs/<run_id>/testcase/`, and the run root are bind-mounted into each successive worker container at `/src`, `/testcase`, and `/arise-run` respectively [42, 43]. So source edits and deliverables persist across worker boundaries; the container is recycled, the workspace is not.

The first time the workspace is prepared for a run, the runtime seeds host `src/` and `testcase/` by `docker cp /src/.` and `docker cp /testcase/.` from a short-lived seed container against the resolved image [42, 44]. The contents of the image's `/src` and `/testcase` therefore reach the host workspace verbatim — making upstream image cleanliness load-bearing for the golden-patch invariant (resolved SAFE; see §11).

> **Latent concurrency invariant.** `plugin._sessions` is keyed by `root_id`, not `agent_id`. With `max_concurrent_workers > 1` two sibling workers in the same run would race; today's default keeps it at 1 [45]. Flagged so it isn't silently broken later.

**Citations.** [33] `plugins/security/cve_instance.py:18-52` · [34] `deployment/secbench-tools.Dockerfile:1-73` · [35] `deployment/build-secbench-tools.sh:36-92` · [36] `plugins/security/cve_instance.py:15, 96-108` · [37] `plugins/security/prompt_strategy.py:17-28, 83-239` · [38] `core/application/services/lifecycle/role_dispatch.py:110-131` · [39] `plugins/security/plugin.py:145-204` · [40] `plugins/security/docker_runtime.py:118-137` · [41] `plugins/security/docker_runtime.py:83-84` · [42] `plugins/security/docker_runtime.py:46-114` · [43] `plugins/security/docker_runtime.py:100-110` · [44] `plugins/security/docker_runtime.py:148-172` · [45] `plugins/security/plugin.py:182-204`

---

## 6. Tool surface

### 6.1 MCP server — `valgrind_run` and `klee_run` always exposed

The security plugin wires an MCP server (`plugins/security/mcp/security_tools_server.py`) once per worker dispatch [46]. Two tools are **always registered at module import**, regardless of phase, worker role, or the `security.tools` config: `valgrind_run` and `klee_run` [47].

The `security.tools` config (default `[valgrind]`) only filters the **prompt advertising** in `tools.j2` — i.e. whether the worker's prompt describes how to use these tools. It does **NOT** gate MCP availability; the server still exposes both [47, 48].

KLEE has an additional runtime concern: the `klee_run` MCP handler performs a lazy `apt-get install -y klee` inside the worker container on first invocation; on failure it returns a structured `klee_install_failed` error [49]. So KLEE may light up at tool-call time even if the static config says only Valgrind.

The MCP server is wired into both engine adapters [50]:

- `ClaudeAgentSDKAdapter` — receives `task_context['mcp_servers']` and registers them with the SDK session.
- `OpenHandsAdapter` — receives the same context and maps to OpenHands' MCP integration.

### 6.2 Worker tool allowlist — uniform within a cell

`worker.allowed_tools` / `worker.disallowed_tools` are set once per cell and apply to every worker [9–14]. There is no per-bracketed-role allowlist; Builder, Exploiter, Fixer (and their sub-roles) all see the same tool surface inside a cell.

### 6.3 The "identical tool surface" invariant — partially violated by design

| Layer                          | Identical across all 6 cells? | Where it differs                              |
|--------------------------------|-------------------------------|-----------------------------------------------|
| MCP server tools (`valgrind_run`, `klee_run`) | ✅ Yes                | —                                              |
| Run command/`secb-exec` helper | ✅ Yes                          | —                                              |
| Worker CLI allowlist           | ❌ A1 ≠ A2 by design          | `disallowed_tools: ["Task"]` is the A axis [9, 10] |
| Worker engine                  | ❌ A/B Claude vs C Qwen        | the B/C hypothesis axis [11–14]                |

The "identical surface" goal is enforced where it must be (MCP, secb commands) and intentionally relaxed where the experiment is varying it (sub-agent decomposition, model family).

**Citations.** [46] `plugins/security/plugin.py:182-220` · [47] `plugins/security/mcp/security_tools_server.py:191, 219` · [48] `config/config.yaml:151-153` · [49] `plugins/security/mcp/security_tools_server.py:40-41, 222-251` · [50] `infrastructure/adapters/worker/{claude_sdk_adapter.py:188-200, openhands_adapter.py:141-145, 290-291}`

---

## 7. Events, metrics & trajectory inspection

### 7.1 Domain event catalogue (~33 frozen Pydantic types)

The analytically relevant events [51]:

| Event                  | Purpose / payload                                                              |
|------------------------|--------------------------------------------------------------------------------|
| `PromptSent`           | Every LLM/worker prompt; `prompt_type ∈ {complexity_evaluation, task_decomposition, worker_execution}` |
| `ThoughtCaptured`      | Worker streaming output; `output_type ∈ {thinking, output, tool_use, tool_result}` |
| `TokensConsumed`       | Token + LLM-cost accounting for orchestrator/judge calls                      |
| `WorkerCostRecorded`   | Token + cost accounting emitted by worker adapters                            |
| `ComplexityEvaluated`  | The `.reasoning` field is the parent's explanation for the decompose/execute decision |
| `SubtasksDefined`      | Parent's chosen subtasks with per-subtask justification                       |
| `ChildSpawned`         | Includes the `briefing` the parent hands to the child                         |
| `RunCompleted`         | `duration_seconds` — wall time of the run loop (post-BOSS creation)           |
| `AgentCreated`         | Carries `parent_id` — used for leaf→BOSS ancestry walk (see §7.4)             |

### 7.2 Numerical metrics

Single source of truth: `experiments/shared/scripts/run_metrics.py:metrics_from_events_jsonl(path)` — a pure event-counting function with one branch per event type, usable against either Postgres or projected `events.jsonl` [52, 53].

Schema [52]:

```
event_count, prompt_sent_count, tool_call_count, tool_result_count,
thinking_event_count, thinking_chars,
worker_output_event_count, worker_cost_event_count,
tokens_prompt, tokens_completion, tokens_total, tokens_reasoning,
llm_cost_usd, worker_cost_usd, total_cost_usd,
run_duration_seconds, tool_calls_by_type
```

**Non-obvious token attribution split** (matters for cross-cell analysis) [52]:

- `tokens_{prompt,completion,total}` accumulate from **both** `TokensConsumed` (orchestrator + judge LLM) **and** `WorkerCostRecorded` (worker adapter).
- `llm_cost_usd` comes **only** from `TokensConsumed`.
- `worker_cost_usd` and `tokens_reasoning` come **only** from `WorkerCostRecorded`.
- `total_cost_usd = llm_cost_usd + worker_cost_usd`, rounded.

> **CAVEAT — read §7.5 before publishing.** The split above is correct as stated, but `tokens_total` itself is currently undercounted on Claude-SDK cells (A1, A2, B1, B2) because the worker adapter drops cache-read and cache-write tokens; OpenHands cells (C1, C2) include them. Cross-cell totals are therefore asymmetrically biased against B-family. See §7.5 issue **N-3**.

`tool_calls_by_type` parses the first line of each `ThoughtCaptured(output_type="tool_use")` payload to extract the tool name [54].

**Two distinct duration clocks** — analysts must pick one consistently [55]:

| Clock                                          | Definition                                                 |
|-----------------------------------------------|------------------------------------------------------------|
| `RunCompleted.duration_seconds`               | Wall time of the run loop, post-`create_boss_agent` [55a]  |
| `run_manifest.json::wall_started_at/ended_at` | Full setup + execution wall (includes docker prep) [55b]   |

### 7.3 Aggregation pipeline

`experiments/2026-04-23-initial-secbench/scripts/collect.py` produces the canonical CSVs [56]:

- `reports/tables/run_metrics.csv` — per-run rows
- `reports/tables/summary.csv`     — per-cell rollups
- `reports/tables/totals.csv`      — single-row study aggregate

`plot_success.py` reads `summary.csv` and emits `cells-overview.svg` (hand-rolled SVG, no matplotlib) [57]. `render_report.py` renders `report.md` from the Jinja template, fed by the CSVs + manifest + `enrollment.lock.yaml` [58]. Reports carry SHA-256 provenance: `inputs_sha256` + `output_sha256` frontmatter on the Markdown, parallel `.generated.json` for binaries (CSV, SVG). `validate_reports.py` re-hashes every input file and the body and rejects drift — so hand-edited reports fail validation [59].

### 7.4 Trajectory inspection (BOSS↔leaf)

**BOSS→descendants** (root downward) — shipping CLI [60]:

```bash
uv run python main.py prompts --agent-id <BOSS-UUID> [--format json]
```

This calls `PromptTraceService.trace(root_id)` which walks via `get_hierarchy_events_grouped(root_id)` (a recursive CTE in `postgres_event_store.py` rooted at the BOSS aggregate) [60, 61]. It returns a `HierarchyTrace` of `AgentNode`s, each with `.prompts` + parsed `<context-update>` sections.

**Leaf→BOSS** (ancestor walk) — **no shipping CLI**. To recover the ancestry chain for a leaf worker UUID, follow `AgentCreated.parent_id` events until `parent_id` is null. Recipe (offline, against `events.jsonl`) [62]:

```bash
jq -r 'select(.event_type=="AgentCreated") | "\(.aggregate_id) \(.parent_id)"' \
   runs/<run_id>/events.jsonl > pairs.txt
# Then walk parent links manually from the leaf UUID.
```

**"Why did the parent emit this prompt?"** — answered by the event quartet on the **parent**'s `aggregate_id`:

1. `PromptSent(prompt_type="complexity_evaluation")` — the parent's input to the LLM that decided "decompose vs execute".
2. `ComplexityEvaluated.reasoning` — the model's natural-language justification.
3. `PromptSent(prompt_type="task_decomposition")` — the prompt that produced the subtasks.
4. `SubtasksDefined` + `ChildSpawned.briefing` — the actual subtask list and the per-child briefing.

These five fields together reconstruct the parent's decision context. No single artifact or shipped renderer combines them today (gap; see §11).

**Worker chain-of-thought**:

```bash
# Typed view, any worker, any cell:
jq 'select(.aggregate_id=="<worker-uuid>" and has("output_type"))' \
   runs/<run_id>/events.jsonl

# Raw stream-json — A1/A2 only (flat-mode Claude CLI). B/C hierarchical adapters
# yield DomainEvents directly and do NOT write stdout_stderr.log.
cat runs/<run_id>/stdout_stderr.log
```

**Citations.** [51] `core/domain/events/events.py` · [52] `experiments/shared/scripts/run_metrics.py:44-156` · [53] `experiments/shared/scripts/run_metrics.py:28-41` · [54] `experiments/shared/scripts/run_metrics.py:210-216` · [55a] `core/domain/events/events.py:RunCompleted` · [55b] `experiments/shared/scripts/register_run.py` · [56] `experiments/2026-04-23-initial-secbench/scripts/collect.py:61-99, 237-382` · [57] `experiments/2026-04-23-initial-secbench/scripts/plot_success.py:28-86` · [58] `experiments/2026-04-23-initial-secbench/scripts/render_report.py` · [59] `experiments/shared/scripts/validate_reports.py:97-528` · [60] `core/application/services/prompt/prompt_trace_service.py:54-95` · [61] `infrastructure/adapters/postgres_event_store.py:465-518` · [62] `core/domain/events/events.py:AgentCreated`

---

## 7.5 Numeric analysis pipeline — scripts, math & known issues

> The numeric pipeline turns event-sourced run history into the three CSV tables that every paper-grade number is copied from. It is **scripts-first**: nothing in `report.md` is recomputed inline — figures are copied from CSVs produced by committed scripts and re-hashed by `validate_reports.py` on every commit [N-10].
>
> This section was written after a 5-agent + 1-synthesis audit (3 Opus + 2 Codex auditors, 1 Opus synthesizer); every load-bearing claim is re-verified against source. The verdict at audit time is: **happy-path math is correct, but four CRITICAL silent-data-loss paths must be fixed before publishing cross-cell numbers.**

### 7.5.1 The five primitives

The pipeline is `Postgres → events.jsonl → per-run metrics → per-cell rollup → CSV / SVG / report.md`:

| Script | Responsibility |
|--------|----------------|
| `project_events.py` [N-2] | Queries the CQRS projection for one `run_id`; writes one event per line to `runs/<run_id>/events.jsonl`. |
| `run_metrics.py` [N-3] | Reduces a typed event list (or replayed `events.jsonl`) to a 17-field metric dict in one pass. |
| `collect.py` (per study) [N-4] | Walks enrolled run manifests, computes per-run metrics, accumulates per-cell totals, writes `run_metrics.csv` / `summary.csv` / `totals.csv`. |
| `plot_success.py` [N-5] | Hand-rolled SVG bar chart over `summary.csv` (plots **runs per cell** — not a derived success rate). |
| `render_report.py` [N-6] | Renders `report.md` from a Jinja template fed by the CSVs + manifest + `enrollment.lock.yaml`. |

### 7.5.2 How the math actually accumulates

Inside `metrics_from_events` [N-3a], one pass over the typed event list increments a flat dict. Counters use `_incr`, cost fields use `_add_float`, and `run_duration_seconds` is **assigned, not accumulated**. Two event types feed disjoint cost lanes but a shared `tokens_*` bucket:

```
TokensConsumed       → tokens_{prompt,completion,total}              + llm_cost_usd
WorkerCostRecorded   → tokens_{prompt,completion,total,reasoning}    + worker_cost_usd
RunCompleted         → run_duration_seconds   (overwrite, not accumulate)
total_cost_usd       = round(llm_cost_usd + worker_cost_usd, 6)              [N-3b]
```

`TokensConsumed` is emitted **only** by the orchestrator (`_query_llm` call sites: BOSS assess, MANAGER decompose, judge) and `WorkerCostRecorded` **only** by worker adapters via `event_sequencer.cost_recorded` — verified disjoint by code-path tracing [N-7]. Summing them into the same `tokens_*` field is an intentional cross-channel additive disjoint union today, but the script has **no structural `usage_id` guard**: a future adapter that emits both for the same call would silently double-count.

Per-cell rollup [N-4a] sums every metric field across enrolled runs (so the per-cell duration is a wall-clock total, not a mean — `_summarize` performs **no** division anywhere). All three CSVs share the same `METRIC_FIELDS` schema.

### 7.5.3 Atomicity & provenance

`write_md` writes Markdown with `sort_keys=True` YAML frontmatter and a body hash that excludes the frontmatter (`output_sha256` covers body bytes only, so the `generated_at` timestamp does not churn the validator) [N-8]. `write_binary` writes via `tempfile.mkstemp` + `Path.replace` and upserts a `.generated.json` sidecar under `fcntl.LOCK_EX` [N-9]. The body-hash boundary is **byte-stable** writer↔validator: a single `\n` separator is stripped before hashing on both sides — verified, no whitespace normalisation, no CRLF rewriting.

### 7.5.4 Edge cases — handled vs not

| Property                                                                        | Handled? |
|---------------------------------------------------------------------------------|----------|
| Deterministic `tool_calls_by_type` ordering (`dict(sorted(...))`)               | ✅       |
| Re-render with permuted-but-equivalent inputs                                   | ✅ (validator catches body drift) |
| Empty cell → zero-valued row, no DBZ                                            | ✅ (no division anywhere) |
| Body hash boundary stable across writer/validator                                | ✅       |
| Multiple `RunCompleted` events                                                  | ❌ last-write-wins, no count assertion [N-3c] |
| Missing `RunCompleted` (failed/aborted run)                                     | ❌ silently reports `0.0` from `empty_metrics()` [N-3c] |
| `tokens` reported as `0` (legitimate, e.g. cancelled call)                      | ❌ `0 or X` evaluates `X` [N-3d] |
| `tokens=None` with cache tokens present                                         | ❌ fallback drops `cache_read`+`cache_write` [N-3d] |
| `NaN` / `Inf` in `cost_usd` / `duration_seconds`                                | ❌ `_float` accepts them; poisons rollup [N-3e] |
| Truncated `events.jsonl` (partial-write recovery)                               | ❌ reader silently skips `JSONDecodeError` [N-3f] |
| Missing `events.jsonl`                                                          | ❌ `metrics_from_events_jsonl` returns `empty_metrics()` silently [N-3f]; `collect._metrics_for_run` propagates that as a clean zero-row [N-4b] |
| Concurrent `--parallel >1` matrix runs                                          | ❌ `harness._read_last_run_id` reads a shared single-slot `.last_run.json`; jobs can swap attribution [N-11] |
| Corrupt `.generated.json` sidecar                                               | ❌ silently reset, prior provenance lost [N-12] |
| Stale `cell` / `task` / `replicate` in `enrollment.lock.yaml` vs `run_manifest.json` | ❌ `validate_study` checks run-id presence only [N-13] |

### 7.5.5 Known issues at audit time

> **STATUS — 2026-05-11: All 13 issues FIXED on branch `feature/experiments-rearchitecture`.** Fixes landed across 6 commits (`b1f3bbd`, `542c777`, `7b40103`, `8d76db6`, `c531ff4`, `4994fad`) plus property-test commit. Verification independently corroborated by 3 Opus + 2 Codex agents. Codex caught a critical refinement to N-3 the original audit's proposed fix would have preserved (`WorkerCostRecorded.total_recorded_tokens` returns `self.tokens` first — fix had to update the property AND the metrics reader AND the Claude-SDK adapter test). N-4 fix uses Codex's alternative option (d) — per-invocation result file via `ARISE_RUN_RESULT_PATH` env var — preserving `--parallel >1` support that audit option (c) would have killed.

Severity column: **CRITICAL** = silently corrupts published numbers; **MATERIAL** = visible bias or silent class of bug; **MINOR** = correctness-relevant but no realistic data harm at current usage; **NIT** = polish. Citations in `[N-k]` resolve in §7.5.7 below.

| ID   | Severity | Path                                                      | Failure mode                                                                                                                                                                | Auditor consensus |
|------|----------|-----------------------------------------------------------|---|---|
| N-1 | CRITICAL | `project_events.py:76-79` [N-2a]                          | Non-atomic JSONL write; a killed harness leaves a truncated file that the reader silently skips. Fix: tmp+rename, mirror `write_report._atomic_write_bytes`.                | 5/5 |
| N-2 | CRITICAL | `harness.py:311-336` [N-14]                                | Projection failure is swallowed by `except Exception:`; `register_run` is called unconditionally, so the run enrolls as a zero-event row indistinguishable from a clean zero. Fix: re-raise, or stamp `projection_status: failed` and have `collect.py` exclude such rows. | 5/5 |
| N-3 | CRITICAL | `run_metrics.py:134` [N-3d]                                | `tokens_total` undercounts cache tokens on Claude-SDK cells. Claude adapter sets `tokens = prompt + completion` (no cache); OpenHands adapter sums all 5 kinds. The short-circuit `total = _int(_get(event,'tokens')) or (prompt + completion + reasoning)` picks up the adapter-reported value verbatim and the fallback also drops cache. Cross-cell comparison is asymmetrically biased against B-family. Fix: mirror `WorkerCostRecorded.total_recorded_tokens` exactly (`prompt + completion + cache_read + cache_write + reasoning`); distinguish `tokens is None` from `tokens == 0`; surface cache buckets as their own columns. | 5/5 |
| N-4 | CRITICAL (under `--parallel >1`) | `harness.py:190-304` + `presentation/cli.py:212` + `run_matrix.py:202` [N-11] | `.last_run.json` is a shared single-slot pointer written once at the END of each `main.py` invocation (`save_last_run` in the `finally` block of `cli.py:_run_orchestration`). Under `--parallel >1` the race window is the gap between Job A's `main.py` exit and Job A's harness reading the pointer; any other concurrent `main.py` whose `finally` block runs inside that window overwrites the slot. The harness's `_last_run_snapshot` "before/after" guard only catches "subprocess died without writing" — it does **not** detect "another subprocess wrote in between," because both jobs see `curr_boss_id != prev_boss_id` and enrol the latest writer. Symptom: duplicate enrolments of the same `run_id` with different `(cell, task, replicate)` stamps, plus orphaned runs whose events.jsonl exists on disk but is not referenced from `enrollment.lock.yaml`. Fix: return `run_id` over stdout from `main.py run`, OR write a per-subprocess `.last_run.<pid>.json`, OR enforce `--parallel 1` for the `arise` runner. | 2/5 (primary-source verified) |
| N-5 | MATERIAL | `run_metrics.py:167-183` + `project_events.py:78` [N-3g, N-2a] | Discriminator handles 7 patterns; the projector emits no `event_type` field (`model_dump_json()`), so the other **25 of 32** `DomainEvent` subclasses silently fall through to `"Unknown"`. No current field-set collision, but brittle to future class additions. Fix: dump events as `{"event_type": type(event).__name__, **event.model_dump(mode="json")}`; convert `_event_type` to a dict lookup. | 5/5 |
| N-6 | MATERIAL | `run_metrics.py:210-216` [N-3h]                            | Tool-name parser expects `"Tool: <name>"`, but `tool_formatters.py` emits `Running:`, `Reading:`, `Writing:`. `tool_calls_by_type` is systematically mislabeled for B-family runs. Fix: emit `tool_name` as a structured field on `ThoughtCaptured`. | 5/5 |
| N-7 | MATERIAL | `run_metrics.py:142-143` [N-3c]                            | `run_duration_seconds` is overwritten, not accumulated; missing `RunCompleted` silently reports `0.0`; multiple silently last-write-wins. Fix: assert exactly one `RunCompleted`; emit a distinct sentinel when absent. | 3/5 |
| N-8 | MATERIAL | `run_metrics.py:201-207` [N-3e]                            | `_float` accepts `nan`/`inf`; a single bad value poisons the rollup via `nan + anything = nan`. Fix: `if not math.isfinite(v): return 0.0`. | 3/5 |
| N-9 | MATERIAL | `write_report.py:267-276` [N-12]                           | Corrupt `.generated.json` sidecar is silently reset; next `write_binary()` writes a one-entry sidecar and orphans all prior provenance. Fix: raise on parse failure. | 2/5 (verified) |
| N-10 | MATERIAL | `validate_reports.py:522` [N-13]                          | `validate_study` checks only run-id presence; stale `cell`/`task`/`replicate` drift between `enrollment.lock.yaml` and `run_manifest.json` passes validation. Fix: compare full tuple, or include each manifest in `inputs_sha256`. | 2/5 (verified) |
| N-11 | MINOR    | `register_run.py:107`                                     | Deterministic `.tmp` filename races on same `run_id` (downstream of N-4). Fix: `tempfile.mkstemp` for the staging path. | 3/5 |
| N-12 | MINOR    | `run_metrics.py:145-150`                                  | Sub-microcent rounding-order quirk: `total = round(llm + worker, 6)` then components rounded independently → invariant `total == round(llm+worker, 6)` is violated at the 5e-7 scale (immaterial at LLM prices ≥ $1e-4). Fix: round components first, then sum. | 2/5 |
| N-13 | NIT      | `plot_success.py:41, 56`                                  | Cell labels & chart title not XML-escaped; title says "matrix" while plotting `runs` per cell. | 3/5 |

### 7.5.6 Verified-correct properties (positive findings)

These were independently checked by at least two auditors and re-verified by the synthesizer:

1. **Token-attribution channels are disjoint by code-path tracing** — `TokensConsumed` only from `agent_orchestrator.py` (3 emit sites); `WorkerCostRecorded` only from `event_sequencer.py:140`. Summing into one `tokens_*` field is an intentional cross-channel union, not double-counting [N-7]. (Structural-guard gap stated.)
2. **No current `_event_type` discriminator collisions** — AST-enumerated all 32 concrete `DomainEvent` subclasses; no field set is a superset of any of the 6 inferred tuple patterns. Brittle to future additions; safe today.
3. **Markdown body-hash boundary is byte-stable across writer and validator** — `write_report.py` writes `frontmatter_bytes + b"\n" + body_bytes`; `validate_reports.py` strips exactly that single `\n` before hashing. No whitespace normalisation, no CRLF rewriting [N-8, N-10].
4. **Frontmatter is deterministic across re-renders** — `yaml.safe_dump(..., sort_keys=True)`; `json.dumps(..., indent=2, sort_keys=True)`. `output_sha256` excludes `generated_at`, so re-render timestamps do not churn validation.
5. **`tool_calls_by_type` ordering is deterministic** — `dict(sorted(...))` in `run_metrics.py`, `json.dumps(..., sort_keys=True)` in `collect.py:_finalize_row`.
6. **`_summarize` performs no division** — per-cell metrics are sums, not means; no DBZ on empty cells.
7. **`write_binary` is atomic + locked** — `tempfile.mkstemp` + `Path.replace`; sidecar upserts serialised by `fcntl.LOCK_EX` on a hashed `/tmp` lockfile.
8. **`load_runs` snapshot semantics are consistent** — `Path.glob` + atomic manifest writer; a partial file cannot appear; a missing one can be lagged but not corrupted.
9. **`validate_reports` provenance pipeline catches body and input drift** — body sha over body bytes; binary outputs sha over full file via `.generated.json`. Tamper test present in `test_validate_reports.py:123-146`.
10. **Cost rounding (apart from N-12) is safe at study scale** — float64 carries ~15 decimal digits; per-run rounding once at the leaf; per-cell re-accumulation at 6dp is immaterial at real LLM prices.

### 7.5.7 Test-coverage gaps

`experiments/shared/scripts/tests/test_run_metrics.py` has only **2 tests** (one happy path + one malformed line). Untested branches that the audit recommends covering:

- Event-type dispatch: `TokensConsumed`, `ProbeStarted`, `ProbeCompleted`, `ThoughtCaptured(output_type="output")`, `WorkerCostRecorded` with `tokens=None` (exposes N-3), with `tokens=0`, with non-zero `cache_*`, all 25 silently-Unknown classes (exposes N-5), multiple `RunCompleted` (exposes N-7), missing `RunCompleted` (exposes N-7).
- I/O: missing file, empty file, UTF-8 BOM, invalid UTF-8 bytes (the `errors="replace"` path), truncated final line (exposes N-1 × N-9), `null` JSON line, non-dict JSON, large file (>100 MB) memory regression.
- Numeric edge cases: NaN/Inf/`OverflowError` in `_float`/`_int` (exposes N-8); property-based round-trip from typed `DomainEvent` → JSONL → metrics.
- Tool-name parser: empty content, no-newline content, `Running:` / `Reading:` / `Writing:` content (exposes N-6), JSON-string content, non-ASCII names.
- Aggregation: `_summarize` direct tests (none today), `collect.py` mid-flight enumeration, parallel attribution under `--parallel >1` (exposes N-4), projection-failure enrollment (exposes N-2), `register_run` concurrency and non-mapping `dataset.yaml`.
- Provenance: `validate_study` cell/task/replicate drift (exposes N-10), corrupt `.generated.json` reset (exposes N-9), CRLF frontmatter fence, malformed-but-validly-hashed body.
- Plotting / rendering: XML-escape, zero-rows, many-cells overflow, empty `totals.csv`, missing required columns.

**Minimum-bar recommendation.** A single Hypothesis test that draws a random instance of each `DomainEvent` subclass, dumps to JSONL via `model_dump_json()`, parses it back through `metrics_from_events_jsonl`, and asserts equality with `metrics_from_events([event])` would catch N-5, every parsing edge case, and any future field-rename drift. ~30 LOC.

### 7.5.8 Verdict

Pipeline math is correct on happy-path live data (verified empirically by multiple auditors on at least one `runs/<run_id>/events.jsonl`). However:

- The chain **N-1 × N-2 × reader silent-skip** means **any harness crash, container OOM, or projection failure produces a silently-undercounted row in `summary.csv` with no operator-visible signal**.
- **N-3 biases `tokens_*` ONLY.** Cost columns (`llm_cost_usd`, `worker_cost_usd`, `total_cost_usd`) come from the adapter-reported `cost_usd` field directly — they are NOT derived from the under-counted `tokens` field. So cost-based cross-cell analysis is unaffected by N-3; only token-based analysis is biased.
- **N-4 only fires under `--parallel >1`.** With `--parallel 1` (the default for paper-grade reproducibility) the race cannot trigger; the bug bites smoke tests and stress runs.

**Bottom line.** The pipeline is trustworthy for: serial (`--parallel 1`) runs, cost-based cross-cell comparison, happy-path event aggregation. It is **NOT yet trustworthy for**: token-based cross-cell comparison between Claude-SDK and OpenHands cells (until N-3 fixed); any run where projection might fail (N-1, N-2); parallel matrix dispatch (N-4). All four CRITICALs are fixable in <100 LOC each.

### 7.5.8a Post-fix status (2026-05-11)

All 13 issues are now resolved on `feature/experiments-rearchitecture`. Verification corroborated by 3 Opus + 2 Codex agents acting as independent code reviewers. Fixes landed across these commits:

| ID | Status | Commit | Notes |
|----|--------|--------|-------|
| N-1 | FIXED | `542c777` | `project_events._atomic_write_events_jsonl` uses `tempfile.mkstemp + fsync + replace + dir fsync`. Reader now raises on malformed lines (was silent skip). |
| N-2 | FIXED | `542c777` | Harness stamps `projection_status: ok\|failed` via `register_run`; `collect.build_enrollment_lock` excludes failed-projection rows. |
| N-3 | FIXED | `7b40103` | Three-side fix: `run_metrics` uses breakdown-sum-first; `WorkerCostRecorded.total_recorded_tokens` prefers breakdown over `self.tokens`; `claude_sdk_adapter._make_cost_event` emits inclusive total. `tokens_cache_read` and `tokens_cache_write` added to `METRIC_FIELDS`. |
| N-4 | FIXED | `b1f3bbd` | Per-invocation result file via `ARISE_RUN_RESULT_PATH` env var (Codex's alternative to audit options a/b/c). Preserves `--parallel >1` support. |
| N-5 | FIXED | `8d76db6` | `project_events` writes `{event_type, ...}` payloads; `run_metrics._event_type` prefers the explicit discriminator with the heuristic kept as legacy fallback. |
| N-6 | FIXED | `8d76db6` | `tool_name: str \| None` added to `ThoughtCaptured`; `EventSequencer.thought` threads it through. Claude SDK and Google ADK adapters pass it. |
| N-7 | FIXED | `7b40103` | `run_completed_count` tracked + sentinel `-1.0` for missing `RunCompleted`. Duplicate `RunCompleted` warns with last-write-wins. |
| N-8 | FIXED | `7b40103` | `_float` and `_int` reject NaN/Inf/OverflowError. |
| N-9 | FIXED | `c531ff4` | `_read_generated_json` raises on corrupt JSON; `write_binary` pre-validates the sidecar BEFORE writing the binary. Same fix in `validate_reports._load_sidecar`. |
| N-10 | FIXED | `c531ff4` | `validate_study` parses each manifest and compares the full `(study_id, cell, task, replicate)` tuple. |
| N-11 | FIXED | `4994fad` | `register_run._write_run_manifest` uses `tempfile.mkstemp`. |
| N-12 | FIXED | `7b40103` | Components rounded first, then summed for `total_cost_usd`. |
| N-13 | FIXED | `4994fad` | `plot_success._render_svg` xml-escapes cell labels. Title rename skipped (audit overstated). |

**Test coverage added.** 27 new regression tests across `test_harness.py` (concurrency), `test_project_events.py` (atomicity), `test_run_metrics.py` (token cache, sentinel, NaN/Inf, discriminator, tool_name), `test_register_run.py` (mkstemp, projection_status), `test_collect_enrollment.py` (failed-projection exclusion), `test_validate_study.py` (tuple drift), `test_write_report.py` (corrupt sidecar), `test_plot_success.py` (XML escape), plus `test_metrics_property.py` — a parametrized round-trip test over all 33 concrete `DomainEvent` subclasses (audit §7.5.7 minimum bar).

**Tests intentionally updated** (codified pre-fix wrong behavior): `test_metrics_from_events_jsonl_skips_bad_lines` → `test_metrics_from_events_jsonl_rejects_malformed_lines`; `test_claude_sdk_adapter.py:256-263` (token total now inclusive).

**Operational follow-ups.** With N-3 fixed, re-running `collect.py` on any enrolled B-family runs will increase their reported `tokens_total` to include cache buckets. Current `enrollment.lock.yaml` is empty so no published numbers are invalidated, but if any historical study runs with `--parallel >1` were previously enrolled, N-4 means their `(cell, task, replicate)` attribution may have been racy — a one-time enrollment audit is worth doing before re-running affected studies.

### 7.5.9 §7.5 Citations

- [N-2] `experiments/shared/scripts/project_events.py:52-82`
- [N-2a] `experiments/shared/scripts/project_events.py:76-79`
- [N-3] `experiments/shared/scripts/run_metrics.py:44-156`
- [N-3a] `experiments/shared/scripts/run_metrics.py:83-156`
- [N-3b] `experiments/shared/scripts/run_metrics.py:145-150`
- [N-3c] `experiments/shared/scripts/run_metrics.py:142-143`
- [N-3d] `experiments/shared/scripts/run_metrics.py:131-139`
- [N-3e] `experiments/shared/scripts/run_metrics.py:201-207`
- [N-3f] `experiments/shared/scripts/run_metrics.py:44-58`
- [N-3g] `experiments/shared/scripts/run_metrics.py:167-183`
- [N-3h] `experiments/shared/scripts/run_metrics.py:210-216`
- [N-4] `experiments/2026-04-23-initial-secbench/scripts/collect.py:198-329`
- [N-4a] `experiments/2026-04-23-initial-secbench/scripts/collect.py:198-205`
- [N-4b] `experiments/2026-04-23-initial-secbench/scripts/collect.py:176-182`
- [N-5] `experiments/2026-04-23-initial-secbench/scripts/plot_success.py:28-86`
- [N-6] `experiments/2026-04-23-initial-secbench/scripts/render_report.py:121-149`
- [N-7] `core/application/agent_orchestrator.py:737, 901, 978` + `infrastructure/adapters/worker/shared/event_sequencer.py:140`
- [N-8] `experiments/shared/scripts/write_report.py:99-176`
- [N-9] `experiments/shared/scripts/write_report.py:206-264`
- [N-10] `experiments/shared/scripts/validate_reports.py:97-528`
- [N-11] `experiments/shared/harness.py:190, 293-304` + `experiments/shared/scripts/run_matrix.py:202`
- [N-12] `experiments/shared/scripts/write_report.py:267-276`
- [N-13] `experiments/shared/scripts/validate_reports.py:465-528`
- [N-14] `experiments/shared/harness.py:311-336`

---

## 8. Run artifacts — what's on disk

### 8.1 Per-run layout

```
runs/<run_id>/
├── run_manifest.json          structured outcome + provenance
├── effective_config.yaml      fully-resolved Pydantic Settings (db password redacted)
├── events.jsonl               hierarchy-rooted event projection (post-run dump)
├── stdout_stderr.log          raw Claude CLI stream-json — FLAT-MODE A1/A2 ONLY
├── mcp_servers.in_container.json   MCP registration — FLAT-MODE A1/A2 ONLY
├── secb-exec                  generated wrapper proxying commands into worker container
├── src/<project>/             host mirror of container /src — PERSISTS WORKER EDITS
└── testcase/                  worker deliverables
    ├── base_commit_hash
    ├── repro.sh               Repro-Creator's deterministic trigger
    ├── repo_changes.diff      output of `git diff [BASE_COMMIT]`
    ├── model_patch.diff       canonical worker deliverable — the security fix
    ├── security_report.md     Reporter's final report
    └── …
```

`run_manifest.json` carries: `cell`, `task`, `replicate`, model names, costs, `exit_status`, `deliverables`, `git_sha`, `uv_lock_sha256`, **`invocation_sha256`** (hash over `(task, settings, domain_context_path)` — distinguishes near-duplicate runs) [63, 64].

### 8.2 Two cells produce file artifacts that the other four do not

| File                            | Origin                  | Present in       |
|---------------------------------|-------------------------|------------------|
| `stdout_stderr.log`             | `ClaudeCodeWorker` raw stdout | A1, A2 (flat only) [65] |
| `mcp_servers.in_container.json` | `ClaudeCodeWorker`      | A1, A2 (flat only) [66] |

B1/B2/C1/C2 use in-process adapters (`ClaudeAgentSDKAdapter`, `OpenHandsAdapter`) that yield `DomainEvent` objects directly; the only thinking trail for hierarchical cells lives in `events.jsonl` [65].

### 8.3 Extracting worker-generated code

The bind-mount design means there is no copy step needed: everything the worker writes to `/src` or `/testcase` shows up live under `runs/<run_id>/{src,testcase}/`. After a run completes:

```bash
cat runs/<run_id>/testcase/repo_changes.diff   # all source edits the Builder made
cat runs/<run_id>/testcase/model_patch.diff    # the security fix produced by the Fixer
cat runs/<run_id>/testcase/repro.sh            # the Repro-Creator's deterministic trigger
ls  runs/<run_id>/src/                         # full host mirror of container /src
```

The `git diff` deliverables (`repo_changes.diff`, `model_patch.diff`) are **prompted contracts** rather than orchestrator-side captures: `prompts/domains/secbench/worker/builder.j2:54` and `worker/fixer.j2:76-84` instruct the workers to write them themselves [67].

### 8.4 Per-study aggregation

`collect.py` copies the per-run trace into `experiments/<study>/artifacts/<cell>/<task>/replicate-<n>/<run_id>/`, holding mirrors of `run_manifest.json`, `events.jsonl`, `effective_config.yaml`, `stdout_stderr.log`, and the entire `testcase/` directory [68]. **This directory is created on demand** — if `collect.py` hasn't been run since the last `runs/<run_id>/` was created, the mirror is absent.

**Citations.** [63] `experiments/shared/scripts/register_run.py` · [64] sample `runs/<uuid>/run_manifest.json` · [65] `infrastructure/workers/claude_code_worker.py:92, 464-471` vs `infrastructure/adapters/worker/claude_sdk_adapter.py` · [66] `infrastructure/workers/claude_code_worker.py:294` · [67] `prompts/domains/secbench/{worker/builder.j2:54, worker/fixer.j2:76-84}` · [68] `experiments/2026-04-23-initial-secbench/scripts/collect.py:94-99, 146-173`

---

## 9. Onboarding a newly-reported CVE

For a CVE you saw yesterday — say `foo.cve-2024-99999` — five concrete steps:

1. **Verify or publish the upstream eval image.** SEC-bench's pipeline must already have published `hwiwonlee/secb.eval.x86_64.foo.cve-2024-99999:patch`. If not (brand-new CVE), either drive SEC-bench's preprocessor to publish it, or set `docker_image_override` in your CVE JSON to a custom tag and supply a `dockerfile` body that clones the project, checks out `base_commit`, and provides a `secb` helper at `/usr/local/bin/secb` [33, 35].

2. **Author** `deployment/cve-instances/foo-cve-2024-99999.json` — at minimum [33]:

   ```json
   {
     "instance_id": "foo.cve-2024-99999",
     "repo": "owner/foo",
     "project_name": "foo",
     "lang": "c",
     "work_dir": "/src/foo",
     "sanitizer": "AddressSanitizer",
     "bug_description": "<free-form>",
     "base_commit": "<commit-sha-at-vulnerable-state>"
   }
   ```

   Optionally fill `bug_report`, `sanitizer_report`. For ground-truth scoring (offline), also fill `patch` and `candidate_fixes`. Both are stripped from every prompt by `to_template_context()` — see §5.2 [36]. **Watch out**: `deployment/cve-instances/njs-cve-2022-32414.json` ships with a `repro()` function inside `secb_sh` that is a placeholder (`# TODO: Add commands to trigger the specific vulnerability`) — `build()` and `patch()` are real, but `repro()` only runs a generic `njs poc.js`. Don't use that fixture as a template until `repro()` is fleshed out.

3. **Build the per-CVE secb-tools image** (adds Valgrind, KLEE attempt, MCP, Claude on top of the eval image) [35]:

   ```bash
   bash deployment/build-secbench-tools.sh foo.cve-2024-99999
   ```

   The script resolves the shorthand to `hwiwonlee/secb.eval.x86_64.foo.cve-2024-99999:patch`, builds the layered image as `secb-tools:foo.cve-2024-99999-patch`, and smoke-tests `claude --version` / `mcp.server.fastmcp` import / `valgrind --version`. **KLEE may be skipped** on bases where the apt package is unavailable; the build proceeds anyway.

4. **Wire into the study** — edit `experiments/<study>/dataset.yaml` [2]:

   ```yaml
   default_cves:
     - foo.cve-2024-99999
   source:
     kind: deployment-json
     paths:
       - deployment/cve-instances/foo-cve-2024-99999.json
   ```

5. **Validate & smoke-run** [6, 7]:

   ```bash
   uv run python -m experiments.shared.scripts.validate_manifest --study 2026-04-23-initial-secbench
   uv run python -m experiments.shared.scripts.run_matrix \
     --study 2026-04-23-initial-secbench \
     --cells B2 \
     --tasks foo.cve-2024-99999 \
     --replicates 1 --parallel 1
   ```

After the run, inspect `runs/<run_id>/events.jsonl`, `runs/<run_id>/testcase/`, and the resulting `experiments/<study>/reports/` before broadening to the full matrix.

---

## 10. Data handoff for external analysts

### 10.1 What's reproducible from disk alone

An analyst gets full re-analysis power from two surfaces:

1. **Per-run host directory** `runs/<run_id>/` — the projection of all events for the run's hierarchy plus deliverables.
2. **The Postgres event store** — the source of truth that `events.jsonl` is projected from. `events.jsonl` is the **hierarchy-rooted** projection: it walks `ChildSpawned` from the run's BOSS via a recursive CTE [61]. In practice every agent in a healthy run is reached this way, but events on aggregates outside that subtree (shared-context, run-level) are not included. If an analyst needs those, they must query Postgres directly.

The harness wraps the projection in `try/except` and continues enrollment on failure — so `events.jsonl` may be missing or partial for an unhealthy run. Check the harness logs for `event projection failed for run_id=…` if a run's `events.jsonl` looks short [69].

### 10.2 Recommended handoff bundle

```
<study>/
├── manifest.yaml
├── dataset.yaml
├── configs/                          # cell overlays
├── reports/
│   ├── enrollment.lock.yaml          # canonical list of run_ids
│   ├── tables/{summary,run_metrics,totals}.csv
│   ├── figures/cells-overview.svg
│   ├── report.md                     # SHA-checked
│   └── matrix-summary.md
└── artifacts/<cell>/<task>/replicate-<n>/<run_id>/
    ├── run_manifest.json
    ├── events.jsonl                  # primary data source
    ├── effective_config.yaml
    ├── stdout_stderr.log             # A1/A2 only
    └── testcase/                     # model_patch.diff, repro.sh, security_report.md, …
```

Also include:

- `uv.lock` — to reconstruct the Python environment recorded by `run_manifest.uv_lock_sha256`.
- Repo at the `git_sha` recorded in `run_manifest.json`.
- `deployment/secbench-tools.Dockerfile`, `deployment/build-secbench-tools.sh`, and the exact image tags used (re-pinning these matters; see §10.4).
- All `deployment/cve-instances/*.json` referenced by the study's `dataset.yaml`.

### 10.3 Adding custom numeric methods

The canonical analyst workflow is to write a small Python script that consumes `events.jsonl` and composes with `metrics_from_events_jsonl()` [52]. If the analyst wants their output to inherit tamper-checking, they should emit via `experiments/shared/scripts/write_report.py:write_md` / `write_binary`, which stamps `inputs_sha256` / `output_sha256` frontmatter (or sidecar JSON for binaries) so `validate_reports.py` will re-verify their artifact [59].

### 10.4 Non-reproducible surfaces

- The upstream `hwiwonlee/…:patch` image is referenced **by tag, not by digest**. If upstream rebuilds the tag (which they can), an analyst pulling later will get different bytes. Pin by digest if exact reproduction matters.
- LLM weights drift (Claude Sonnet 4.6, Qwen 3.5 cloud) — model identity is recorded but the underlying weights aren't.
- The lazy `apt-get install -y klee` in the MCP server pulls live packages on first call — versions drift.
- The `secb-tools` layered image's apt install is not version-pinned.

**Citations.** [69] `experiments/shared/harness.py:310-321`

---

## 11. Appendix A — Invariant audit

| # | Invariant                                                                 | Status | Mechanism                                                                                          | Caveats                                                                                                                              |
|---|---------------------------------------------------------------------------|--------|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| i | **Golden Patch must NOT leak (image + prompt).** Use `:patch` or `:recent`? | **SAFE — `:patch` is correct.** | (a) Prompt layer: `_PROMPT_FORBIDDEN_FIELDS = {patch, candidate_fixes}` + 4 tests assert no gold/candidate markers appear in A-flat, B-BOSS, or per-phase manager/worker renderings [70]. (b) Image layer: upstream `:patch` literally `rm -rf /testcase/model_patch.diff` before commit; `/src` is left at vulnerable `base_commit` (per SEC-bench `build_eval_instances.py:542-552`, validated upstream). The runtime `docker cp`s these into the host workspace verbatim — so what reaches the worker is the SEC-bench cleaned state. **There is no `:recent` codepath; `:patch` is the only tag wired and is by-construction the correct one.** | Two validators (Opus + Codex) independently confirmed. One observation: `repo_changes.diff` (a SEC-bench build-stub diff, NOT the gold fix) is *not* stripped — workers could read it, but it contains build scaffolding, not the security fix. |
| ii | **KLEE + Valgrind always via MCP.**                                       | **HOLDS at the MCP layer.** | Both tools registered unconditionally at import time in `security_tools_server.py` [47].             | `klee_run` performs lazy `apt-get install -y klee` on first call; structured `klee_install_failed` returned on failure. `security.tools: [valgrind]` filters only prompt advertising, not MCP availability. |
| iii | **Identical tool surface across the experiment.**                         | **PARTIALLY VIOLATED — by design.** | MCP surface (`valgrind_run`, `klee_run`) and secb-exec are uniform [47]. Worker CLI allowlist deliberately diverges A1↔A2 (`disallowed_tools: ["Task"]` is the A axis) [9, 10]; worker engine deliberately diverges B↔C (Claude vs Qwen/OpenHands). | The "identical" property is enforced where the experiment requires it (MCP, framework, prompts) and relaxed where the experiment is varying it (sub-agent decomposition, model family). |
| iv | **2nd layer = Builder/Exploiter/Fixer; last worker of each subtree must be a "verifier" — enforced via `assess.j2`.** | **PARTIAL.** | **(a) Tree shape is incomplete as worded**: BOSS prompt mandates **four** children — Builder, Exploiter, Fixer, **AND Reporter** [15]. (b) `assess.j2` prompt-mandates verifier-last for the three complex phases: `[Build-Verifier]`, `[Exploit-Validator]`, `[Fix-Aggregator]` [16]. **(c) This is prompt-enforced only, NOT code-enforced**: a `grep` for verifier-role names across `core/`, `plugins/`, `bootstrap/` returns zero hits. `agent_orchestrator._spawn_children` applies whatever subtasks the LLM returns without validating role names or final-worker placement [71]. | A non-compliant LLM could in principle skip the verifier role; in practice the prompt is strict enough that compliance is the norm. Adding a Python-side validator on the role-name pattern is a low-effort hardening. Also: `boss.j2:59-60` says "4-5 workers" while `assess.j2` says "exactly 6" — fix the BOSS prompt [15, 16]. |
| v | **Every worker runs in an isolated container.**                           | **HOLDS, per-WORKER, not per-run.** | `prepare_worker_execution` starts a fresh `docker run -d` per WORKER dispatch and `cleanup_worker_execution` does `docker rm -f` in a `try/finally` [38–40]. Container name = `"{prefix}-{root_id[:8]}-{agent_id[:8]}"` [41]. Workspace bind-mounts (`/src`, `/testcase`, `/arise-run`) are run-scoped and shared across the worker chain. | Latent contract: `plugin._sessions[root_id]` is keyed by root, raises on collision — safe today only because `max_concurrent_workers: 1`. If concurrency rises, the keying must change to `(root_id, agent_id)` [45]. |

**Citations.** [70] `plugins/security/tests/test_prompt_unification_invariants.py:185-227` · [71] `core/application/agent_orchestrator.py:484-746`

---

## 12. Appendix B — Cheat sheet

```bash
# Build secb-tools image for one CVE
bash deployment/build-secbench-tools.sh openjpeg.cve-2016-7445

# Validate the manifest before a run
uv run python -m experiments.shared.scripts.validate_manifest --study 2026-04-23-initial-secbench

# Smoke: one cell × one task
set -a && source deployment/.env && set +a && export POSTGRES_HOST=localhost
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench --cells B2 --tasks openjpeg.cve-2016-7445 \
  --replicates 1 --parallel 1 --no-render --no-continue-on-error

# Full matrix in parallel
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench --cells A1,A2,B1,B2,C1,C2 \
  --replicates 1 --parallel 4

# Walk descendant prompts from a BOSS UUID
uv run python main.py prompts --agent-id <BOSS-UUID> --format json

# Typed worker chain-of-thought (any cell, any worker)
jq 'select(.aggregate_id=="<worker-uuid>" and has("output_type"))' runs/<run_id>/events.jsonl

# Raw Claude CLI stream-json — flat-mode A1/A2 only
cat runs/<run_id>/stdout_stderr.log

# Worker-generated artifacts
cat runs/<run_id>/testcase/repo_changes.diff   # Builder's source edits
cat runs/<run_id>/testcase/model_patch.diff    # Fixer's security patch
cat runs/<run_id>/testcase/repro.sh            # Repro-Creator's deterministic trigger
ls  runs/<run_id>/src/                         # host mirror of container /src

# Regenerate per-study reports + provenance validation
uv run python experiments/2026-04-23-initial-secbench/scripts/collect.py
uv run python experiments/2026-04-23-initial-secbench/scripts/plot_success.py
uv run python experiments/2026-04-23-initial-secbench/scripts/render_report.py
uv run python -m experiments.shared.scripts.validate_reports --study 2026-04-23-initial-secbench

# Re-project events for a single run from Postgres to file
uv run python -m experiments.shared.scripts.project_events --run-id <uuid>
```

---

## 13. Open gaps (worth tracking)

**Pipeline correctness (4 CRITICAL, blocking for cross-cell paper numbers — full audit in §7.5.5):**

1. **Non-atomic `events.jsonl` write** (`project_events.py:76-79`) — N-1 in §7.5.5.
2. **Harness silently enrolls projection-failed runs as zero-event rows** (`harness.py:311-336`) — N-2.
3. **`tokens_total` undercounts cache tokens on Claude-SDK cells**, biasing B-family vs C-family comparison (`run_metrics.py:134`) — N-3.
4. **`.last_run.json` race under `run_matrix --parallel >1`** can swap attribution between concurrent jobs (`harness.py:190, 304` + `run_matrix.py:202`) — N-4.

**Hardening & polish:**

5. **No code-level enforcement of `assess.j2`'s role roster** — a compliant LLM is currently a soft assumption. A one-screen Python validator on subtask names would close this.
6. **Worker-side anti-cheat phrasing is not asserted**. `test_anti_cheat_rules_present_in_b_boss_prompt` covers only B-BOSS rendering; the per-phase worker/manager prompts are not directly tested for the `git checkout` / `git log --all` / `curl/wget` / "web search the fix" prohibitions.
7. **No shipped CLI walks leaf→BOSS.** `python main.py prompts --agent-id` walks descendants only; ancestry requires a manual `parent_id` chain.
8. **`boss.j2` vs `assess.j2` worker-count contradiction** ("4-5" vs "exactly 6") — see §3.5.
9. **Latent concurrency invariant on `_sessions[root_id]`** — see §5.3.
10. **Two duration clocks** — see §7.2; downstream analysis should standardize on one.
11. **Upstream `:patch` image is tag-referenced, not digest-pinned** — see §10.4.
12. **Event-type discriminator covers 7 of 32 `DomainEvent` subclasses** — N-5 in §7.5.5; brittle to future class additions.
13. **`tool_calls_by_type` mislabels real worker output** (records `Running`/`Reading`/`Writing` instead of `Bash`/`Read`/`Write`) — N-6 in §7.5.5.
14. **Test coverage for `run_metrics.py` is 2 tests** — see §7.5.7 for the recommended Hypothesis-based minimum bar.

---

## Methodology

This doc was produced by:

1. **5 independent investigations** (3 Opus + 2 Codex) each writing a fresh tech doc from source — none allowed to read the prior `experiment-setup-tech-doc.md`. The Agent tool's `model` parameter only distinguishes `opus|sonnet|haiku`, so the requested Opus-4.6 vs Opus-4.7 split could not be enforced at the tool layer; the three Opus instances were issued identically.
2. **1 synthesis pass** (Opus) producing a structured agreement/disagreement matrix.
3. **2 validators** (Opus + Codex) on the one material disagreement (image-layer gold-patch verdict). Both independently reached `SAFE` with primary-source citations from the upstream SEC-bench repo at `/Users/garfield/PycharmProjects/SEC-bench/secb/evaluator/build_eval_instances.py:542-552` (which literally `rm -rf /testcase/model_patch.diff` before committing the `:patch` tag).
4. **Spot-check pass** (this session) verified single-source citations for `_MAX_VERIFICATION_RETRIES=2`, `_JUDGE_PASS_THRESHOLD=60`, the `boss.j2` vs `assess.j2` "4-5 vs exactly 6" worker-count contradiction, and the `njs-cve-2022-32414.json` `repro()`-only TODO — all confirmed in source.
5. **Second audit pass (numeric pipeline)**: 3 Opus + 2 Codex auditors independently scrutinised `run_metrics.py`, `collect.py`, `plot_success.py`, `render_report.py`, `validate_reports.py`, `write_report.py`, `register_run.py`, `project_events.py`, and `harness.py`. 1 Opus synthesizer consolidated findings and re-verified every load-bearing claim against source. Result: §7.5 — 4 CRITICALs (silent-data-loss chain), 6 MATERIALs, 10 verified-correct properties, and a documented test-coverage gap list. The auditors agreed on every CRITICAL by 5/5 or 2/5 primary-source verified.

Source documents:

- `/tmp/tech-doc-{A,B,C,D,E}.md` — the five original investigations.
- `/tmp/synthesis.md` — the agreement/disagreement matrix.
- `/tmp/validator-{A,B}.md` — the validator verdicts.
