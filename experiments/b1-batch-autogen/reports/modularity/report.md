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
- **Null model:** per run, shuffle module labels among non-boss nodes (sizes fixed), recompute; one-sided p = (#{perm ≤ observed}+1)/(N+1). **Instability** I = Ce/(Ca+Ce) from cross-module dataflow.

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

| module | decisions/run | artifacts/run | total/run |
|---|---|---|---|
| builder | 6.07 | 6.25 | 12.32 |
| exploiter | 12.16 | 12.33 | 24.49 |
| fixer | 7.02 | 5.42 | 12.44 |
| reporter | 0.4 | 0.69 | 1.09 |

- Interpretation: the global context is **heavily used** (not minimized) — Exploiter writes ~2× the others; Reporter is near-silent. Reads are not event-logged, so consumption is not directly observable; writes alone show active reliance.
- **Content characterization** (all 81 runs): the store is a **module-partitioned annotation layer, not a payload store** — **0 of ~2,000 artifacts contain actual file content** (diffs/dumps); values are prose descriptions/pointers to workspace files (median ~93 chars). The per-module division matches the prompts (Builder→build/env, Exploiter→PoC+findings, Fixer→patch+validation), but the **key vocabulary is ~75% run-unique/ad-hoc** — e.g. the prompt's 'MANDATORY' `binary_path` key appears verbatim in only 2/81 runs (the concept in 56/81, fragmented across 20+ spellings). So the shared state functions as a human-readable changelog, not the machine-keyable table the prompts specify.

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

## Figures — what each shows and how to read it

### `figures/fig1_node_dsm.svg`
- **Shows:** Node×node matrix for one representative run; rows/cols = nodes grouped by module (black lines = module boundaries), cell darkness = interaction-graph weight (messages + dataflow).
- **Read:** Dense dark blocks ON the diagonal = high within-module cohesion; near-empty OFF-block cells = low inter-module coupling. Good modularity = a clean block-diagonal.

### `figures/fig2_module_dsm.svg`
- **Shows:** Module×module matrix; cell = mean interaction weight per run; diagonal = cohesion, off-diagonal = coupling, the boss row/column = the relay.
- **Read:** Bright diagonal + bright boss row/col with DIM non-boss off-diagonal = cohesive modules whose only cross-talk is via the boss.

### `figures/fig3_module_dataflow_dsm.svg`
- **Shows:** Directed 4×4; row = producer (writes a file), column = consumer (reads it); cell = mean producer→consumer edges/run.
- **Read:** Mass on the Builder→Exploiter→Fixer→Reporter super-diagonal = data flows along the prescribed pipeline; little back-flow or off-pipeline coupling.

### `figures/fig4_recovery_nmi.svg`
- **Shows:** Bars = NMI of unsupervised communities vs the true module labels, for the full graph, the dataflow-only graph, and a random-partition baseline; error bars = 95% CI.
- **Read:** Full and dataflow-only well ABOVE the random baseline = modules are recoverable from behavior; the full-minus-dataflow gap = how much recovery is driven by tree topology vs behavior.

### `figures/fig5_coupling_null_z.svg`
- **Shows:** Histogram of per-run z-scores: observed inter-module dataflow fraction vs the label-permutation null; dashed line at z=0.
- **Read:** Mass LEFT of 0 = coupling below chance (more self-contained than random labeling); the further left, the stronger. Mean z ≈ −1.8.

### `figures/fig6_module_instability.svg`
- **Shows:** Bars = mean instability I = Ce/(Ca+Ce) per module.
- **Read:** An ASCENDING Builder→Reporter pattern = stable producers → unstable consumers, i.e. the data dependency direction mirrors the prescribed DAG (Conway's law).

### `figures/fig7_cohesion_coupling.svg`
- **Shows:** Per module, two bars: intra-module (cohesion) vs inter-module (coupling) interaction weight per run.
- **Read:** Intra > inter for every module = each module talks to itself more than to others — cohesive and loosely coupled.

### `figures/fig8_file_overlap_zone.svg`
- **Shows:** Per workspace zone (`/src`,`/testcase`,`/work`), bars: write-write overlap, read-overlap, and producer→consumer edges per run.
- **Read:** Low write-write everywhere = little contention; read-overlap concentrated in `/src` = shared input (expected, not a defect); producer→consumer in `/testcase` and `/src` = the prescribed handoff.

## Bottom line
- **Supported:** the modular *structure* is real and **recoverable above chance** (NMI/ARI well above a random-partition baseline; positive Q), and inter-module data coupling runs **below a label-permutation null on average** (mean z ≈ −1.8; individually significant in ~half of runs). The system executes a clean, low-coupling modular decomposition — but recovery is partly topology-driven (see disclosure), so this is execution fidelity + recoverability, not spontaneous emergence.
- **Not supported as literally stated:** modules **do** share recon reads (Claim 2) and make heavy use of the global context (Claim 1b); cross-module file dependence exists but matches the prescribed handoff (Claim 3).

## Limitations
- Inter-module persisted messaging is 0 *by construction* (schema cannot encode it) — weak alone.
- The interaction graph is message-dominated (~64% tree-edge weight), so module recovery partly reflects the prescribed tree topology; dataflow-only recovery (~0.65 NMI) is the behavioral lower bound, still well above the random-partition baseline.
- Shell-mediated file writes are under-captured (LOW confidence); headline write metrics use tool-based writes (Fixer patch recall ≈ 0.987).
- Global-context reads are not logged; only writes are observed.
- Cross-aggregate ordering uses `occurred_at` (coarse); producer/consumer timing is approximate.

## Appendix A — Operational definitions (exactly as computed)

These are **operationalizations specific to this event data**, inspired by but **not identical to** the textbook software-engineering metrics. Each gives the exact logic (code in `experiments/shared/scripts/analysis/modularity/`).

- **Node** = an `aggregate_id` that has an `AgentCreated` event. **Module** = the `[Builder]/[Exploiter]/[Fixer]/[Reporter]` bracket of the node's depth-1 ancestor (direct child of the boss root); the root = `boss`; no bracket = `unknown`. (`modules.LabeledModules`)
- **Interaction graph** = undirected weighted graph over nodes; weight(u,v) = (#persisted messages u↔v: each `ChildSpawned` parent→child + each `ChildCompleted`/`ChildFailed` child→parent) + (#producer→consumer dataflow edges u↔v). (`claims.interaction_weights`)
- **Dataflow edge (producer→consumer)** = per normalized path, order touches by (occurred_at, seq); a read/search of the path by node B that follows the most recent write/edit by a *different* node A emits a directed edge A→B ("last-writer-before-read"). (`normalize.build_dataflows`)
- **Cohesion (intra-module weight)** = Σ interaction-graph edge weights with both endpoints in the same non-boss module. **Coupling (inter-module weight)** = Σ weights with endpoints in two different non-boss modules. **coupling ratio** = inter/(intra+inter). NOTE: a weighted-edge-sum operationalization, *not* LCOM/relational cohesion. (`claims.compute_module_profiles`)
- **inter_module_fraction (run)** = inter/(intra+inter) over all module-pair edges; boss-relay edges excluded from both numerator and denominator. (`claims.compute_coupling`)
- **Modularity Q** = Newman weighted modularity of the *labeled* partition on the interaction graph: Q = Σ_c [L_c/m − (D_c/2m)²], where L_c = internal edge weight of module c, D_c = summed weighted degree of its nodes, m = total edge weight. (`graph.modularity`)
- **Community detection** = greedy agglomerative: repeatedly merge the two edge-connected communities whose merge most increases Q, until none improves it. (`graph.greedy_communities`)
- **Recovery NMI / ARI** = agreement between the labeled-module partition and the greedy-community partition over the same nodes (label-name-invariant). NMI = 2·MI/(H_a+H_b); ARI = chance-adjusted Rand. (`graph.normalized_mutual_info`, `adjusted_rand_index`)
- **Dataflow-only recovery** = recovery NMI where community detection runs on the dataflow-only edge weights (messages excluded) — the behavioral lower bound. **Random-partition baseline** = mean NMI over 20 random node→module relabelings (module sizes preserved) vs the true labels — the chance floor.
- **Permutation null (coupling)** = statistic is the fraction of dataflow edges that are cross-module (both endpoints non-boss); null = 1000× shuffle of module labels among non-boss nodes (sizes fixed); z = (obs − null_mean)/null_sd; one-sided p = (#{perm ≤ obs}+1)/(N+1) (add-one estimator). (`claims.permutation_nulls`)
- **Instability I (per module)** = from CROSS-MODULE dataflow edges only: Ca(m) = #edges where m is the WRITER (m's output consumed elsewhere ⇒ afferent/depended-upon), Ce(m) = #edges where m is the READER (m consumes others ⇒ efferent/depends-on); I = Ce/(Ca+Ce). NOTE: maps Martin's instability onto dataflow direction; *not* package class-dependency counting. (`claims.compute_module_profiles`)
- **Recon-read Jaccard (task overlap)** = per module, the set of files recon-read (op∈{read,search}, confidence∈{high,med}); statistic = mean pairwise |A∩B|/|A∪B| over module pairs. (`claims.compute_task_overlap`)
- **Write-write overlap** = per zone, #distinct normalized paths written/edited (HIGH-confidence) by ≥2 different modules. **Producer→consumer** = directed dataflow edges aggregated by module pair. (`claims.compute_file_overlap`, `dsm.module_dataflow_matrix`)
- **Global-context write** = a `DecisionRecorded` or `ArtifactStored` event on the SharedStore aggregate (`uuid5(NS, run_id)`), attributed to its author module via `decided_by`/`stored_by`. (`claims.compute_global_context`)
- **File touch / confidence / zone** = from `ThoughtCaptured` tool_use (Read/Write/Edit → `file_path`/prefix = HIGH; Bash/security-shell → `/src|/testcase|/work` tokens in the command = LOW; Grep/Glob → search `path` = MED) and `ProbeCompleted.result_summary` (read_symbol/read_file header, search-match lines = MED). **zone** = first path segment (`src`/`testcase`/`work`/`other`). (`paths.extract_touches`)