# Experiments Re-Architecture

Design proposal for running A/B/C… matrix comparisons end-to-end via a single
command, producing a reproducible `report.md` with provenance. Status:
**proposal**, not yet implemented. Derived from an iterative design session;
this document is the canonical record of the decisions reached.

---

## 1. What we want

One command that runs a full matrix and emits a structured report:

```
uv run python -m experiments.shared.scripts.run_matrix \
    --study 2026-04-23-initial-secbench \
    --cells A1,A2,B1,B2,B3
```

- **Input**: the study's pinned design (cells + dataset) under
  `experiments/<study>/` and the 10 CVE fixtures under
  `deployment/cve-instances/`.
- **Output**: per-run artifacts under `runs/<uuid>/`, plus
  `experiments/<study>/reports/report.md` with tables, figures, and a frozen
  enrollment roster.
- **Matrix axes**: groups (A = Claude Code CLI, B = Our System, C = future)
  × variants within each group × dataset tasks × replicates.

**Non-goals:** cross-study aggregation, continuous benchmarking, publishing.
This is the single-study driver.

---

## 2. What's broken today

Findings from reading the current codebase:

| # | Problem | Evidence |
|---|---|---|
| 1 | `manifest.yaml` mixes design inputs with runtime outputs | `cells:` (design) + `runs:` (executed run_ids) in one file |
| 2 | `harness:` field in manifest is **not execution-load-bearing** — never parsed, never dispatched on; the only consumer is `render_report.py:40` which echoes it into the rendered report for display | Grep: no dispatcher reads it; render_report uses `entry.get("harness", "")` as a display string |
| 3 | `config:` field is asymmetric: load-bearing for B cells, documentation-only for A cells | A1/A2 files contain the comment *"NOT consumed by `main.py run`"* |
| 4 | No explicit group concept exists today — cells are flat in the manifest; any group-level labeling must be inferred from cell-name letter prefixes (`A*` vs `B*`), which is fragile and duplicated across scripts. The proposed re-architecture introduces `group:` + shared labels in `groups.yaml` | Grep: zero `group_label` references in current tree; report-side grouping is implicit |
| 5 | **B1, B2, B3 are byte-identical except for the header comment** — three "variants" are the same treatment | `diff` and `sha256` confirm |
| 6 | Parallel pipelines: `run_claude_code.py` (baseline) vs `main.py` (ours) can silently drift on shared invariants (briefing, tool policy, timeout, env) | `run_claude_code.py:149` already shares `prompts/domains/secbench/briefing.md`, but tool policy + timeout + env are separately hardcoded |
| 7 | No matrix driver exists; enrolling runs requires hand-calling `harness.py run-ours` / `run-baseline` per `(cell, task, attempt)` | `harness.py:490+` only has per-run subcommands |
| 8 | `attempt` field is used for *replicates* but named for *retries*; 0-indexed tuples repeat with different UUIDs | Current manifest has 5× `(B2, njs.cve-2022-38890, attempt=0)` |
| 9 | Baselines don't get workspace prep (`/src`, `/testcase`, `secb`) — known fairness gap | `harness.py:398` comment: *"KNOWN LIMITATION"* |

---

## 3. Design principles

1. **Inputs and outputs are separated.** Design files never mutate during
   execution. Runtime state lives in the runs pool and in derived snapshots.
2. **Open/Closed.** Adding a new group (C), variant (B7), or worker backend
   (gemini_cli) is file-addition only — no conditionals in dispatch code.
3. **Configs are load-bearing and authentic.** Every field in a cell config
   corresponds to a real code path. Pydantic `extra="forbid"` prevents dead
   knobs.
4. **One pipeline.** "Baseline" and "ours" are both valid configurations of
   the same generic system. No parallel codebases.
5. **Invariants come from one source.** Task briefing, tool policy,
   timeouts, workspace prep — each has exactly one producer consumed by
   every mode.
6. **Provenance over process.** Every run records the exact config it ran
   under plus the lockfile hash. The report derives from an immutable frozen
   roster, not from whatever happens to be in the runs pool at render time.

---

## 4. Target architecture

### 4.1 Three concerns, three files, three lifecycles

| Concern | File | Lifecycle | Written by |
|---|---|---|---|
| **Design** — what to compare | `experiments/<study>/manifest.yaml` | Written once; committed; immutable during study | Human author |
| **Execution** — what actually ran | `runs/<uuid>/run_manifest.json` + `effective_config.yaml` | Append-only; one dir per run | The single runner |
| **Publication snapshot** — what the report was derived from | `experiments/<study>/reports/enrollment.lock.yaml` | Regenerated before each report; committed alongside `report.md` | `collect.py` |

Pattern parallels `pyproject.toml` (intent) + `uv.lock` (materialized snapshot).

### 4.2 File layout

```
experiments/
├── shared/
│   ├── groups.yaml                      # project-wide group registry
│   ├── runners/
│   │   ├── __init__.py                  # registry + auto-discovery
│   │   └── aris.py                      # THE runner; handles every cell
│   │                                    #   (exceptions: truly external baselines)
│   └── scripts/
│       ├── run_matrix.py                # NEW: matrix driver
│       ├── validate_manifest.py         # NEW: pre-flight validator
│       ├── collect.py                   # derives enrollment.lock.yaml
│       ├── render_report.py             # reads lockfile → report.md
│       └── validate_reports.py          # pre-commit hook
└── 2026-04-23-initial-secbench/
    ├── manifest.yaml                    # design only
    ├── dataset.yaml
    ├── configs/                         # per-cell overlays
    │   ├── A1-claude-code-subagent.yaml
    │   ├── A2-claude-code-nosubagent.yaml
    │   └── …
    ├── templates/
    └── reports/
        ├── enrollment.lock.yaml         # OUTPUT: frozen roster
        ├── tables/
        ├── figures/
        └── report.md                    # OUTPUT
```

### 4.3 Manifest schema (design only — no `runs:` key)

```yaml
study_id: 2026-04-23-initial-secbench
created_at: 2026-04-23T00:00:00Z
hypothesis: "…"
dataset: dataset.yaml
replicates: 1                              # samples per (cell, task)

cells:
  A1:
    group: A                                # ← references experiments/shared/groups.yaml
    runner: aris                            # ← references runner registry
    config: configs/A1-claude-code-subagent.yaml
  A2:
    group: A
    runner: aris
    config: configs/A2-claude-code-nosubagent.yaml
  B1:
    group: B
    runner: aris
    config: configs/B1-ours-naive.yaml
  B2:
    group: B
    runner: aris
    config: configs/B2-ours-cybersec.yaml
  B3:
    group: B
    runner: aris
    config: configs/B3-ours-full.yaml
```

Three fields per cell. No `harness:`, no `group_label:`, no duplication.
**Validator** rejects: unknown group, cell name not starting with group
letter, missing config file, unknown runner.

### 4.4 Groups registry (shared across studies)

```yaml
# experiments/shared/groups.yaml
A: Claude Code CLI
B: Our System
# C: OpenHands                # add here to introduce group C; no code change
```

Keys are single uppercase letters (regex `^[A-Z]$`). Shared because groups
are project-wide invariants; a study that re-defines `A` would make
cross-study comparisons meaningless.

### 4.5 Runner registry

Default: **one runner, `aris`**, which handles every cell by routing through
`main.py run -c <config>`. The registry remains for the rare 5% case — a
truly external baseline that cannot be expressed as a config of our system.

```python
# experiments/shared/runners/__init__.py
from typing import Protocol, runtime_checkable
from pathlib import Path
from uuid import UUID

@runtime_checkable
class Runner(Protocol):
    id: str
    label: str
    def run(self, *, study_id: str, cell: str, task: str,
            replicate: int, config: Path, context_file: Path) -> UUID: ...

_REGISTRY: dict[str, Runner] = {}

def register(r: Runner) -> Runner:
    if r.id in _REGISTRY:
        raise ValueError(f"runner id collision: {r.id!r}")
    _REGISTRY[r.id] = r
    return r

def get(rid: str) -> Runner:
    try: return _REGISTRY[rid]
    except KeyError:
        raise KeyError(f"unknown runner {rid!r}; known: {sorted(_REGISTRY)}")
```

Dispatch is `runners.get(cell_spec["runner"]).run(...)` — no string
parsing, no `if/elif`.

### 4.6 Cell configs as overlays (not snapshots)

Every cell config is a minimal overlay on `config/config.yaml`. The delta
**is** the experimental hypothesis. Strict Pydantic schema (`extra="forbid"`)
at every nesting level prevents dead fields.

```yaml
# A1 — Claude Code CLI in flat mode, sub-agents enabled
extends: config/config.yaml
overrides:
  orchestration.mode: flat                  # no boss/manager decomposition
  worker.tool: claude_code
  worker.tool_params.disallowed_tools: []   # Task tool allowed
  domain.plugin: null

# A2 — same as A1 except Task tool disallowed
extends: config/config.yaml
overrides:
  orchestration.mode: flat
  worker.tool: claude_code
  worker.tool_params.disallowed_tools: ["Task"]
  domain.plugin: null

# B1 — hierarchical, openhands worker, no domain scaffolding
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  domain.plugin: null

# B2 — B1 + security domain recon toolsets
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  domain.plugin: security
  domain.params:
    toolsets.recon.enabled_for_roles: [boss, manager]
    toolsets.recon.allowed_tools:
      [search_codebase, read_file, get_file_structure]

# B3 — B2 + deeper topology + full recon tool set
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  orchestration.topology.max_depth: 4
  orchestration.topology.max_total_agents: 60
  domain.plugin: security
  domain.params:
    toolsets.recon.enabled_for_roles: [boss, manager, pending]
    toolsets.recon.allowed_tools:
      [search_codebase, read_file, get_file_structure,
       get_symbols_overview, read_symbol]
```

**Critical invariant:** reading `diff A1 A2` or `diff B1 B2` tells you
exactly what the study manipulates. Today's configs fail this (B1/B2/B3 are
byte-identical — see Problem 5).

### 4.7 Single pipeline with `orchestration.mode`

Add a new explicit knob to `core/application/orchestrator`:

```
orchestration.mode: flat | hierarchical
```

**Why a new knob is required (resolves Open Question 2):**
Setting `topology.max_depth: 0` does NOT achieve flat mode. In the current
code:

- `TopologyConfig.is_depth_limited` (config/settings.py:163) returns
  `self.max_depth > 0` — a value of `0` means depth limit is **disabled**
  (unlimited), not zero.
- `HierarchyLimits.is_depth_limited` (core/domain/values/limits.py:27) uses
  the same semantic.
- `ExecutionService._create_boss_session` (core/application/
  execution_service.py:552) is unconditional — a BOSS agent is always
  created as the root.
- `build_role_handlers` (core/application/services/lifecycle/
  role_dispatch.py:179) registers BOSS/MANAGER/WORKER handlers every run.

So flat-mode behavior cannot be expressed by tuning existing knobs; it
requires a new code path that short-circuits BOSS creation when
`mode == "flat"` and dispatches the task prompt directly to the worker.

- **flat**: skip BOSS/MANAGER entirely; the worker receives the task
  prompt directly. Event stream contains worker events only.
- **hierarchical**: existing BOSS → MANAGER → WORKER behavior.

The cell config — not the Python module — determines which mode runs. A1/A2
set `flat`; B1/B2/B3 set `hierarchical`. One codepath, one event stream, one
metrics surface.

### 4.8 Invariant layer (the fairness contract)

Pure functions in `core/application/run_invariants.py` consumed by both modes:

```python
build_task_prompt(briefing, cve_context, task)   → TaskPromptSpec
build_timeouts(config)                           → TimeoutBudget
build_env_policy()                               → frozenset[str]
build_workspace_spec(config, cve_context)        → WorkspaceSpec
```

**Rule:** these are the *only* producers for their respective invariants.
Nothing else in the codebase constructs a `TaskPromptSpec`; `ToolPolicy` is
composed once at bootstrap (`composition.py`) from top-level `worker.*_tools`.

| Invariant | Source today | Source after |
|---|---|---|
| Task briefing | `prompts/domains/secbench/briefing.md` (already shared ✅) | `build_task_prompt` (unchanged) |
| CVE context JSON | `--domain-context-file` (already shared ✅) | `build_task_prompt` (unchanged) |
| Tool policy | Baseline: hardcoded `--disallowedTools Task`; Ours: config `tool_calling.policies` | `ToolPolicy` composed at bootstrap (`composition.py`) → renders to CLI flags for claude_code, in-process allowlist for openhands |
| Timeouts | Baseline: hardcoded 1800s; Ours: `worker.timeout` + `orchestration.max_run_duration_seconds` | `build_timeouts(config)` |
| Env allowlist | Baseline strips env; Ours inherits full | `build_env_policy()` applied everywhere |
| Workspace prep | Baseline: none (known gap); Ours: `SecurityDomainPlugin.prepare()` | `build_workspace_spec(config, cve)` applied to both modes |

### 4.9 Worker adapters (replace `run_claude_code.py`)

Everything moves behind `core/ports/worker_port.py`:

```
infrastructure/workers/
├── claude_code_worker.py     # invokes `claude -p` behind WorkerPort
├── openhands_worker.py       # existing
└── google_adk_worker.py
```

`ClaudeCodeWorker` materializes a scratch `settings.json` from the cell's
`ToolPolicy` every run (via `CLAUDE_CONFIG_DIR`), so Claude Code's settings
are derived from config — no committed file to drift.

`experiments/shared/baselines/` is **deleted** once migration completes.

### 4.10 Matrix driver

```python
# experiments/shared/scripts/run_matrix.py (sketch)
def main(study, cells_filter=None, tasks_filter=None, parallel=1,
         continue_on_error=True):
    manifest = load_manifest(study)
    validate_manifest(manifest)              # fail fast
    dataset  = load_dataset(manifest)

    # Effective CVE resolution must respect per_cell_overrides from the current
    # dataset schema (default_cves + per_cell_overrides.<cell>.subset). Reuse
    # harness._resolve_effective_cves to avoid re-implementing the semantic.
    jobs = [
        (cell_name, task, replicate)
        for cell_name, spec in manifest["cells"].items()
        if cells_filter is None or cell_name in cells_filter
        for task in (tasks_filter or resolve_effective_cves(dataset, cell_name))
        for replicate in range(manifest.get("replicates", 1))
    ]
    results = dispatch_jobs(jobs, parallel=parallel,
                            continue_on_error=continue_on_error)

    # Post-execution pipeline (reuses existing scripts)
    collect.main(study)                      # writes enrollment.lock.yaml
    plot_success.main(study)
    render_report.main(study)
    validate_reports.main(study)
    write_matrix_summary(results, study)     # skipped/failed/succeeded per cell
```

**Defaults:**
- Sequential (`parallel=1`). Cap at 3–4 if raising (Docker + LLM rate-limit
  contention).
- `continue_on_error=True` with a clear failures table in the summary.
- Shard per-task, not per-cell, so each Docker container lifecycle is
  isolated.

### 4.11 Provenance

Recorded per run in `runs/<uuid>/`:
- `run_manifest.json` — runtime metadata (existing) + `study_id`, `cell`,
  `task`, `replicate`, `uv_lock_sha256`.
- `effective_config.yaml` — merged overlay + base, materialized at run
  time. Future researchers read this to know exactly what ran.

### 4.12 Enforcement (what prevents drift)

1. **Pydantic strict mode** on all config models: `extra="forbid"` at every
   level. Dead fields cannot exist.
2. **Manifest validator** (`validate_manifest.py`): unknown group, unknown
   runner, missing config file, cell-name/group-letter mismatch. Runs at
   driver start and as a pre-commit hook.
3. **Invariant tests** (`tests/experiments/test_invariants.py`): assert
   task prompt / tool policy / timeouts are byte-identical between
   `mode=flat` and `mode=hierarchical` for the same CVE.
4. **Report validator** (`validate_reports.py`, existing, extended): every
   number in `report.md` must trace to an enrolled `run_id` in
   `enrollment.lock.yaml`, and the lockfile must match the pool state at
   commit time.
5. **Lockfile hash comparison**: two enrolled runs with different
   `uv_lock_sha256` surface in the report as a warning (potential
   dependency drift confounder).

---

## 5. Data flow (target)

```
┌────────────────────────────────────────────┐
│  manifest.yaml  dataset.yaml  configs/*    │  INPUT: design
└────────────────────┬───────────────────────┘
                     │ validate_manifest
                     ▼
┌────────────────────────────────────────────┐
│  run_matrix.py                             │
│    for (cell, task, replicate) in matrix:  │
│      runners.get(cell.runner).run(…)       │
└────────────────────┬───────────────────────┘
                     │ each invocation =
                     │   uv run python main.py -c <merged_overlay> run <task>
                     ▼
┌────────────────────────────────────────────┐
│  main.py                                   │
│    builds TaskPromptSpec, ToolPolicy,      │  invariant layer
│    TimeoutBudget, WorkspaceSpec (shared)   │
│    dispatches on orchestration.mode:       │
│      flat  → WorkerPort.run_task           │
│      hier. → boss → manager → Worker       │
└────────────────────┬───────────────────────┘
                     ▼
┌────────────────────────────────────────────┐
│  runs/<uuid>/                              │  EXECUTION: per-run state
│    run_manifest.json (study_id stamped)    │
│    effective_config.yaml                   │
│    events/, logs/                          │
└────────────────────┬───────────────────────┘
                     │ collect.py (glob + filter by study_id)
                     ▼
┌────────────────────────────────────────────┐
│  reports/enrollment.lock.yaml              │  OUTPUT: frozen snapshot
│  reports/tables/  reports/figures/         │
│  reports/report.md                         │
└────────────────────────────────────────────┘
```

---

## 6. Migration plan

Five PRs, each independently verifiable by diffing `report.md` (byte-identical
at every step except where deltas are expected).

| PR | Change | Verification |
|---|---|---|
| 1 | Split inputs from outputs: drop `runs:` from `manifest.yaml`; `collect.py` derives `enrollment.lock.yaml` from `runs/*/run_manifest.json` filtered by `study_id` | Lockfile entries match current `manifest.yaml.runs` list; `report.md` unchanged |
| 2 | Add `experiments/shared/groups.yaml`, `runners/` registry, `validate_manifest.py`; rewrite cells to `{group, runner, config}`; delete the descriptive-only `harness` field (render_report.py:40 switches to reading `cell.runner` for its display column) | Validator passes; `report.md` unchanged |
| 3 | **Schema + overlay resolver prerequisite.** Extend `Settings.from_yaml` (config/settings.py:348) to resolve `extends:` + `overrides:` into a single merged config before validation. Add new Pydantic models: `orchestration.mode: Literal["flat","hierarchical"]`, `worker.tool_params` (tagged union keyed by `worker.tool`), `domain.plugin`, `domain.params`. Strict `extra="forbid"` at every nesting level. Without this, PRs 4 and 5 have nothing to load | Unit tests: overlay loader produces identical dict to hand-merged YAML; schema rejects unknown keys; all existing B configs still parse after being rewritten to overlay form |
| 4 | Extract invariant layer (`core/application/run_invariants.py`); port `run_claude_code.py` → `infrastructure/workers/claude_code_worker.py` behind `WorkerPort`; implement `orchestration.mode: flat` short-circuit in `ExecutionService` (skip `_create_boss_session` when flat; dispatch task prompt to worker directly) | **Normalized** comparison (not byte-identical) of `run_manifest.json` between old baseline path and new flat-mode A1 path: `run_id`, `exit_status`, `git_sha`, `variant`↔`worker.tool`, tokens ±5%, wall-time ±10%. Field-shape differences (`kind: claude_code_baseline` vs main-path `kind`, presence of `invocation_sha256` / `models` / `deliverables` in main) are **expected** — the new path produces the richer main-path manifest shape; that's the migration goal. Invariant tests green. |
| 5 | Rewrite B1/B2/B3 configs as overlays with real semantic deltas (the naive/cybersec/full distinction); delete `experiments/shared/baselines/`; switch harness to `main.py` with overlay configs only | Each B cell's `diff` shows intended deltas; no "documentation-only" configs remain |
| 6 | Ship `run_matrix.py` + `uv.lock` sha in `run_manifest.json` + `effective_config.yaml` snapshot; hook `validate_manifest` + `validate_reports` into pre-commit | One command produces a complete matrix report end-to-end |

Field rename `attempt` → `replicate` happens inside PR 1 (schema version bump).

**Dependency chain:** PR 3 is the new prerequisite gate. PRs 4, 5, 6 all
depend on the overlay resolver + schema being in place.

---

## 7. Open questions

### Resolved

- **Q2 (RESOLVED): `mode: flat` requires a new knob — `topology.max_depth:
  0` cannot be used as a shortcut.** Verified against current code:
  `max_depth <= 0` disables the depth limit (treated as unlimited) in both
  `config/settings.py:163` and `core/domain/values/limits.py:27`;
  `ExecutionService._create_boss_session` (core/application/
  execution_service.py:552) is unconditional; `build_role_handlers`
  (core/application/services/lifecycle/role_dispatch.py:179) always
  registers all roles. PR 4 must add an explicit
  `orchestration.mode: Literal["flat", "hierarchical"]` and short-circuit
  BOSS/MANAGER creation when flat.

### Still open (must resolve before coding starts)

1. **What does `naive` / `cybersec` / `full` mean concretely?** The current
   B configs are identical. Before running the matrix, the semantic deltas
   between B1/B2/B3 must be declared (which knobs, which toolsets, which
   topology). Otherwise the study measures nothing.
2. **Baseline fairness vs. cross-lab comparability.** Under consolidation,
   baselines inherit our workspace prep (`/src`, `/testcase`, `secb`).
   That's strictly better fairness for our comparison but diverges from
   "raw Claude Code on SEC-bench" numbers other labs might publish. Should
   we support an opt-in `workspace: raw` mode for external-facing numbers?
3. **Replicate semantics.** Current data has up to 5× the same
   `(cell, task, attempt=0)` tuple with different UUIDs. Going forward: is
   `replicate` 0-indexed and monotonic per (cell, task), or is `run_id` the
   sole identity with `replicate` as a clustering label? Pick one and
   enforce in `register_run.py`.

---

## 8. Tradeoffs explicitly accepted

- **Single runner by default** over a registry-first design. Adding a new
  worker (gemini_cli) = one adapter file + one enum entry. Adding a truly
  external baseline = one runner module. The registry stays, but is
  almost empty.
- **Sequential execution by default.** Parallelism opt-in via `--parallel
  N`, capped. Docker daemon contention and LLM rate limits are real and
  hard to tune per-machine; sequential is correct-by-default.
- **Overlay configs over snapshot configs.** Reproducibility comes from
  `effective_config.yaml` in the run dir + `uv.lock` hash, not from
  full-config snapshots in cell files. Cell files express intent.
- **Baselines gain workspace prep.** Fairer for our comparison; documented
  departure from how Claude Code runs "in the wild."

---

## 9. Terminology (to avoid future confusion)

- **Study** — a single experimental design, rooted at
  `experiments/<study_id>/`. Has one manifest, one dataset, one report.
- **Cell** — a row in the study's `cells:` table. Opaque name (convention:
  `<GROUP_LETTER><N>`); carries `group`, `runner`, `config`.
- **Group** — display bucket (A, B, C) for aggregation in reports. Declared
  in `experiments/shared/groups.yaml`. No semantic pairing across groups
  (A1 does *not* correspond to B1).
- **Runner** — dispatcher that executes a cell. Default: `aris` (our
  system). Registry allows exceptions for external baselines.
- **Replicate** — independent sample of the same `(cell, task)` pair for
  statistical power. Monotonic 0-indexed.
- **Attempt** — deprecated; do not reuse. Was confusingly named for what
  is actually a replicate.
- **Enrollment** — the roster of `run_id`s that "belong to" a study. Lives
  in `reports/enrollment.lock.yaml`, derived from the pool, frozen with
  the report.
