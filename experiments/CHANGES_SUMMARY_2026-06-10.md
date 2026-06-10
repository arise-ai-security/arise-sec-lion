# Working-tree change summary — branch `experiment/2026-06-09` (as of 2026-06-10)

Uncommitted changes (modified + untracked), tests excluded. Two themes dominate:
the **shared code-prefix cache** (cross-worker source reuse via events, behind
`orchestration.shared_worker_session`) and the **N1/N2/B3/B4 cost-family experiment
harness** (OpenHands flat-mode wiring, grounded SQL metrics, study folders).

---

## 1. Prompts (`prompts/`)

**New templates**

- `prompts/context/shared_code.j2` — renders the `<provided_source_files>` block:
  an append-only log of source files prior workers already read. A path may repeat;
  the last `<file>` entry is current, a `<file_modified>` note marks staleness.
  Append-only by design so each worker's prompt shares the longest possible byte
  prefix with its predecessor's (OpenAI prefix-cache friendly).
- `prompts/domains/secbench/phases/_detached_exec.j2` — guidance to run long
  commands (sanitizer builds, repro loops) detached via `nohup` + sentinel file +
  short polling calls, instead of blocking one shell call past the ~300 s tool limit.

**Modified templates**

- `cve.j2` — CWE classification and per-CWE tool prescriptions condensed from ~64
  verbose lines to ~14 compact ones (same content, fewer tokens). Decomposition
  guidance now gated behind `include_decomposition_guidance` so only roles that
  produce subtasks (boss/manager/assess) see it; workers and the flat baseline don't.
- `phases/build.j2`, `phases/exploit.j2`, `phases/fix.j2` — validation gates rewritten
  to include `_detached_exec.j2` and run builds / 3× repro loops detached with
  sentinel polling rather than blocking inline.
- `worker.j2`, `phases/_mindset.j2` — "verification judge" terminology replaced with
  "evaluation harness" throughout; new instruction to read source with the
  file_editor `view` tool instead of shell `cat`/`head`/`less` (this is what makes
  shared-code capture observable). `worker.j2` adds a "source already in context /
  do NOT re-view" bullet gated on `shared_code_enabled`.
- `worker/builder.j2`, `worker/exploiter.j2`, `worker/fixer.j2` — per-role
  `shared_code_enabled` conditionals telling workers to build on
  `<provided_source_files>` instead of re-viewing; judge→harness wording.

## 2. Code logic

### A. Shared code-prefix cache (feature flag `orchestration.shared_worker_session`)

Source files a worker's `view` tool returns are persisted as events and re-injected
verbatim into later workers' prompts. Off by default; when off, prompts are
byte-identical to before.

- **New port** `core/ports/shared_code_context_port.py` — `SharedCodeContextPort`
  Protocol (`record_view`, `record_edit`, `code_block`).
- **New domain events** in `core/domain/events/events.py` — `SourceFileObserved`
  (verbatim content + sha256) and `SourceFileEdited` (stale marker); registered in
  `postgres_event_store.py`; new `AgentSession` domain methods
  `record_source_file_observed` / `record_source_file_edited` plus `_apply` handlers.
- **New provider** `infrastructure/adapters/worker/shared_code_context.py`
  (`SharedCodeContextProvider`) — rebuilds the canonical byte-stable block purely by
  replaying the run's SourceFile events (DB is the source of truth, no disk reads);
  append-only entry reduction (re-view dedup, edit→stale-note); render memo keyed by
  exact event-id set; emit-sink indirection (`bind_capture`) so the live OpenHands
  path yields events into the worker stream instead of an out-of-band OCC-racing
  append (Invariant D).
- **OpenHands adapter capture** (`openhands_adapter.py`) — classifies
  FileEditorObservation commands (`view` = capture, `str_replace/create/insert/
  undo_edit` = invalidate); `_SharedCodeCapture` binding + `_capture_file_tool`
  forwards observations to the port and yields the resulting events; best-effort
  (capture failure never breaks the run).
- **Orchestrator injection** (`agent_orchestrator.py`) — optional
  `shared_code_port`; resolves the run's block by `root_id` and passes it into
  `build_worker_prompt`; puts `root_id` into the worker task context for
  capture scoping.
- **Prompt plumbing** — `PromptContext.shared_code_block` field;
  `PromptBuilder.build_worker_prompt(shared_code_block=…)`; the security plugin's
  `SecBenchPromptStrategy` sets `shared_code_enabled` and inserts the pre-rendered
  block into the stable prefix (after CVE context + mindset, before the
  BEF-role-specific section).
- **Bootstrap wiring** — `bootstrap/infrastructure.py` constructs one provider
  (gated on the flag) shared by adapter capture and orchestrator rebuild;
  `bootstrap/application.py` injects it; `config/settings.py` adds
  `OrchestrationConfig.shared_worker_session` (default False).

### B. OpenHands flat mode + N2 native subagents

- `bootstrap/composition.py` — `_build_flat_worker` now wires `worker.tool=
  "openhands"` (OpenHandsAdapter wrapped in OpenHandsWorker); new
  `_flat_subagent_enabled` predicate: claude_code keys off `disallowed_tools`,
  openhands keys off `tool_params.openhands.enable_subagents` (note and capability
  stay in lockstep for the N1 vs N2 contrast).
- `config/settings.py` — `OpenHandsParams.enable_subagents` (bounded two-level
  delegation tree, N2 baseline).
- `infrastructure/workers/openhands_worker.py` — forwards `workspace.extras`
  (container session, MCP servers, helper script) into the adapter task context so
  flat-mode execution routes into the SEC-bench container.
- `core/application/services/prompt/prompt_builder.py` — `FLAT_SUBAGENT_NOTE`
  wording: "decompose" → "delegate".

### C. One shared container per run (`plugins/security/plugin.py`)

- `prepare` now **reuses** the run's existing container instead of raising
  "already active"; per-worker cleanup is intentionally a **no-op** (the container
  is reaped at process exit by the PID-labeled cleanup). Rationale: per-worker
  containers destroyed container-local state (installed tools, non-mounted scratch
  files) that later workers needed.

### D. Execution-environment identity on cost events

- `WorkerCostRecorded` gains `container_id` and `conversation_id`, threaded through
  `event_sequencer.cost_recorded`, `usage.emit_cost`, and the OpenHands adapter
  (`_conversation_identity` helper) — so run invariants (one shared container per
  run, one fresh conversation per worker) are checkable from the DB alone
  (`study_sql invariants`).

## 3. Scripts (`experiments/`)

- `shared/scripts/study_sql.py` (~870 lines) — grounded SQL metrics over Postgres
  `events` + `runs/*/run_manifest.json`. Subcommands: `runs`, `cost` (raw USD
  recomputed from price sheets + normalized USD), `cache` (hit rate by
  role/depth), `tools`, `files` (redundant cross-agent reads), `check` (recomputed
  vs reported cost drift), `invariants`. No log parsing, no estimates.
- `shared/scripts/curate_cve50.py` — seeded, idempotent curation of the shared
  50-CVE task set; writes per-study `dataset.yaml` + a lock file; picks a
  2-instance smoke pair whose Docker images already exist locally.
- `shared/scripts/dump_trajectory.py` — dumps a run's event trajectory as one
  diff-friendly line per event (`TOOLCALL <tool> :: <content>` normalization),
  filterable to all nodes / leaf workers / role substring.
- `run-cycle.sh` — runs all four cost-family smokes concurrently for one
  optimization cycle, logs to `temp/<cycle-label>/`.
- `smoke-all-2026-06-09.sh` — same concurrent 4-study smoke launch (8 runs in
  flight), dated variant.

## 4. Misc

- **Four new study folders** — `n1-openhands-linear` (flat OpenHands, subagents
  off), `n2-openhands-subagents` (flat + native subagents on; only delta vs N1),
  `b3-boss-bef-direct` (boss → BEF workers, `max_depth=1`, manager tier ablated),
  `b4-boss-manager-worker` (full 3-level tree, mixed models: sonnet-4-6
  boss/managers, gpt-5.4-mini workers). Each has `manifest.yaml`, `dataset.yaml`,
  `configs/`, `smoke.sh`, `reports/`.
- `experiments/shared/groups.yaml` — new group `N: Naive OpenHands`.
- **Experiment docs**:
  - `COST_FAMILY_README.md` — family design; hypothesis: normalized cost
    `N1,N2 > B3 > B4` (full tree cheapest via manager-level prompt-cache reuse).
  - `SMOKE_FINDINGS_2026-06-09.md` — 2-instance smoke, all 8 runs green;
    **headline: the hypothesis is inverted** on the smoke data.
  - `B4_COST_OPTIMIZATION_2026-06-09.md` — in-progress B4 cost-reduction log;
    billing audit shows 0.00% drift between logged `cost_usd` and recomputed
    token×price.
- `experiments/shared/datasets/` — `cve50-2026-06-09.lock.yaml`, smoke metric
  snapshots, dumped trajectories.

## Branch context (already committed on `experiment/2026-06-09`, not in this diff)

Recent committed work this uncommitted diff builds on: native OpenHands subagent
support for N2 (`3f8562e`), cache-token accumulation fix in the summary projection
(`42924af`), extraction of shared secbench phase partials (`11cac34`), and all-path
prompt caching + template split + metrics (`05bfd61`).
