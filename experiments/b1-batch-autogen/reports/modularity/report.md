# Subtree Modularization — b1-batch-autogen

**Runs analyzed:** 81 (one per CVE, success-only primary). Source of truth: Postgres `events` (DB-grounded).

## Data provenance & correctness
- Membership: manifest `study_id == 'b1-batch-autogen'`; dir==manifest `run_id` triangulated (0 mismatches); events-root triangulated.
- De-duplication: 124 runs → 112 unique CVEs (success>timeout>failed, latest); multi-success anomalies: 0.
- Extractor validation (vs on-disk diffs): Fixer patch-file recall 0.987; shell-mediated build writes under-captured (documented limitation).

## Methodology (brief)
- **Source of truth:** Postgres `events`; one normalized model per run (nodes, messages, producer→consumer dataflow, shared-store writes).
- **Modules:** the depth-1 `[Builder]/[Exploiter]/[Fixer]/[Reporter]` branch of each node; the Boss is the composition root, not a module.
- **Interaction graph / DSM:** undirected weight = persisted messages (briefing/report) + producer→consumer file dataflow; the DSM renders this as a node×node matrix grouped by module.
- **Recovery:** greedy modularity-maximizing community detection vs the labels (NMI/ARI), with a dataflow-only variant and a random-partition baseline.
- **Null model:** per run, shuffle module labels among non-boss nodes (sizes fixed), recompute; one-sided p = P(perm ≤ observed). **Instability** I = Ce/(Ca+Ce) from cross-module dataflow.

## Claim 1 — low inter-module coupling, high cohesion, recoverable modules
- **Inter-module persisted messages: 0 total** across all runs — a *structural* zero (briefing/report only run along tree edges); reported for completeness, not as evidence.
- **Modularity Q (labeled partition):** median 0.323 (IQR 0.267–0.384; mean 0.293, 95% CI [0.260, 0.322]).
- **Unsupervised recovery of the prescribed modules — NMI:** median 0.753 (IQR 0.723–0.875; mean 0.762, 95% CI [0.726, 0.791]); **ARI:** median 0.668 (IQR 0.610–0.835; mean 0.691, 95% CI [0.653, 0.726]). Louvain on the interaction graph rediscovers the Builder/Exploiter/Fixer/Reporter partition.
  - *Circularity disclosure:* the interaction graph is 64% tree-message weight (median) and modules ARE depth-1 subtrees, so recovery partly reflects the defining topology. On the **dataflow-only** (behavioral) graph recovery is lower — NMI median 0.653 (IQR 0.622–0.691; mean 0.655, 95% CI [0.641, 0.669]) — yet still far above a random-partition baseline (NMI median 0.389 (IQR 0.365–0.431; mean 0.432, 95% CI [0.407, 0.459])).
- **Inter-module interaction fraction (messages + dataflow):** median 0.163 (IQR 0.125–0.216; mean 0.174, 95% CI [0.153, 0.194]).
- **Inter-module *dataflow* fraction (the permutation-tested behavioral signal):** observed median 0.389 vs label-permutation null 0.722 (mean z = -1.80; individually p<0.05 in only 49% of runs — significant on average, not universally).

## Claim 2 — no duplicated analysis across modules
- **Cross-module recon-read Jaccard:** median 0.132 (IQR 0.093–0.200; mean 0.165, 95% CI [0.141, 0.190]); permutation mean z = -0.21 (p<0.05 in 12% of runs).
- Interpretation: modules **share** recon reads of the common target source, so literal 'no duplicated analysis' is **not** supported — overlap is at/above chance. This is largely expected (shared input understanding), not a coupling defect.

## Claim 1b — minimized global-context reliance
- **Shared-store writes per run:** median 53.000 (IQR 39.000–66.000; mean 50.346, 95% CI [45.654, 54.654]); 79/81 runs use the channel.
- Interpretation: the global context is **heavily used** (not minimized). Reads are not event-logged, so consumption is not directly observable; writes alone show active reliance.

## Claim 3 — no cross-module file contention
- **Write-write overlap (same file written by ≥2 modules), totals by zone:** {'src': 63, 'testcase': 1, 'other': 0}.
- **By module pair:** {'exploiter+fixer': 63, 'builder+exploiter': 1} — almost all are Exploiter+Fixer co-editing the vulnerable source (the prescribed trigger→patch handoff), not contention.
- **Producer→consumer dataflow edges by zone:** {'src': 322, 'testcase': 230} — concentrated in the prescribed `/testcase` + `/src` handoff (expected by the DAG).
- **Read overlap by zone (shared inputs):** {'src': 365, 'testcase': 132, 'work': 0, 'other': 0} — `/src` reads are shared input, not contention.

## Per-module profile (mean across runs)

| module | cohesion (intra) | coupling (inter) | coupling ratio | instability I |
|---|---|---|---|---|
| builder | 6.1 | 0.4 | 0.06 | 0.00 |
| exploiter | 15.6 | 5.9 | 0.29 | 0.04 |
| fixer | 8.2 | 6.3 | 0.52 | 0.88 |
| reporter | 0.0 | 1.0 | 0.40 | 1.00 |

Producer→consumer dataflow (mean edges/run, top): exploiter->fixer 5.46, fixer->reporter 0.67, exploiter->reporter 0.3, builder->exploiter 0.19, builder->fixer 0.15, builder->reporter 0.06

## Figures (SVG in `figures/`, captions in `FIGURES.md`)
- **`figures/fig1_node_dsm.svg`** — Node x node interaction DSM for a representative run (faad2.cve-2018-20358); cell = message + dataflow weight, nodes grouped by module. Dense block-diagonals are within-module cohesion; sparse off-blocks are inter-module coupling.
- **`figures/fig2_module_dsm.svg`** — Aggregate module x module interaction (mean weight per run). The bright diagonal (cohesion) and the boss row/column (sole inter-module relay) dominate; non-boss off-diagonal coupling is comparatively small.
- **`figures/fig3_module_dataflow_dsm.svg`** — Directed producer->consumer file dataflow between modules (row writes, column reads), concentrated along the prescribed Builder->Exploiter->Fixer->Reporter handoff.
- **`figures/fig4_recovery_nmi.svg`** — Unsupervised community detection recovers the prescribed modules: full interaction graph NMI ~0.75, dataflow-only (purely behavioral) ~0.65, both far above a random-partition baseline ~0.39. The full-vs-dataflow gap quantifies how much recovery is topology-driven.
- **`figures/fig5_coupling_null_z.svg`** — Per-run z-score of the observed inter-module dataflow fraction against a label-permutation null. Mass left of the dashed line = coupling below chance (mean z ~ -1.8).
- **`figures/fig6_module_instability.svg`** — Module instability mirrors the prescribed dependency DAG: Builder is stable (its build outputs are consumed downstream), Reporter is unstable (consumes all, consumed by none).
- **`figures/fig7_cohesion_coupling.svg`** — Within-module interaction (cohesion) exceeds cross-module interaction (coupling) for every module.
- **`figures/fig8_file_overlap_zone.svg`** — File interaction by workspace zone: /src read-overlap is shared input (expected), cross-module producer->consumer concentrates in /testcase + /src (the prescribed handoff), write-write contention is small.

## Bottom line
- **Supported:** the modular *structure* is real and **recoverable above chance** (NMI/ARI well above a random-partition baseline; positive Q), and inter-module data coupling runs **below a label-permutation null on average** (mean z ≈ −1.8; individually significant in ~half of runs). The system executes a clean, low-coupling modular decomposition — but recovery is partly topology-driven (see disclosure), so this is execution fidelity + recoverability, not spontaneous emergence.
- **Not supported as literally stated:** modules **do** share recon reads (Claim 2) and make heavy use of the global context (Claim 1b); cross-module file dependence exists but matches the prescribed handoff (Claim 3).

## Limitations
- Inter-module persisted messaging is 0 *by construction* (schema cannot encode it) — weak alone.
- The interaction graph is message-dominated (~64% tree-edge weight), so module recovery partly reflects the prescribed tree topology; dataflow-only recovery (~0.65 NMI) is the behavioral lower bound, still well above the random-partition baseline.
- Shell-mediated file writes are under-captured (LOW confidence); headline write metrics use tool-based writes (Fixer patch recall ≈ 0.987).
- Global-context reads are not logged; only writes are observed.
- Cross-aggregate ordering uses `occurred_at` (coarse); producer/consumer timing is approximate.