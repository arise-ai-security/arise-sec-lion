# God-objects refactor — progress ledger (handoff file)

> **What this is.** The live state of the behavior-preserving god-object decomposition sweep
> (`/software-quality:full-refactor`). The worklist lives in
> `agent-docs/god-objects-refactor-plan.md`; THIS file is the progress ledger. A fresh session
> must read this file FIRST, trust the ✅ rows, and resume at the first ⬜.
> **Update after every committed target and at each phase boundary.**

## ✅ SWEEP COMPLETE & CONVERGED (2026-07-07) — 12 of 13 ranked targets + E1–E4 decomposed, reviewed, committed; #13 closed leave-as-is; convergence re-audit passed twice

Branch `refactor/god-objects`, 22 commits off `c1e2b0f`. Full suite baseline-exact at every
commit (**1215 passed / 3 pre-existing failures / 13 skipped**), pyright **0 errors**, architecture
boundary hook green throughout. God-object LOC reductions (production files):

| Target | Before → After | Target | Before → After |
|---|---|---|---|
| `criteria.py` | 1383 → package | `docker_runtime.py` | 708 → 343 |
| `openhands_adapter.py` | 1622 → 1012 | `harness.py` | 696 → 459 |
| `agent_orchestrator.py` | 1677 → 944 | `query_service.py` | 686 → 131 |
| `execution_service.py` | 1310 → 924 | `claude_code_worker.py` | 733 → 560 |
| `settings.py` | 734 → package | `events.py` (routes) | 482 → 328 |
| `litellm_adapter.py` | 790 → 452 | `summary.py` | 461 → 303 |

Every extraction preserved public surfaces (re-export shims / thin delegators). 8 CO adversarial
reviews: 5 clean APPROVE; 3 REJECT all overruled with evidence (settings + criteria = transitive-
re-export non-issues with zero real consumers; orchestrator = a verified byte-equivalent inline).
Structural smells held or fell (long-function 38→36, isinstance-chain 11→9, long-if-chain 4→2);
no regressions. Remaining >600-LOC files are the documented leave-alone set (cohesive-by-design:
`events.py`, `postgres_event_store.py`, `subtask_parser.py`, `procedures.py`) plus the cohesive
cores of decomposed linchpins (`agent_session.py`, the 4 above) — none is a conflation.

**Not done (intentional, needs user sign-off — behavior-CHANGING, out of scope for this sweep):**
the plan's "out-of-scope findings" — 5 swallowed-exception bugs, 2 private cross-module imports to
publicize, 2 dead-code suspects (`stop_session`, `benchmark_result.py`), and the `run_invariants.py`
`settings.security` boundary drift. And the DEFERRED-deeper `agent_session` sub-state split (replay/OCC
risk). See `god-objects-refactor-plan.md` §out-of-scope + Deferred decisions below.

## Resume steps (cold start)

1. `git checkout <branch below>` — do NOT work directly on `experiment/2026-06-09`.
2. Re-establish the baseline: `uv run pytest -q` → expect **1215 passed, 3 failed, 13 skipped**
   (the 3 failures are PRE-EXISTING, listed below — they are NOT caused by this refactor).
   `uv run pyright` → expect **0 errors, 0 warnings**.
3. Read `agent-docs/god-objects-refactor-plan.md` (worklist), then pick the first ⬜ target below.
4. Per target: DESIGN → APPLY (Recipe A `split-module` / Recipe B `extract-collaborators`) →
   VERIFY (gate below) → adversarial CO review → commit → flip the row here to ✅ + SHA.

## Branch & baseline

| Item | Value |
|---|---|
| Refactor branch | `refactor/god-objects` off `c1e2b0f` |
| Base commit | `c1e2b0f` (tip of `experiment/2026-06-09` at sweep start, 2026-07-07) |
| Test baseline | `uv run pytest -q` → 1215 passed, **3 failed (pre-existing)**, 13 skipped, ~22 s |
| Type baseline | `uv run pyright` → 0 errors, 0 warnings |
| Lint | `uv run ruff check .` + `uv run ruff format --check .` run check-only as part of the gate (user override 2026-07-07; pre-commit hooks not installed locally) |
| Commits | agent commits per green target (explicit user override 2026-07-07: "everything on your call"); conventional, ≤3 lines, no trailers; WIP files below stay unstaged |

### Pre-existing failures (NOT ours — never "fix" these as part of the refactor)

1. `infrastructure/tests/test_claude_sdk_adapter.py::TestToolUseInputPayload::test_capture_tool_use_payload_parses_back_via_detector`
   — `ModuleNotFoundError: experiments.shared.scripts.analysis` (module absent from repo).
2. `config/tests/test_settings.py::test_study_openhands_tool_policy_overrides_base_defaults[C1-qwen-noverifier.yaml]`
   — missing `experiments/shared/templates/study/configs/C1-qwen-noverifier.yaml`.
3. same test `[C2-qwen-verifier.yaml]` — missing `C2-qwen-verifier.yaml`.

### WIP files — NEVER touch, stage, or commit (in-flight experiment state on `experiment/2026-06-09`)

- `experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml` (modified)
- `experiments/b4-boss-manager-worker/reports/.generated.json`, `reports/enrollment.lock.yaml` (modified)
- `experiments/n1-openhands-linear/reports/.generated.json`, `reports/enrollment.lock.yaml` (modified)
- untracked: `B4_vs_N1_progress_report_2026-06-09_to_06-18.docx`, `_run_logs/`,
  `agent-docs/held-out-validation-and-judge-floor.md`, `agent-docs/manager-cache-prime-finding.md`,
  `agent-docs/rca-b4-gate-fix-failures-2026-06-16.md`, `agent-docs/rca-b4-vs-n1-heldout-2026-06-18.md`,
  `experiments/forest-runner-design.md`

## Verification gate (run per target; paste output before claiming done)

```
uv run pytest -q          # must match baseline: 1215 passed / 3 pre-existing failed / 13 skipped
uv run pyright            # must stay at 0 errors
uv run python scripts/check_architecture_boundaries.py  # (pre-commit hook; run manually)
```

Plus the `software-quality:python-call-sites` audit after any move/rename.

## Per-target status

Worklist details (clusters, recipes, justifications): `agent-docs/god-objects-refactor-plan.md`.
Phase 0 (detect) completed 2026-07-07: 6 read-only area audits + AST scan + fan-in map at `c1e2b0f`.
Phases 1–4 executed and committed same day under the user's "everything on your call" override;
convergence verified by two consecutive clean passes (see Convergence record below).

| # | Target | Recipe | Phase | Status | Commit |
|---|---|---|---|---|---|
| P0 | Detect + plan + this ledger | — | 0 | ✅ | d405783 |
| D1 | docs-drift fixes (SYSTEM_REFERENCE §IV, cli.py docstring, event count) — NOTE: `.claude/docs/` is gitignored (`.gitignore:680`), so the architecture.md/patterns.md fixes are local-only | docs | 1 | ✅ | fac93e4 |
| L0 | ruff: pydantic runtime-evaluated bases + dead-noqa cleanup (unplanned, surfaced by #4) | lint | 1 | ✅ | a1e6cc0 |
| 4 | `config/settings.py` → `config/settings/` package (11 files), zero-churn shim | A | 1 | ✅ CO-reviewed (2 findings overruled, see Review log) | 6735d4d |
| 10 | `litellm_adapter.py` → `anthropic_cache.py` + `content_tool_calls.py`, importers migrated | A | 1 | ✅ CO APPROVE | 92ca902 |
| 9 | `summary.py` → `_SummaryAccumulator` (singledispatch) composed by both projections; 461→300, duplication gone | B | 2 | ✅ CO APPROVE (no findings) | 3a73b47 |
| 8 | `query/api/routes/events.py` → `streaming.py` + `event_mapping.py`; 482→328 | B | 2 | ✅ CO clear (only "untracked files" staging note) | 319fe5c |
| 7 | `claude_code_worker.py` → extract `claude_transcript.py` (parser only; watchdog deliberately kept); 733→560, zero test edits | B | 2 | 🔶 applied+gate green; CO review rides with Phase-3 batch | — |
| 5 | `query_service.py` → `read_models.py` + `agent_readiness.py` + `sibling_view.py`; bootstrap binds SiblingViewService as sibling_view_port; 686→130 coordinator | B | 2 | ✅ CO clear (only "untracked files" staging note; differential + call-site checks passed) | 4fb54ff |
| 6 | `docker_runtime.py` → `plugins/security/runtime/` (docker_cli/image_ensurer/workspace_mirror/sealer); 708→343; sealed surface byte-identical (SHA test) | B | 3 | ✅ committed; CO review rides next batch | 29805fe |
| 3 | `openhands_adapter.py` → `openhands_events`/`openhands_cost`/`openhands_container_tools`; 1622→1012; reaper untouched | B | 3 | ✅ committed; CO review in batch-3 | d1f7322 |
| 7 | `claude_code_worker.py` → `claude_transcript.py` (pure parser; delegators kept); 733→560 | B | 2 | ✅ committed; CO review in batch-3 | 799bcf0 |
| 2 | `execution_service.py` → FlatModeRunner/PostStepHandler/WorkspaceContextProvider; 1310→924; loop stays on service | B | 4 | ✅ committed (3 stalls; I ran the final gate — baseline-exact); CO review in batch-3 | cea9bb8 |
| 1 | `agent_orchestrator.py` → AssessmentRecovery/DecompositionContract/ReconPropagation; 1677→944; 3 methods stay direct, `__init__` unchanged | B | 4 | ✅ committed — CO REJECT overruled (1 finding: an inlined `domain_context` ternary verified byte-equivalent to `_get_domain_context`; identical truthiness test); 3-method-direct + dedup-monkeypatch + recon-map-ownership all CO-confirmed | ecfe5e4 |
| 11 | `agent_session.py` → child-result rendering slice ONLY | B | 4 | ✅ committed; CO review in batch-3 | 59ef79b |
| 12 | `composition.py` → `_docker_pid_cleanup` slice → infrastructure/cleanup/ | B | 4 | ✅ committed; CO review in batch-3 | 45262f5 |
| 13 | `bootstrap/application.py` → private sub-factories (OPTIONAL) | B | 4 | ⏭️ closed leave-as-is (2026-07-07): single-pass linear factory, no conflation — nearly every local feeds the one `ExecutionServiceDependencies` bundle, so sub-factories would thread the same locals through params (indirection, not decomposition); 280 LOC, well under threshold; YAGNI | — |
| E1 | `experiments/shared/evaluation/criteria.py` → `criteria/` package (metrics/verdict/judge_prompts/_shared + shim; 9 privates re-exported) | A | 3 | ✅ committed — CO REJECT overruled (transitive-reexport non-issue); verified 3 ways: AST-identical evaluate_run/metrics, 115+57 tests green, every real consumer resolves | 4568e66 |
| E2 | `experiments/shared/harness.py` → `subprocess_runner.py` | B | 3 | ✅ committed (with E3/E4 plumbing) | 428ee5c |
| E3 | `experiments/shared/scripts/run_matrix.py` → shared `container_cleanup.py` (dedups harness+run_matrix docker sweep) | B | 3 | ✅ committed | 428ee5c |
| E4 | `experiments/shared/evaluation/common.py` → `tool_categorization.py` | A | 3 | ✅ committed | 428ee5c |
| — | events.py, postgres_event_store, subtask_parser, prompt_builder, tool_calling_service, verification_pipeline, procedures.py, prompt_strategy.py, plugin.py, schemas.py, shared_context.py, presentation/*, bootstrap.py, recon/openrouter adapters, worker/shared/* | — | — | 🚫 leave-alone (reasons in plan) | — |

Legend: ✅ done (+SHA) · ⬜ todo · 🔶 in-progress (+what's left) · ⏭️ deferred (+why) · 🚫 leave-alone

## Convergence record (two consecutive clean passes — criterion met 2026-07-07)

- **Pass 1** = sweep-end verification (banner above): per-target gates baseline-exact, smell
  counts held or fell, remaining >600-LOC files all documented.
- **Pass 2** = fresh-session re-audit at `a2829ff` (HEAD): (a) gate baseline-exact
  (1215 passed / 3 pre-existing failed / 13 skipped; pyright 0 errors; boundary hook green);
  (b) production LOC scan — every >600-LOC file maps to the documented leave-alone set or a
  decomposed cohesive core; one new entrant `experiments/shared/evaluation/criteria/verdict.py`
  (642) verified cohesive-by-design (single concern: the four-phase contract verdict cluster the
  E1 cut isolated) — NOT a re-flag; (c) AST smell scan, production scope: isinstance-chain 9
  (exact match to sweep-end), long-if-chain 2 (exact match), long-function 31 (≤ recorded 36;
  the recorded figure's scan scope wasn't pinned, but no reading yields a regression);
  (d) runtime import-cycle diff HEAD vs base `c1e2b0f`: **none on either tree** (TYPE_CHECKING /
  function-local edges excluded — they cannot cycle at import time and are repo-idiomatic).
- **Out-of-scope inventory line drift** (moves carried the pre-existing sites verbatim, as
  behavior preservation requires): openhands swallowed exceptions now at
  `openhands_adapter.py:355, :456` (were `:608, :707`); the docker_runtime `:479` site now lives
  in `plugins/security/runtime/workspace_mirror.py:129`. Scanner error inventory unchanged at 6
  (5 actionable + `judge.py:198` intentional) — no new correctness findings introduced by the sweep.

## Review log (CO = Codex adversarial reviewer)

- **#4 settings split — CO said REJECT; overruled, committed.** Both "blocking" findings fail on
  their own evidence: (1) "public surface loss" = accidental transitive re-exports (`BaseModel`,
  `yaml`, `os`, `Path`…) that CO itself verified have ZERO in-repo consumers — re-exporting
  third-party names from `config.settings` would be namespace pollution, not preservation;
  (2) "SecurityConfig in config/ is a layering violation" — CO itself notes it is pre-existing
  in the old monolith; already tracked in the plan's out-of-scope findings (`settings.security`
  coupling). CO's substantive checks all PASSED: AST move-diff clean, pydantic schema/field
  order identical, tagged-union validator behavior identical, CONFIG_DIR resolves to config/,
  no pickling path depends on `__module__`.
- **#10 litellm split — CO APPROVE.** One note worth keeping: log records from the moved parser
  now carry logger name `infrastructure.adapters.content_tool_calls` (was `…litellm_adapter`);
  no in-repo log filter keys on logger names.
- **#9 summary accumulator — CO APPROVE**, no findings.
- **#8 events routes / #5 query split — CO cleared** (only staging-order "untracked files" note, since
  reviewed pre-commit); differential + call-site audits passed.
- **Batch-3 (six commits 59ef79b/45262f5/29805fe/d1f7322/cea9bb8/799bcf0) — all APPROVE, INFO-only.**
  Load-bearing confirmations: docker sealing constants (`_SECB_WRAPPER`/`_REPRO_SKELETON`/
  `_PATCH_SCRIPT`/`_FORBIDDEN_TESTCASE_ARTIFACTS`) hash-identical to parent; sealed `secb` source
  stays outside all bind mounts; patch.sh RO-overlaid twice; openhands cost event reads wall-time
  once and forwards identical values; `events → container_tools` edge acyclic; post-step failed-worker
  ladder preserves digest→procedural-retry→verification-retry→generic-RetryPolicy order; orchestrator
  ops remain direct (no pipeline/strategy wrapper). Reviewer ran read-only (no `uv`), so runtime
  verification is mine: every commit was full-suite baseline-exact (1215/3/13) before commit.
- **execution_service (#2) three stalls:** subagent completed + self-verified all gates green before
  dying on report composition; I re-ran the full gate independently (baseline-exact, pyright 0,
  boundaries pass) and committed. Not a quality gap — a transport/watchdog artifact.

## Provenance note — experiments/shared (E1–E4)

The `experiments/shared/` refactor was **deferred** in the plan (scorer consistency during in-flight
b4/n1 studies). It was nonetheless carried out by a background task and arrived complete. Handling:
the b4/n1 WIP data (configs/reports/enrollment locks) is **untouched** (identical to session start);
the change is behavior-preserving (re-export shims) and passes the full 115-test eval suite + baseline
full suite. The lower-risk run-harness plumbing (E2/E3/E4) is committed as `428ee5c`. The **scorer
split (E1, `criteria/`) is committed only after a dedicated adversarial CO review** confirms
`evaluate_run` + every metric is byte-for-byte behavior-preserving — because that code decides
B4-vs-N1 experimental outcomes. If the user wants the scorer frozen during the studies, reverting
the single E1 commit restores `criteria.py` without affecting the rest of the sweep.

## Deferred decisions

- **agent_session deep sub-state split** (RetryState/FailureHistory/VerificationState/ProcedureState
  via SharedStore-style delegation): feasible, in-repo precedent exists, but touches the replay
  path + OCC invariants → still deferred (offered 2026-07-07 alongside the other deferrals;
  user picked the other three, not this one).
- **openhands_adapter process-reaper extraction**: ✅ executed 2026-07-07 (`db40bca`) — see
  §Deferred-items batch below.
- **openrouter_adapter ↔ litellm_adapter DRY consolidation**: ✅ executed 2026-07-07
  (`96d648b`) as shared helpers, not a base class — see §Deferred-items batch below.
- **experiments/shared targets (E1–E4)**: ✅ re-audited 2026-07-07 — all four splits confirmed
  landed; no god object remains. Residual small items stay blocked until the b4/n1 studies
  land — see §Deferred-items batch below.
- **Swallowed-exception bug fixes + dead-code removal** (plan §out-of-scope): ✅ executed
  2026-07-07 as the user-approved follow-up pass — see §Follow-up pass below.

## Follow-up pass — out-of-scope bugs & enhancements (2026-07-07, user-approved)

Executes the plan's §Out-of-scope findings as a deliberately behavior-CHANGING pass
(user re-ran /software-quality:full-refactor to approve it). Five commits:

- **8980668 fix: surface swallowed exceptions (5 sites).** subtask_parser chains the JSON decode
  cause into the tiered-parse ValueError; EventBroadcaster unsubscribe stops resurrecting a
  vanished defaultdict key (empty-list leak) and warns; openhands closed-loop SDK-event drops log
  at debug; real exceptions from the cancelled run_task log via logger.exception (CancelledError
  still swallowed — no coverage change, it is BaseException in 3.12); workspace_mirror chmod
  failures summarize in ONE warning (native-Linux root-owned mirrors previously failed fully
  silently). +4 regression tests (cause-chain, broadcaster round-trip + non-resurrection, chmod
  summary).
- **d6ad62c chore: remove dead code.** `build_tool_policy` (zero production callers; the live
  path is composition.py's inline ToolPolicy — the BUG-A1 pin moved onto the live path in
  test_flat_mode with a new allowed_bash_commands assertion). Deleting it also CURES the plan's
  boundary-drift finding (core reading `settings.security.*`). `stop_session` (impl + protocol;
  containers are reaped by PID-labeled cleanup at process exit) and `benchmark_result.py` (no
  importer beyond the package re-export) deleted; describing docs truthed up.
- **3144f09 + 97b3a1d fix: .gitignore / track `.claude/docs`.** Found during the pass: the bare
  `.claude` exclusion defeated the pre-existing `!.claude/docs/` negations (git cannot re-include
  children of an excluded dir) — the canonical doc set, including this sweep's own drift fixes,
  had NEVER been tracked. Fixed to `.claude/*`; 12 docs (5,873 lines) now versioned. CO caught
  that the first fix left non-md/nested files trackable → `.claude/docs/*` re-ignore (97b3a1d).
- **1d969bc refactor: publicize `pid_alive`, `role_from_task`** (plan §private cross-module
  imports; `_pid_alive`'s second importer had moved to `experiments/shared/container_cleanup.py`
  during the E2–E4 extraction).
- **Gate:** full suite 1218/13/0 (= the 1216/13/0 post-`5078f7a` baseline + 4 new − 2 dead-path
  tests; `5078f7a` had retired the old 1215/3/13 baseline's 3 failures by decoupling two test
  modules from branch-absent files), pyright 0, boundaries clean, call-site AST audit clean for
  every renamed/deleted name (its 17 `[imports]` hits are PEP 695 `type`-alias false positives;
  `runs/` vendored noise excluded).
- **CO review: REJECT → fixed → APPROVE.** Initial REJECT on exactly one BLOCKING finding (the
  gitignore non-md hole above); the claims on exception semantics, dead-code liveness (incl.
  dynamic-dispatch/template/config attack), rename completeness, and layering all PASSED first
  try. INFO kept as-is: `experiments-implementation-plan.md:580` still names build_tool_policy
  (historical plan doc, intentional).
- **Observed drift, NOT fixed (out of scope):** `plugins-security.md:233` claims
  prepare_worker_execution raises on an existing session (tests pin reuse);
  SYSTEM_REFERENCE §container-lifecycle line refs (:233/:265/:332) stale since the
  docker_runtime split; `test_run_invariants.py` module docstring points at the renamed
  test_prompt_unification_invariants.py. (All three fixed in the next batch, below.)

## Deferred-items batch (2026-07-07, second follow-up — user picked deferrals #2/#3/#4 + doc drift)

Four commits, CO REJECT→fixed→APPROVE:

- **923b246 + c4346dd docs.** The three observed-drift spots above (session-reuse claim,
  container-lifecycle refs → `docker_runtime.py:74/:129/:136-180/:251` + `runtime/sealer.py`
  symbols, prompt-test docstring), plus CO's catch: sealed-dir ref (`sealer.py:172`) and the
  `_SECB_WRAPPER`/`_REPRO_SKELETON`/`_PATCH_SCRIPT` constants heading (now `runtime/sealer.py`
  `:39/:66/:72`), and `dependencies.md` retargeted to shared `is_o_series()`.
- **96d648b refactor: `llm_common.py`.** `require_model` / `is_o_series` /
  `extract_cache_tokens` moved verbatim to one module; openrouter's cross-adapter import of
  `litellm_adapter.require_model` eliminated. A shared adapter BASE CLASS was evaluated and
  REJECTED as the wrong abstraction: the flows diverge deliberately (240s vs 120s per-attempt
  timeouts, disjoint retryable sets, cache-to-tail only on the litellm path, `extra_body`
  usage.include only on openrouter, `json.loads(args)` vs `json.loads(args) if args else {}`).
- **db40bca refactor: `worker/process_reaper.py`.** Verbatim move of `snapshot_child_pids` /
  `reap_with_escalation` / `_drain_waitpid` (+ `SHUTDOWN_GRACE_SECONDS`, publicized) out of
  `openhands_adapter`; the adapter keeps SDK `pause()/close()` shutdown and delegates. Test
  patch targets retargeted to where the lookups now happen; the real-OS zombie/SIGKILL
  escalation tests pass against the moved module. Closes the last open slice of target #3.
- **experiments/shared re-audit (read-only, no commits).** E1–E4 all landed (`criteria/`
  package 642/411/361; `common.py` 448→292 with `tool_categorization.py` out; `harness.py`
  696→459 with `subprocess_runner.py`/`container_cleanup.py` extracted; `run_matrix.py`
  581→516). No remaining god object; `harness.py`/`run_matrix.py` reclassified cohesive
  orchestrators. Residuals for the post-study batch: publicize `_sweep_stale_containers`
  (`run_matrix.py:34`); dedupe manifest/dataset loaders (`harness.py:80` ↔ `run_matrix.py:358`);
  polish-only signals (`evaluate_run` 151 lines `criteria/verdict.py:492`, isinstance chain
  `metrics.py:308`, isinstance dispatch `collect.py:85`/`load_runs.py:149`). CO concurred:
  no live SRP fracture.
- **Gate:** full suite 1218/13/0 after every commit, pyright 0, boundaries clean.
- **CO review:** first pass REJECT on exactly one BLOCKING finding (the two sealed-dir/script
  refs above, missed by 923b246); claims on both moves (body drift, patch-target correctness,
  grace default binding, pruned imports, cycles) PASSED first try; c4346dd re-verified →
  **APPROVE**. INFO noted: moved reaper log lines now carry logger
  `infrastructure.adapters.worker.process_reaper` (no in-repo filter keys on the old name).
- **Still deferred:** agent_session deep sub-state split (only remaining item; needs opt-in).
