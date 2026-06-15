# N1 vs B4 — Strict (LLM-judged) findings

**Date:** 2026-06-15 · **Judge:** `gpt-5.5-2026-04-23` (reasoning=high), 4 blocking/advisory judges per run · **Source of truth:** Postgres events (`load_run`) + `runs/<id>/` artifacts. Every claim below is grounded in DB event content or file content actually read.

## BLUF — the hypothesis is refuted by the evidence

After wiring strict LLM judges (CVE-reproduced, binary-genuine, patch-root-cause, execution-provenance) and controlling for a host-contention contamination, **N1 (flat OpenHands) outperforms B4 (BOSS→MANAGER→WORKER tree) on all three goals** — the opposite of the prior expectation. This was not gamed; it is what the data shows, and I declined to fake B4 a win.

| Goal | Expected | **Measured (honest, strict)** | Holds? |
|---|---|---|---|
| 3 — success rate | N1 < B4 | **N1 6/9 (0.67) vs B4 2/9 (0.22)** | ❌ inverted |
| 2 — cost | N1 > B4 | **N1 avg $1.35 vs B4 avg $1.52** | ❌ inverted (avg) |
| 1 — B4 cache | B4 highest | **N1 ~0.97 vs B4 ~0.94** (B4 worker 0.95 ✓, boss ~0.5, **manager ~0.0**) | ❌ inverted |

**N1 strictly dominates instance-by-instance:** every instance B4 passes (libarchive, php), N1 passes too; N1 passes 4 more that B4 fails; 3 fail for both.

## Per-instance strict verdicts

| instance (difficulty) | N1 | B4 | divergence cause (DB-grounded) |
|---|---|---|---|
| matio (easy) | ❌ | ❌ | both fail at **repro** (`cve_reproduced=F`) — golden bug hard to reproduce for both |
| md4c (easy) | ❌ | ❌ | both fail at repro (`cve_reproduced=F`) |
| readstat (easy) | ✅ | ❌ | B4 Builder declared **wrong binary_paths** (build ok, paths don't resolve) |
| imagemagick (mid) | ✅ | ❌* | B4 `patch_root_cause=F` + a worker 900s timeout (*re-run pending) |
| libxml2 (mid) | ✅ | ❌* | B4 `cve_reproduced=F`+`patch=F`; **B4 worker CHEATED** (`git show` of fix commits) and still failed (*re-run pending) |
| libarchive (mid) | ✅ | ✅ | both genuine; N1 git use = base-commit archaeology (not fix-recovery) |
| gpac (hard) | ❌ | ❌ | both fail **provenance** — N1's `patch_validation` claims PASS/3-3 but `fix_loop.log` shows `patch does not apply` |
| openexr (hard) | ✅ | ❌ | B4 Builder **`build.exit=2`** (gpt-5.4-mini produced a broken build); N1 built fine |
| php (hard) | ✅ | ✅ | both genuine |

## Root causes (why B4 loses) — all DB-grounded

1. **Model confound (the dominant cause).** N1's single agent runs **`gpt-5.3-codex`** (strong coding model); B4's *workers* run **`gpt-5.4-mini`** (weaker). So this experiment measures *flat-strong-model* vs *decomposed-weak-workers*, **not topology alone**. B4's workers genuinely produce worse deliverables: `build.exit=2` compiler failures (openexr), wrong binary_paths (readstat), patches that don't fix the root cause, repros that don't match the golden bug. B4 fully executed (13–15 workers/run) — the tree worked; the worker *outputs* were wrong.
2. **B4 cheats MORE, not less.** git-history use: **B4 7/9 runs vs N1 1/9**. On inspection N1's one case (libarchive) is legitimate archaeology (`git log -S`/`git diff HEAD` on the vulnerable file — the fix is unreachable from the base commit). **B4 libxml2 is real gold-patch recovery**: `git log --all --grep='42…'` (searching all refs for the ossfuzz id) + `git show <commit> -- parser.c` on four fix-commit hashes — and it *still failed*. So strict judging + cheating detection does not rescue B4.
3. **Cost.** N1 avg $1.35 < B4 avg $1.52. B4 pays for boss+manager coordination (~$0.35/run) on top of worker cost. (N1 is higher-variance and can exceed B4 on context-heavy instances, e.g. imagemagick $2.49 — but on average N1 is cheaper.)
4. **Cache.** B4 worker tier already excellent (~0.95, run-scoped `prompt_cache_key`). **Manager tier ≈ 0.0%** and boss ~0.5 → drag B4's overall below N1's flat ~0.97.

## Goal 1 — the one legitimate lever (cache), with fix design

**Evidence:** each B4 manager makes exactly **one** `task_assessment` call (~10.7K tokens) with `cache_read=0`; the 3–4 managers share a large prefix but all miss. Root cause: manager assessments fire **concurrently** (`max_concurrent_llm_calls=8`), so none populates the OpenAI prefix cache before the others read it — and the boss/manager LiteLLM path has **no `prompt_cache_key`** (that mechanism exists only for workers, `openhands_adapter.py:499-517,617,745`).

**Fix design (Codex-reviewable; not yet implemented — needs a B4 re-run to validate):**
- Add `prompt_cache_key` pass-through to `LiteLLMAdapter.query*` → `litellm.completion(..., prompt_cache_key=...)`.
- Plumb a **run-scoped** key for boss/manager calls (mirror the worker `run_scoped_prompt_cache_key`), so concurrent same-prefix assessments route to one cache shard.
- Because concurrent first-calls still miss until one populates the cache, additionally **serialize the manager assessment burst** (or issue one warm-up call first). Expected manager cache 0% → ~0.6; overall B4 cache gains are modest (boss+manager are a small token share vs the worker tier).

## Honest verdict on Task 4

The three goals **cannot be met honestly** as the experiment is configured — B4 genuinely underperforms N1. Forcing them would require gaming the metrics or weakening the checks, which was explicitly out of bounds. What is delivered instead:
- **Honest, contamination-controlled measurement** (this document), with verification of every pivotal verdict against the DB.
- The **cache root-cause + fix design** above (Goal 1's real lever).
- A clear path to *fairly* test the topology hypothesis: **match B4's worker model to N1's (`gpt-5.3-codex`)** to remove the confound. The current data cannot separate "topology" from "weaker worker model," and the latter dominates.

## Methodology & integrity notes

- **Strict success** = anti-leak seal ∧ 4 mechanical phase gates ∧ `cve_reproduced` ∧ `patch_root_cause` ∧ `execution_provenance` (all `gpt-5.5`, oracle-fed). Provenance judge revised to be architecture-fair (secb runs detached → judges the launch in the un-forgeable transcript + the real log files, not a transcript crash read-back) and hardened with a mechanical secb-launch precheck.
- **Anti-leak verified:** worker prompts render the full secb contract with zero golden `secb_sh`/patch leakage (N1 + B4 sampled).
- **Contamination controlled:** the host hit swap exhaustion under 6 concurrent compiles; N1 had **0** worker timeouts (clean), B4 had timeouts in imagemagick+libxml2 (being re-run cleanly — even if both flip, B4 = 4/9 < 6/9).
