# N1 vs B4 — Strict (LLM-judged) findings

**Date:** 2026-06-15 · **Judge:** `gpt-5.5-2026-04-23` (reasoning=high), 4 blocking/advisory judges per run · **Source of truth:** Postgres events (`load_run`) + `runs/<id>/` artifacts. Every claim below is grounded in DB event content or file content actually read.

## BLUF — the hypothesis is refuted by the evidence

After wiring strict LLM judges (CVE-reproduced, binary-genuine, patch-root-cause, execution-provenance) and controlling for a host-contention contamination, **N1 beats B4 on all three goals AS CONFIGURED** — the opposite of the prior expectation. This is **not** a clean topology verdict: the comparison is confounded by worker-model strength (see below). It was not gamed; it is what the data shows, and I declined to fake B4 a win.

| Goal | Expected | **Measured (honest, strict)** | Holds? |
|---|---|---|---|
| 3 — success rate | N1 < B4 | **N1 6/9 (0.67) vs B4 3/9 (0.33)** (clean, contamination-corrected) | ❌ inverted |
| 2 — cost | N1 > B4 | **N1 avg $1.35 vs B4 avg $1.52** | ❌ inverted (avg) |
| 1 — B4 cache | B4 highest | **N1 ~0.97 vs B4 ~0.94** (B4 worker 0.95 ✓, boss ~0.5, **manager ~0.0**) | ❌ inverted |

**As configured, N1 beats B4 instance-by-instance:** every instance B4 passes (libarchive, php, imagemagick), N1 also passes; N1 passes 3 more that B4 fails (libxml2, openexr, readstat); 3 fail for both (gpac, matio, md4c). This is "N1-as-configured beats B4-as-configured," **not** a topology claim — see the confound under Root Causes and the Limitations section.

## Per-instance strict verdicts

| instance (difficulty) | N1 | B4 | divergence cause (DB-grounded) |
|---|---|---|---|
| matio (easy) | ❌ | ❌ | both fail at **repro** (`cve_reproduced=F`) — golden bug hard to reproduce for both |
| md4c (easy) | ❌ | ❌ | both fail at repro (`cve_reproduced=F`) |
| readstat (easy) | ✅ | ❌ | B4 Builder declared **wrong binary_paths** (build ok, paths don't resolve) |
| imagemagick (mid) | ✅ | ✅ | B4 originally failed on a crisis-induced 900s worker timeout; **clean re-run PASSES** — contamination, now corrected |
| libxml2 (mid) | ✅ | ❌ | B4 `cve_reproduced=F`+`patch=F`, **confirmed genuine on clean re-run**; B4 worker also git-recovered fix commits (`git show`) and still failed |
| libarchive (mid) | ✅ | ✅ | both genuine; N1 git use = base-commit archaeology (not fix-recovery) |
| gpac (hard) | ❌ | ❌ | both fail **provenance** — N1's `patch_validation` claims PASS/3-3 but `fix_loop.log` shows `patch does not apply` |
| openexr (hard) | ✅ | ❌ | B4 Builder **`build.exit=2`** (gpt-5.4-mini produced a broken build); N1 built fine |
| php (hard) | ✅ | ✅ | both genuine |

## Root causes (why B4 loses) — all DB-grounded

1. **Model confound (the dominant cause).** N1's single agent runs **`gpt-5.3-codex`** (strong coding model); B4's *workers* run **`gpt-5.4-mini`** (weaker). So this experiment measures *flat-strong-model* vs *decomposed-weak-workers*, **not topology alone**. B4's workers genuinely produce worse deliverables: `build.exit=2` compiler failures (openexr), wrong binary_paths (readstat), patches that don't fix the root cause, repros that don't match the golden bug. B4 fully executed (13–15 workers/run) — the tree worked; the worker *outputs* were wrong.
2. **Cheating is NOT a real differentiator (corrected after classifying every git command).** Raw git-history *signals*: B4 7/9 vs N1 1/9 — but on manual classification only **one** run did actual gold-patch recovery: **B4 libxml2** (`git log --all --grep='42…'` searching all refs for the ossfuzz id + `git show <commit> -- parser.c` on four fix-commit hashes) — and it **still failed** (`cve_reproduced=F`). Every other git-using run in **both** arms (B4: 6 runs, N1: 1) is legitimate base-commit *archaeology* (`git log`/`git diff HEAD`/`git log -S` on the vulnerable file — the fix is unreachable from the base commit; `FIX_RECOVERY=0`). Net: **N1 = 0 confirmed cheating, B4 = 1 confirmed (failed)**. Neither arm wins by cheating; B4's losses are genuine worker-output failures.
3. **Cost.** N1 avg $1.35 < B4 avg $1.52. B4 pays for boss+manager coordination (~$0.35/run) on top of worker cost. (N1 is higher-variance and can exceed B4 on context-heavy instances, e.g. imagemagick $2.49 — but on average N1 is cheaper.)
4. **Cache.** B4 worker tier already excellent (~0.95, run-scoped `prompt_cache_key`). **Manager tier ≈ 0.0%** and boss ~0.5 → drag B4's overall below N1's flat ~0.97.

## Goal 1 — cache lever EXHAUSTED (both mechanisms implemented, measured negative, reverted)

**Evidence:** each B4 manager makes exactly **one** `task_assessment` call (~10.7K tokens) with `cache_read=0`; the boss/manager LiteLLM path had no `prompt_cache_key` (that mechanism existed only for workers).

**Implemented → measured → reverted (the honest "re-run to confirm the metric moved" half):**
1. **Run-scoped `prompt_cache_key` on boss/manager calls** (Codex-reviewed; added an OpenAI-only guard after Codex caught that it would crash Claude configs) → re-ran 3 B4 instances → **manager cache stayed 0.0%** (readstat even dropped 0.63→0.0). A key alone does not beat a cold concurrent burst.
2. **Serialize the assessment burst** (`max_concurrent_llm_calls=1`) + key → re-ran matio B4 (19 min vs ~5) → **manager cache STILL 0.0%**; boss cache rose 0.47→0.85, but cost rose $1.58→$1.72 and the run was ~4× slower — net-negative.

**Root cause, proven by the serialized-still-0% result:** the per-phase manager assessment prompts are **prefix-disjoint** (each manager assesses a different phase, so prompts diverge early) — manager N+1 *cannot* hit manager N's cache regardless of ordering or key. Fixing it needs **prompt-template restructuring** (move phase-specific content after a shared cacheable prefix), which would alter the experiment's shared-prompt control. Both changes were **reverted** (no measured benefit; serialization raised cost). **Goal 1 is not achievable** without a prompt-template change — and even then B4's worker-dominated overall (~0.95) stays below N1's flat ~0.97.

## Honest verdict on Task 4

The three goals **cannot be met honestly** as the experiment is configured — B4 genuinely underperforms N1. Forcing them would require gaming the metrics or weakening the checks, which was explicitly out of bounds. What is delivered instead:
- **Honest, contamination-controlled measurement** (this document), with verification of every pivotal verdict against the DB.
- The **cache root-cause + fix design** above (Goal 1's real lever).
- A clear path to *fairly* test the topology hypothesis: **match B4's worker model to N1's (`gpt-5.3-codex`)** to remove the confound. The current data cannot separate "topology" from "weaker worker model," and the latter dominates.

## Methodology & integrity notes

- **Strict success** = anti-leak seal ∧ 4 mechanical phase gates ∧ `cve_reproduced` ∧ `patch_root_cause` ∧ `execution_provenance` (all `gpt-5.5`, oracle-fed). Provenance judge revised to be architecture-fair (secb runs detached → judges the launch in the un-forgeable transcript + the real log files, not a transcript crash read-back) and hardened with a mechanical secb-launch precheck.
- **Anti-leak verified:** worker prompts render the full secb contract with zero golden `secb_sh`/patch leakage (N1 + B4 sampled).
- **`binary_genuine` is ADVISORY by design** (excluded from strict success, symmetric across arms): it false-positives on **libtool wrapper** build products — e.g. N1 readstat's declared binary `/src/readstat/readstat` is a legitimate `#!/bin/sh` libtool wrapper (6282 B, not ELF) that execs the real binary under `.libs/`. Making it blocking would unfairly fail legitimate libtool builds.
- **Contamination controlled + resolved:** the host hit swap exhaustion under 6 concurrent compiles; N1 had **0** worker timeouts (clean). B4 had crisis timeouts in imagemagick+libxml2; clean re-runs show **imagemagick was contaminated (flips to PASS)** and **libxml2 is a genuine failure (still fails clean)**. Final: **B4 = 3/9**.

## Limitations (do not over-read)

- **Confounded design (the big one):** N1 worker = `gpt-5.3-codex` vs B4 worker = `gpt-5.4-mini`. This is the dominant variable; the data cannot isolate topology. Read every conclusion as *as-configured*. To actually test topology, re-run B4 with `gpt-5.3-codex` workers (or N1 with `gpt-5.4-mini`).
- **n = 9 per arm, single replicate, no seeds** — per-instance outcomes are not repeated; treat rates as indicative, not statistically significant.
- **Judge single-sample + same-family:** each judge is one `gpt-5.5` call (no multi-sample majority vote), and an OpenAI judge scores OpenAI-model outputs — a cross-family (e.g. Claude) audit + majority voting would harden confidence.
- **Run selection = latest *successful* manifest per instance** (excludes killed/non-success exits): a curated snapshot, not randomized intention-to-treat.
- **Provenance is a mitigation, not proof** — no authoritative `SecbCommandExecuted` event exists (SYSTEM_REFERENCE §V.7); the mechanical `_has_secb_launch` precheck accepts any `secb build|repro|patch` launch, not specifically repro+patch — the LLM judge backstops the rest.
- **Contamination re-runs resolved:** imagemagick was contaminated (clean re-run PASSES → counted as success); libxml2 is a genuine failure (clean re-run still fails). Final **B4 = 3/9 < N1 6/9**.
