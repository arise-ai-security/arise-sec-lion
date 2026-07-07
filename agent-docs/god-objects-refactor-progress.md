# God-objects refactor — progress ledger (handoff file)

> **What this is.** The live state of the behavior-preserving god-object decomposition sweep
> (`/software-quality:full-refactor`). The worklist lives in
> `agent-docs/god-objects-refactor-plan.md`; THIS file is the progress ledger. A fresh session
> must read this file FIRST, trust the ✅ rows, and resume at the first ⬜.
> **Update after every committed target and at each phase boundary.**

## ✅ SWEEP COMPLETE (2026-07-07) — all 13 ranked targets + E1–E4 decomposed, reviewed, committed

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
**Phases 1–4 are NOT started — awaiting user sign-off on the plan (pre-implementation gate).**

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
| 13 | `bootstrap/application.py` → private sub-factories (OPTIONAL) | B | 4 | ⬜ (optional) | — |
| E1 | `experiments/shared/evaluation/criteria.py` → `criteria/` package (metrics/verdict/judge_prompts/_shared + shim; 9 privates re-exported) | A | 3 | ✅ committed — CO REJECT overruled (transitive-reexport non-issue); verified 3 ways: AST-identical evaluate_run/metrics, 115+57 tests green, every real consumer resolves | 4568e66 |
| E2 | `experiments/shared/harness.py` → `subprocess_runner.py` | B | 3 | ✅ committed (with E3/E4 plumbing) | 428ee5c |
| E3 | `experiments/shared/scripts/run_matrix.py` → shared `container_cleanup.py` (dedups harness+run_matrix docker sweep) | B | 3 | ✅ committed | 428ee5c |
| E4 | `experiments/shared/evaluation/common.py` → `tool_categorization.py` | A | 3 | ✅ committed | 428ee5c |
| — | events.py, postgres_event_store, subtask_parser, prompt_builder, tool_calling_service, verification_pipeline, procedures.py, prompt_strategy.py, plugin.py, schemas.py, shared_context.py, presentation/*, bootstrap.py, recon/openrouter adapters, worker/shared/* | — | — | 🚫 leave-alone (reasons in plan) | — |

Legend: ✅ done (+SHA) · ⬜ todo · 🔶 in-progress (+what's left) · ⏭️ deferred (+why) · 🚫 leave-alone

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
  path + OCC invariants → deferred beyond this sweep unless the user opts in.
- **openhands_adapter process-reaper extraction**: OS/waitpid/timing-sensitive; do LAST within
  target #3 or skip — decide at target design time.
- **openrouter_adapter ↔ litellm_adapter DRY consolidation**: both cohesive individually;
  a shared LLM-adapter base is a `python-boilerplate` pass, not a god-object fix. Not scheduled.
- **experiments/shared targets (E1–E4)**: blocked until the b4/n1 studies land; re-audit then.
- **Swallowed-exception bug fixes + dead-code removal** (plan §out-of-scope): behavior-changing;
  needs its own pass with user approval — NOT part of the behavior-preserving sweep.
