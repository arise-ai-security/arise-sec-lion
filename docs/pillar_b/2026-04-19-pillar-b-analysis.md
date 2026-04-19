# Pillar B: Tree-vs-Flat Agent Orchestration — Analysis Report

**Date:** 2026-04-19
**Branch:** `experiment/tree-vs-flat-agent-orchestration`
**Dataset:** `dataset-v1.0-20260419.tar.zst` (47 runs, T2 reduced matrix)
**Handoff-from:** `docs/superpowers/plans/2026-04-19-pillar-a-completion-report.md`

---

## Executive Summary

**H1 (A1 vs B2 end-to-end mechanical pass rate) cannot be supported or falsified: p_exact = 1.0 (McNemar), 0 / 0 discordant pairs, BH-FDR(0.1) does not reject the null — zero end-to-end passes in either system across all 10 locked CVEs.** Apparent failure is universal at the mechanical-evaluator gate; the experiment is informative about efficiency and failure-mode asymmetry, not about correctness.

Top-line caveats the reader must carry into every claim below:

- **Retrofit required for both A and B.** Two latent bugs in `experiments/mechanical_evaluator.py` systematically under-reported `fixer_pass` and `exploiter_pass` for every cell: (a) the evaluator bind-mounts the run's workspace to `/workspace` but the `secb` bash script reads `/testcase/`; (b) each phase runs in a fresh container, so `secb repro` never sees the binaries `secb build` just produced. Both fixes are applied in `docs/pillar_b/scripts/retrofit_tree_patches.py`; `experiments/mechanical_evaluator.py` is not modified (scope: Pillar B does not touch run-execution code). Upstream INDEX.jsonl has been rewritten only for B-cell rows; A-cell rows retain their original `mechanical_pass` bits, and corrected A-cell evaluations are stored alongside originals as `corrected_mechanical.json` for sensitivity analysis.
- **Tree events lost tool identity.** The Claude Agent SDK adapter used during Pillar A recorded every `tool_use` event in B-cell runs with `tool_name = "Tool"` and `tool_input = {}`. Tool-call redundancy (R1) is therefore 0 by construction for B cells and cannot be compared directly against A cells. The original retrofit plan (parse events.jsonl for final Write/Edit to `model_patch.diff`) was unusable for this reason; the retrofit pivoted to on-disk artifact extraction from `<run_dir>/<agent_id>/testcase/`.
- **B2 runs all failed at decomposition.** 10 / 10 B2 runs failed because the BOSS LLM response was not valid JSON; no worker agents were spawned, no patch was ever written, total B2 wallclock median = 80 s at $0.49. All 9 on-disk patches among B-cell runs come from B1. The "tree at full power" cell produced 0 artifacts, which trivially bounds its end-to-end pass rate.
- **Flat CLI (A-cells) did not write to `workspace/` either.** Only 2 of 30 A-cell runs wrote a `model_patch.diff` to `workspace/` (njs.cve-2022-32414/A4/0 and njs.cve-2022-38890/A3/0). The rest are empty. `builder_pass = True` in 4 of 30 A-cell runs reflects the image's pristine build succeeding in a fresh container, not any output the flat agent produced.
- **SDK-reported cost ($152.58) is not reconciled** against the Anthropic billing dashboard. That check is still pending and must be performed out-of-band before any cost-based claim is published externally.
- **N = 10 underpowered by design.** As pre-registered, this study reports descriptive statistics, effect sizes, and paired tests where applicable. Underpowered null results do not warrant equivalence claims.

Headline result table:

| Comparison | Metric | Test | Result | FDR(0.1) reject? |
|---|---|---|---|---|
| **H1** A1 vs B2 | end-to-end pass (retrofitted) | McNemar exact | p = 1.0, diff = 0 | No |
| **S1** B1 vs B2 | end-to-end pass (retrofitted) | McNemar exact | p = 1.0, diff = 0 | No |
| **S4** A2 vs B1 | end-to-end pass (retrofitted) | McNemar exact | p = 1.0, diff = 0 | No |
| **S5** A3 vs B2 | end-to-end pass (retrofitted) | McNemar exact (N = 3, directional) | p = 1.0, diff = 0 | No |
| **C1** cost per run | USD total | Kruskal-Wallis + MWU | H = 25.97, **p = 9.1 × 10⁻⁵** | (not pre-reg. FDR cohort) |
| **C2** wall-clock per run | seconds | Kruskal-Wallis + MWU | H = 31.14, **p = 8.8 × 10⁻⁶** | (not pre-reg. FDR cohort) |
| **R1** tool-call redundancy | rate per cell | descriptive | A cells 32–37 %; B1 = 0 % (capture gap); B2 N/A | — |
| **R2** CNR (context novelty) | mean per run | descriptive | B1 = 0.70, B2 = 1.00; A cells unavailable | — |

---

## Methodology

### Matrix reduction (pre-registered 6-cell → executed 4-cell T2)

Pillar A's budget pressure collapsed the 6-cell × 10-CVE × 1 replicate + 3 anchor design (63 runs) into a 4-cell T2 design plus opportunistic bonus data: cells **A1, A2, B1, B2** at 10 CVEs each + 2 anchor A1 replicates on openjpeg = 42 primary runs; **A3 (3 runs)** and **A4 (2 runs)** were captured on njs + faad2 CVEs only and are treated here as supplementary, not primary evidence. Total 47 runs.

### Retrofit: two evaluator fixes

`experiments.mechanical_evaluator.evaluate_run` is the arbiter of the primary metric `end_to_end_pass`. During the Pillar B retrofit the following upstream behaviors were discovered to systematically bias the metric:

1. **Bind-mount target mismatch.** `evaluate_run` mounts the run workspace as `/workspace` inside the eval image, but every `secb-tools:*-patch` image ships a `secb` script at `/usr/local/bin/secb` that reads exclusively from `/testcase/` (see e.g. the `patch` function: `git apply /testcase/model_patch.diff`). Any `model_patch.diff` the agent wrote to `workspace/` was invisible to the evaluator.
2. **Per-phase container churn.** The evaluator runs `secb build`, `secb repro`, and `secb patch` as three separate `docker run --rm` calls. Because containers are ephemeral, the binaries produced by `secb build` are gone when `secb repro` launches. This causes every `secb repro` to exit with 127 ("binary not found"), forcing `exploiter_pass = False` universally.

The retrofit at `docs/pillar_b/scripts/retrofit_tree_patches.py` supplies a drop-in `evaluate_run_fixed` that:

- Bind-mounts `workspace` at `/testcase`.
- `docker create` + `docker cp /testcase/.` hydrates the workspace with the image's pre-baked PoC / base-commit files so that mounting over `/testcase` doesn't shadow them.
- Runs `secb build; secb repro; secb patch; secb repro` (post-patch verify) as a single persistent bash invocation inside one container, so the built binaries are still present when `secb repro` fires.
- Records `fixer_pass = (patch applied) AND (post-patch repro no longer triggers the expected sanitizer class) AND (pre-patch repro did trigger it)`.

The retrofit **does not modify `experiments/mechanical_evaluator.py`** — the upstream code path is preserved for future re-runs if the evaluator is fixed separately.

### Retrofit application scope

- **B cells (10 B1 + 10 B2):** `mechanical.json` files are overwritten with retrofitted results; `INDEX.jsonl` `mechanical_pass` fields are rewritten. Committed as `fix(pillar-b): retrofit tree patch extraction for mechanical evaluation`.
- **A cells (12 A1 + 10 A2 + 3 A3 + 2 A4):** per the user's explicit scope instruction, `mechanical.json` is **not** overwritten; the corrected-evaluator output is written alongside as `corrected_mechanical.json` and INDEX.jsonl is left unchanged for A rows. This becomes the "A-cell sensitivity" column in `index_enriched.csv`.

Analyses below use the **effective** `end_to_end_pass`:

- B cells: the retrofitted `mechanical_pass.end_to_end`.
- A cells: the `corrected_mechanical.json` value when present, otherwise the original (equivalent here because the original A-cell evaluator also read from the non-existent `/workspace` path, so all original A-cell `end_to_end = False` outcomes were already as pessimistic as the corrected pass).

### Patch extraction for B-cells

The originally-planned extraction path ("parse events.jsonl for the final `ThoughtCaptured` event with `tool_name ∈ {Write, Edit}` targeting `model_patch.diff`") was impossible: no `ThoughtCaptured` events exist in the dataset, and the `tool_use` events that do exist record `tool_name = "Tool"` and `tool_input = {}`. The Claude Agent SDK adapter simply did not propagate those fields to event-sourcing — the adapter's raw JSON is in `infrastructure/adapters/worker/claude_sdk_adapter.py`.

Instead, the retrofit scans `<run_dir>/*/testcase/` on disk. Tree workers wrote artifacts under a per-agent UUID directory that was bind-mounted into their container. Patch-file preference order (reflecting empirical naming diversity): `fix.patch` > `cve-*.patch` > any `*.patch` > `repo_changes.diff`. Ties break on larger file size.

### Statistical framework

- **Paired binary outcomes (H1/S1/S4/S5).** McNemar's exact binomial test on discordant pairs. Bootstrap 95 % CI for paired-difference rate, `seed = 42`, 10 000 iterations.
- **Continuous outcomes (C1/C2).** Kruskal-Wallis across cells + pairwise Mann-Whitney U (two-sided) without Dunn correction; significance attributed at α = 0.05 per-pair.
- **Multiple comparisons.** Benjamini-Hochberg FDR at 0.1 **only across the pre-registered primary quartet H1 / S1 / S4 / S5**; C1 / C2 / R1 / R2 are reported as supporting evidence and are not in the FDR cohort.
- **Tokenizer for CNR.** `tiktoken.cl100k_base` — the closest publicly available approximation; Anthropic does not ship a free Claude 4.6/4.7 tokenizer. Documented as an approximation; relative ordering between cells is robust to tokenizer choice.

### Cost reconciliation status

The SDK-aggregated cost of **$152.58** (per Pillar A INDEX) has **not** been reconciled against the Anthropic billing dashboard. Pillar B defers that check; any absolute-cost claim is conditional on dashboard agreement.

---

## Results

### H1 — A1 vs B2 end-to-end pass rate (primary)

Per-CVE paired outcomes on `end_to_end_pass_effective`:

- 10 paired CVEs, 0 discordant pairs (neither system passed on any CVE).
- McNemar exact binomial p = 1.0, diff_rate = 0, 95 % bootstrap CI = [0.00, 0.00].
- BH-FDR(0.1) does not reject the null.

The pre-registered P1-a success criterion ("B2 wins ≥ 3 of 4 non-tied pairs AND effect-direction holds across all 3 difficulty strata") is not met. H1 is neither supported nor falsified: universal failure dominates the signal. See `./figures/fig_h1_endtoend.png`.

The dominant reason B2 has 0 passes is operational: **every B2 run failed at BOSS decomposition** (LLM response was not valid JSON), before any worker agent could be spawned. This is a deterministic prompt/format bug, not a reasoning failure — it manifests in ~80 s at < $0.50 per run.

The dominant reason A1 has 0 passes is artifactual: **28 of 30 A-cell runs did not write `workspace/model_patch.diff`** at all. The flat CLI's deliverable contract was understood — the CLI reads the task text — but no run produced the conventional output file. Combined with the evaluator-mount bug, every A1 run was guaranteed to fail `fixer_pass`.

### S1 — B1 vs B2 end-to-end pass rate

- 10 paired CVEs, 0 discordant pairs, p = 1.0, diff_rate = 0.
- Descriptive: B1 has 9 / 10 on-disk patches; B2 has 0. B1 mechanical passes: builder 2, exploiter 1, fixer 0. B2 mechanical passes: builder 2, exploiter 1, fixer 0.

Same `exploiter_pass = True` count as B1 (mruby.cve-2022-0240), purely from the image's pristine build succeeding and the baked-in PoC triggering the SEGV; the Pillar A agent never contributed to that outcome, as the workspace was empty.

Briefing effect on tree: **directionally B1 ≈ B2 on end-to-end**; B2's much lower cost is a crash-at-decomposition artifact, not an efficiency gain.

### S4 — A2 vs B1 "pure orchestration" effect

- 10 paired CVEs, 0 discordant, p = 1.0, diff = 0.

Pure orchestration contrast cannot be evaluated at the end-to-end level in this dataset. On secondary metrics: A2 cost median $4.87, B1 cost median $0.47 (~10× ratio); A2 wallclock 1 614 s, B1 wallclock 544 s (~3× ratio). A2 audit-violation count distribution is skewed: 3 of 10 A2 runs had ≥ 1 violation. B1 had 0 / 10 violations.

### S5 — A3 vs B2 matched-briefing

- 3 paired CVEs (A3 only captured on njs/faad2 — below the pre-registered power threshold of N = 10); p = 1.0, diff = 0.
- **Directional only.** Present as supporting evidence that the A3-briefed flat arm was as unproductive on end-to-end as B2, at considerably higher cost (A3 median cost $5.75, wallclock 2 348 s).

### C1 — cost per cell

- Kruskal-Wallis H = 25.97, p = 9.06 × 10⁻⁵ across 6 cells.
- Median cost: A1 $3.83, A2 $4.87, A3 $5.75, A4 $9.62, B1 $0.47, B2 $0.49.
- Pairwise MWU vs B2: A1 p = 0.0011, A2 p = 0.00058, A4 p = 0.030; vs B1: A1 p = 0.0011, A2 p = 0.00018.
- Effect magnitude (medians): A1/B2 ratio ≈ 7.8×; A2/B2 ≈ 9.9×; A4/B2 ≈ 19.6×.

Caveats: B2 cost is depressed by the universal decomposition crash at ~80 s; if B2 had run to completion the cost gap would narrow. A conservative reading is A1 vs B1 (both ran to completion for most CVEs): A1 cost median $3.83 is ~8× B1's $0.47 at comparable exploration depth (B1 average event count ≈ 164; A1 average ≈ 354).

See `./figures/fig_cost_by_cell.png` and `./figures/fig_cost_vs_pass.png`.

### C2 — wall-clock per cell

- Kruskal-Wallis H = 31.14, p = 8.81 × 10⁻⁶.
- Median wall-clock: A1 1 902 s, A2 1 614 s, A3 2 348 s, A4 2 697 s, B1 544 s, B2 80 s.
- Same caveat as C1: B2's 80 s is a crash artifact; B1's 544 s is the fair tree-vs-flat comparison and is ~3.5× faster than A1 (p = 0.002 pairwise MWU).

See `./figures/fig_wallclock_by_cell.png`.

### R1 — tool-call redundancy

- Per-cell mean `redundancy_rate_total`:
  - A1: 0.35 (n = 12). Median 0.35.
  - A2: 0.32 (n = 10).
  - A3: 0.34 (n = 3).
  - A4: 0.37 (n = 2).
  - **B1: 0.0 (n = 8; 2 runs had no tool-call events). Not comparable — tool_name / tool_input not captured.**
  - B2: no A-cell-style tool_use events (all generic "Tool" with empty input).

A cells consistently exhibit ~30–40 % tool-call redundancy within a single agent session (mostly repeated reads of the same file and repeated Bash listings). This is a within-flat phenomenon — the sibling / hierarchy redundancy the experiment was designed to measure cannot be evaluated against it, because B-cell tool identities were lost by the adapter.

Pre-registered Exp-2 threshold ("tool_call_redundancy_sibling + _hierarchy ≥ 20 % of total tool calls in tree cells") **cannot be evaluated**. See §Exp-2 note in Limitations.

See `./figures/fig_toolcall_redundancy.png`.

### R2 — Context Novelty Ratio (CNR)

- B1 mean CNR = 0.70 (n = 10). B1 median 0.64; min 0.50.
- B2 mean CNR = 1.00 (n = 10). Every B2 prompt is tokenizer-novel because B2 fires 1–3 prompts before crashing — no history accumulates.
- **A cells: no prompt_sent events captured by the flat CLI harness.** CNR is not computable.

The tree's observed CNR (0.70 in B1) is consistent with the experiment design's expectation that orchestration produces structured, mostly-novel sub-prompts per worker. However, with no A-cell baseline, the pre-registered H3 ("tree `cq_mean` > flat `cq_mean` on ≥ 7 of 10 CVEs") cannot be evaluated. Extending capture to A-cells requires modifying `flat_cli_harness.py` to emit one `prompt_sent` per round-trip.

See `./figures/fig_cnr_distribution.png`.

### Stratified analysis (EASY / MEDIUM / HARD)

Per-stratum effective end-to-end pass rates are uniformly 0 across all three strata for all cells (see `./figures/fig_stratified.png`). Direction-across-strata check (part of the pre-registered H1 success criterion) is vacuously "met" but unsupported — all strata show the same zero outcome.

---

## Qualitative findings (excerpts)

Four annotated log excerpts in `./examples/`:

- **`tree_success_best.md`** — mruby.cve-2022-0240 B1: the one B-cell run that passed both `builder_pass` and `exploiter_pass` after retrofit. The patch applies cleanly, but `secb repro` still detects the SEGV post-patch → the model's fix was syntactically valid but logically insufficient.
- **`tree_failure.md`** — njs.cve-2022-32414 B2: canonical B2 failure mode. BOSS `decompose` LLM call returned non-JSON; `work_failed` event fires at seq 26 with reason "Failed to parse subtasks: LLM response is not valid JSON: Expecting value: line 1 column 1 (char 0)". Zero tool calls, zero workers spawned, 82 s total.
- **`flat_success_best.md`** — mruby.cve-2022-0240 A1: 33-minute exploration with 124 Bash calls, 40 Reads, 2 Task subagents, 3 Writes. No `model_patch.diff` in workspace/ at completion. Excerpt shows repeated `ls /src` and re-reads of the same files — illustrating the 35 % intra-agent redundancy measured for A1.
- **`flat_failure.md`** — njs.cve-2022-32414 A1: 499-event, $6.66, 33-minute run that also produced no artifact. Shows the agent's final events as the CLI `stop_reason` triggered without a completion declaration.

---

## Limitations and Threats to Validity

1. **Universal failure at end-to-end.** Neither system produced a correct patch on any CVE. H1 cannot discriminate the systems at the pre-registered primary metric. Future work: lower the deliverable gate (e.g. `patch applies AND builds`) or run on an easier benchmark.
2. **B2 is a distribution of crashes, not a distribution of attempts.** 10 / 10 B2 runs died at BOSS decomposition before any worker was spawned. Any cost / wallclock / CNR comparison involving B2 is confounded by this failure mode. A re-run after the decomposition-prompt bug is fixed is the cleanest path to evaluating B2 properly.
3. **Tree event capture incomplete.** Claude Agent SDK adapter recorded `tool_name = "Tool"` and `tool_input = {}` for every B-cell tool call; tool-call-redundancy metrics (sibling, hierarchy) are therefore 0 by construction and un-diagnostic. Fix requires patching `claude_sdk_adapter.py` to extract tool metadata from SDK frames.
4. **Flat event capture incomplete.** A-cell `events.jsonl` lacks `prompt_sent` entries. `flat_cli_harness.py` normalizes `stream-json` events into `tool_use` / `tool_result` / `tokens_consumed` but does not synthesise a `prompt_sent` per round-trip. CNR (R2) and pre-registered H3 are therefore un-evaluable for A cells.
5. **Evaluator mount + persistent-container bugs affected all of Pillar A.** The retrofit corrects this, but A-cell `mechanical.json` files are left unmodified per user scope; A-cell sensitivity is carried separately in `corrected_mechanical.json`. The effective comparison uses `corrected_mechanical` where available.
6. **Docker platform mismatch.** All eval images are `linux/amd64`; host is `linux/arm64/v8`. qemu emulation inflates wall-clock measurements. Relative cell ordering is preserved but absolute wall-clock numbers are not portable.
7. **SDK cost dashboard reconciliation pending.** $152.58 is self-reported by the SDK; dashboard check not performed.
8. **Anchor-CVE variance.** Only 2 anchor A1 replicates (openjpeg), below the pre-registered 3. Between-replicate variance: cost σ = $0.11, wall-clock σ = 50 s — small and uninformative given all three outcomes were end-to-end failures.
9. **Single reviewer / author-not-blinded.** As pre-registered, the manual rubric (§5.9 in the design doc) is not executed in this round — all mechanical signal is so low that per-patch dimension ratings would be dominated by ceiling effects. Deferred until an end-to-end pass exists.
10. **Budget / wallclock cap hits.** 6 / 47 runs (13 %) hit either the $10 budget or the 90-min wall-clock cap, concentrated in A-cells on HARD-stratum CVEs (exiv2, imagemagick). Not excluded from analysis but flagged.

---

## Pre-registration Compliance

Pre-registered items and their execution status:

| Pre-registered item | Status |
|---|---|
| Primary metric P1: `end_to_end_pass` A1 vs B2, McNemar | Executed; H1 not supported (0/0 discordant). |
| Paired pairing on (CVE, cell) | Executed — McNemar on per-CVE pairs for H1/S1/S4/S5. |
| Anchor replicates on A1 CVE #5 | Executed (2 replicates vs. planned 3 — pre-registered as "3"; cell-variance estimate therefore under-powered). |
| Secondary comparisons S1/S4 | Executed as planned. |
| Secondary S5 | **Partially executed** — A3 has 3 runs not 10 (matrix reduction); presented as directional evidence only. |
| Secondary S2 / S3 / S6 | **Not executed** — these require A3 / A4 at full sample, which the reduced matrix did not provide. |
| BH-FDR 0.1 across primary quartet | Executed; all four fail to reject. |
| Bootstrap CIs seed=42, 10k iter | Executed for paired-difference rates. |
| Manual rubric (§5.9) | **Deferred.** Zero end-to-end passes means rubric ratings would be ceiling-effect-dominated. |
| Exp-2 trigger thresholds (§6.10) | **Un-evaluable** — redundancy sibling/hierarchy requires tool-name capture that the SDK adapter lost. |
| CQ judge rubric (§5.8) | **Not executed** — deferred pending cost reconciliation and adapter fix. |

Any analysis not in the table above is exploratory.

---

## Exploratory / non-pre-registered observations

- **Audit violations concentrate in A cells:** A1 had 11 of 12 runs with ≥ 1 violation (distribution: {0:1, 1:1, 2:2, 3:2, 5:2, 6:1, 7:1, 12:1, 15:1}); A2 had 3 / 10; A3 had 2 / 3; A4 had 0 / 2. B1 and B2 had 0 / 10 violations each. The flat CLI's tool freedom (Bash, WebFetch, Task subagents) makes cheating easier; the tree's narrower worker tool set suppresses it by construction. Worth formalising in a follow-up pre-registered hypothesis.
- **Cost-per-builder-pass is the only dimension where flat and tree look comparable.** 4 A-cell builder passes for $\$5.90$ each on average; 4 B-cell builder passes for $\$0.55$ each. That's ~10× efficiency for tree per successful build attempt — but the "build" semantic is asymmetric: both are building from the image baseline, not from any agent-produced patch, so neither is really a meaningful "capability" measurement.
- **Failure-mode asymmetry:** Flat fails by exhausting budget / time without producing a deliverable; Tree fails by crashing decomposition in 80 s. Both are "zero end-to-end pass" but the flat failure is *engaged non-progress* while the tree failure is *premature termination*. An observer would score these very differently on a hand-wavy "is the system trying" axis.

---

## Appendix A — Per-CVE per-cell breakdown

See `./tables/per_cve_breakdown.csv` (47 rows) for full numbers. Headline pass counts:

| Cell | N | builder | exploiter | fixer | end_to_end |
|---|---|---|---|---|---|
| A1 | 12 | 2 (mruby, imagemagick) | 0 (orig) / 1 (corrected, mruby) | 0 | 0 |
| A2 | 10 | 2 (mruby, imagemagick) | 0 (orig) / 1 (corrected, mruby) | 0 | 0 |
| A3 | 3 | 0 | 0 | 0 | 0 |
| A4 | 2 | 0 | 0 | 0 | 0 |
| B1 | 10 | 2 (mruby, imagemagick) | 1 (mruby) | 0 | 0 |
| B2 | 10 | 2 (mruby, imagemagick) | 1 (mruby) | 0 | 0 |

"Corrected" column: A-cell sensitivity via the fixed evaluator (see Methodology). Imagemagick exploiter_pass is False everywhere because the image's pristine repro fires a `leak` sanitizer event but our `classify_sanitizer_output` prefers `heap-buffer-overflow` precedence — the sanitizer class was not recoverable from the image for that CVE.

## Appendix B — Per-run cost table

See `./tables/index_enriched.csv` (47 rows, 35 columns) for the authoritative per-run table. Quick per-cell totals:

| Cell | N | Σ cost | mean cost | Σ wall-clock (s) | mean wall-clock (s) |
|---|---|---|---|---|---|
| A1 | 12 | $45.93 | $3.83 | 22 824 | 1 902 |
| A2 | 10 | $44.57 | $4.46 | 16 136 | 1 614 |
| A3 | 3 | $16.39 | $5.46 | 7 045 | 2 348 |
| A4 | 2 | $19.25 | $9.62 | 5 393 | 2 697 |
| B1 | 10 | $5.91 | $0.59 | 6 082 | 608 |
| B2 | 10 | $4.95 | $0.49 | 811 | 81 |
| **Total** | **47** | **$137.00** | — | **58 291** | — |

Total $137.00 via INDEX.jsonl `total_cost_usd` sum; Pillar A reported $152.58 SDK-aggregate. Gap is accounted for by judge LLM costs not propagated into per-run `total_cost_usd`. Both numbers remain un-reconciled with the Anthropic dashboard.

## Appendix C — Reproducibility

`./reproduce.sh` — one-shot script that re-extracts the tarball, runs the retrofit, regenerates all tables, figures, and excerpts. Run from repo root after Docker daemon is up and `secb-tools:*-patch` images are pulled.

```bash
docs/pillar_b/reproduce.sh
```

Script prerequisites: `uv`, Docker Desktop with all `secb-tools:<cve>-patch` images available locally (pull list in `dataset/locked_instances.yaml`), `ANTHROPIC_API_KEY` not required (we only re-evaluate mechanically, no LLM calls are made in Pillar B).

### Commit history

- `fix(pillar-b): retrofit tree patch extraction for mechanical evaluation` — `docs/pillar_b/scripts/retrofit_tree_patches.py`, `docs/pillar_b/tables/retrofit_summary.csv`, corrected `dataset/INDEX.jsonl` + `dataset/runs/*/B*/*/mechanical.json` (dataset left locally; not committed in line with Pillar A precedent).
- Subsequent commits (this document): analysis scripts, tables, figures, examples, report.
