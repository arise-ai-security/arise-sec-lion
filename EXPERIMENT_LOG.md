# SEC-Bench Experiment Log

> Append-only shared context file. Every subagent reads this at start, appends at end.
> Main conversation applies fixes between phases.

---

## Phase 0: Infrastructure Pre-Flight

**Status**: COMPLETE
**Started**: 2026-03-29

### Checklist

- [x] PostgreSQL up (`docker compose -f deployment/docker-compose.yml --profile local up -d`)
- [x] SEC-bench eval images pulled (per-CVE, NOT a single base image)
  - `hwiwonlee/secb.eval.x86_64.njs.cve-2022-32414` — PULLED
  - `hwiwonlee/secb.eval.x86_64.gpac.cve-2023-5586` — PULLED
- [x] `.env` has required keys — `POSTGRES_PASSWORD` + `OPENAI_API_KEY` present in `deployment/.env`
- [x] DB connectivity confirmed (`python main.py list` — returns "No BOSS runs found.")
- [x] CVE JSON files present — `njs-cve-2022-32414.json`, `gpac-cve-2023-5586.json`, `test_instance.json`

### Results

- Docker daemon: OK (v29.1.3, Compose v2.40.3)
- PostgreSQL: STOPPED (defined in docker-compose.yml under `local` profile, needs to be started)
- SEC-bench images: 2/2 eval images pulled successfully
- Env keys: `POSTGRES_PASSWORD` present, `OPENAI_API_KEY` present, `ANTHROPIC_API_KEY` commented out
- CVE JSON files: 3 found (2 real instances + 1 test fixture)
- Python: OK (main.py exists, key packages importable)
- Config alignment: All 18 baseline settings match experiment plan

### Issues Found

- ~~BLOCKER: PostgreSQL not running~~ — RESOLVED: containers started, DB healthy
- **CORRECTED**: Experiment plan previously referenced `hwiwonlee/secb.base:latest` as the only image needed. SEC-Bench actually uses per-vulnerability eval images (`hwiwonlee/secb.eval.x86_64.<project>.<cve-id>`). Plan updated.
- **NOTE**: `POSTGRES_HOST=db` in `deployment/.env` is the Docker-internal hostname. When running `main.py` from the host, override with `POSTGRES_HOST=localhost`. Port 5432 is mapped.
- WARNING: `ANTHROPIC_API_KEY` commented out — non-blocking since config uses OpenAI models
- WARNING: `test_instance.json` should be excluded from Phase 5 batch runs

---

## Phase 1: Smoke Runs (NJS CVE-2022-32414)

**Status**: COMPLETE — 4 iterations, all failed. Led to significant code changes.

### Run History

| Run | BOSS ID | Agents | Cost | Outcome | Root Cause |
|-----|---------|--------|------|---------|------------|
| v1 | `9f35420e` | 13 | $4.90 | FAILED | Concreteness validator rejected exploiter; verification judge cascade |
| v2 | `03a21736` | 4 | ~$0.01 | FAILED | Concreteness validator still blocking (assess.j2 changed but validator still active) |
| v3 | `d7bdefde` | 13 | $4.37 | FAILED | Workers executed but verification judge cascade (retry not wired in bootstrap) |
| v4 | `2f7902bf` | 13 | ~$4+ | FAILED | Verification judge cascade (user was making code changes during run) |

### Key Findings Across All Runs

1. **BOSS decomposition works** — consistently produces 3 phases (Builder, Exploiter, Fixer) with correct `depends_on` edges
2. **Workers reach execution** (v3/v4) — once validators were removed, workers built the project, found commits, ran builds inside SEC-bench containers
3. **Verification judge is the remaining blocker** — `VerificationFailed` on completed workers cascades to kill entire subtree. Auto-healing retry exists but was not wired through bootstrap in v3/v4.
4. **`pending` role in AgentCreated is NOT a bug** — event sourcing stores initial role; `ComplexityEvaluated` event transitions it. This is by design.
5. **Cost dominated by worker OpenHands sessions** (~$0.70-0.88 each), not orchestration LLM calls (~$0.004 total)

### Issues That Were Fixed

| Issue | Fix Applied |
|-------|------------|
| Concreteness validator rejecting domain-overridden assessments | Removed `validate_decomposition_quality()` from parse paths |
| assess.j2 enforcing "exactly 2 subtasks" | Replaced with domain reference material; LLM decides freely |
| Topology too tight (`max_children: 3`, `max_total: 15`) | Loosened to `max_children: 5`, `max_total: 25` |
| Denied command patterns blocking legitimate operations | Removed from config (to be re-evaluated in Phase 4) |

### Remaining Issue: Verification Judge Cascade

The verification judge (`VerificationFailed`) remains the primary failure mode. Workers complete their tasks but the LLM judge rejects on subjective criteria, triggering cascading failure up the tree. Auto-healing retry config exists (`model_escalation_chain: ["openai/gpt-4-turbo"]`) but needs verified wiring through bootstrap.

---

## Phase 2: Failure Analysis & Fixes

**Status**: COMPLETE — significant refactoring done by user

### Design Changes Applied

The user applied a broader refactoring beyond the initial targeted fixes:

| Change | What | Why |
|--------|------|-----|
| Removed quality validators | Deleted `validate_decomposition_quality()` calls from `parse_subtasks_from_llm()` and `parse_assessment_response()` | Heuristic validators fought LLM judgment; domain templates provide sufficient guidance |
| Freed LLM decomposition | assess.j2 now provides reference material, not hard constraints | LLM decides subtask count and structure based on task complexity |
| Loosened topology | `max_children: 3→5`, `max_total: 15→25` | Allow deeper/wider trees when LLM judges it necessary |
| Removed deny-list | `denied_command_patterns` removed from config.yaml | To be re-evaluated in Phase 4 |
| Enabled auto-healing retry | `model_escalation_chain: ["openai/gpt-4-turbo"]` | Failed workers get 1 retry with escalated model |

### Outstanding Issue

Auto-healing retry config exists in `config.yaml` but the **bootstrap wiring** needs verification — `ExecutionLimitsBridge` must pass `retry` config to `RetryPolicy`. Check `bootstrap/application.py` and `bootstrap/composition.py` before next run.

---

## Phase 3: GPAC Run (CVE-2023-5586)

**Status**: NOT STARTED

---

## Phase 4: OpenHands Deny-List

**Status**: NOT STARTED

---

## Phase 5: Batch Run (5+ Instances)

**Status**: NOT STARTED

### Results

| Instance | Builder | Exploiter | Fixer | E2E | Cost | Agents | Duration |
|----------|---------|-----------|-------|-----|------|--------|----------|

---

## Phase 6: Hypothesis Validation

**Status**: NOT STARTED
