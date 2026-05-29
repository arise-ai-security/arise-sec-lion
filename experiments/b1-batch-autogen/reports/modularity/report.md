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
- **Shows:** Role-resolved DSM aggregated across ALL runs: rows/cols = role slots (module × manager/worker tier), cell = mean interaction weight per run between slots; black lines = module boundaries.
- **Read:** Dense dark blocks ON the diagonal (manager↔worker within a module) = cohesion; near-empty OFF-block cells = low inter-module coupling; the boss row/col is the relay. A clean block-diagonal = modular. This is the all-runs average, not a single sample.

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

## Appendix A — Operational definitions (plain meaning + exact logic)

Every metric is an **operationalization specific to this event log** — inspired by the classic software-engineering ideas, but computed from agent events and **not identical** to the textbook metric. Each entry gives (a) the plain question it answers, (b) exactly how it is computed, and (c) how to read it. Code is in `experiments/shared/scripts/analysis/modularity/`.

### A.0 Building blocks (everything else is built from these)

- **Node** — one agent in the run; concretely an event-store `aggregate_id` that has an `AgentCreated` event. The Boss, the four phase managers, and every worker are nodes.
- **Module** — *which phase a node belongs to.* Walk from the node up its parent chain to the **depth-1 ancestor** (the direct child of the Boss) and read that ancestor's `[Builder]/[Exploiter]/[Fixer]/[Reporter]` task prefix; the Boss itself is `boss`, a node with no bracketed ancestor is `unknown`. Intuition: a node inherits the phase of the subtree it lives in. (`modules.LabeledModules`)
- **Touch** — *one file access by a node*, recovered from the log: it records the node, the normalized file path, the operation (read / write / edit / search / exec), a **confidence** (HIGH when the path is explicit in the tool call, LOW when scraped from a shell command), and a **zone** = the top-level directory: `/src` = target source, `/testcase` = deliverables + cross-phase handoff, `/work` = scratch. (`paths.extract_touches`)
- **Dataflow edge (producer→consumer)** — *a behavioral dependency between two nodes.* If node A writes a file and later node B reads that same file (B≠A), record a directed dependency A→B. (Per file, touches are sorted in time and each read is linked to the most recent earlier write — "last-writer-before-read".) Intuition: "B used what A produced." (`normalize.build_dataflows`)
- **Interaction graph** — *the central structure every coupling/modularity number is computed on.* One vertex per node; an undirected edge between two nodes weighted by how much they interact = (#messages between them) + (#producer→consumer file dependencies between them), where a "message" is a parent briefing a child (`ChildSpawned`) or a child reporting back (`ChildCompleted`/`Failed`). The DSM figures are simply this graph drawn as a matrix. (`claims.interaction_weights`)

### A.1 Coupling & cohesion — how self-contained is each module?

- **Cohesion (intra-module interaction)** — *how much does a module talk to itself?* Sum of interaction-graph edge weights whose two endpoints are in the **same** module. Higher = the module's nodes coordinate mostly internally. (A weighted-edge sum — *not* the textbook LCOM.)
- **Coupling (inter-module interaction)** — *how much do different modules talk to each other?* Sum of edge weights between **two different** non-boss modules. Lower = better-encapsulated modules.
- **Coupling ratio** — coupling / (cohesion + coupling) for a module: the fraction of its interaction that crosses its own boundary. 0 = fully self-contained, 1 = all cross-module. (`claims.compute_module_profiles`)
- **inter_module_fraction (per run)** — the run-level version: cross-module interaction / total non-boss interaction (Boss-relay edges excluded from both sides, since the Boss is wiring, not a module). (`claims.compute_coupling`)

### A.2 Is the module structure *real*? — recovery

- **Modularity Q (Newman)** — *is the prescribed Builder/Exploiter/Fixer/Reporter grouping a good partition of the interaction graph?* One number = (fraction of edge weight that falls **inside** modules) − (what you'd expect if the same edges were rewired at random while preserving each node's degree). Formula Q = Σ_modules [ L_c/m − (D_c/2m)² ] (L_c = internal edge weight of module c, D_c = total degree of its nodes, m = total edge weight). Read: >0 = more internal clustering than chance; ~0.3–0.7 is a typical "modular" range. (`graph.modularity`)
- **Community detection (greedy)** — *if we did NOT know the modules, would the graph reveal them?* An unsupervised algorithm groups nodes to maximize Q: start with each node alone, then repeatedly merge the two connected groups whose merge most increases Q, until no merge helps. The result is the "discovered" grouping. (`graph.greedy_communities`)
- **Recovery NMI / ARI** — *do the discovered groups match the real modules?* Compare the discovered grouping to the true `[Builder]/…` labels. NMI (normalized mutual information) and ARI (adjusted Rand index) each run ~0 (no better than chance) → 1 (identical grouping) and ignore the arbitrary group names. High = the modular structure is visible in behavior, not just in the labels. (`graph.normalized_mutual_info`, `adjusted_rand_index`)
- **Dataflow-only recovery** — the same NMI but the graph uses **only** producer→consumer file edges (parent/child messages dropped, since those follow the tree that *defines* the modules). This is the stricter, purely-behavioral recovery; it is lower than the full-graph number, and the gap between them measures how much recovery is "given" by tree topology.
- **Random-partition baseline** — the chance floor for recovery: randomly relabel nodes into modules (keeping each module's size), compute NMI vs the true labels, average over 20 shuffles. Recovery only counts as evidence if it sits well above this floor.

### A.3 Is the low coupling actually below chance? — permutation null

- **Permutation null** — *the observed cross-module coupling is low, but is it lower than chance?* The statistic is the fraction of producer→consumer edges that cross module boundaries. We build a null by **shuffling which module each (non-boss) node belongs to** — keeping module sizes fixed — and recomputing the statistic 1000×. We report z = (observed − mean of shuffles) / sd and a one-sided p = (#shuffles ≤ observed + 1)/(1000 + 1). Read: z well below 0 = the real modules are more self-contained than a random regrouping. (`claims.permutation_nulls`)

### A.4 Producer vs consumer role — instability

- **Instability I (per module)** — *is a module mainly a producer others depend on (stable), or a consumer that depends on others (unstable)?* Using only **cross-module** dataflow edges: Ca(m) = #edges where m is the WRITER (its output is consumed elsewhere ⇒ others depend on it); Ce(m) = #edges where m is the READER (it consumes others' output ⇒ it depends on them). I = Ce/(Ca+Ce), from 0 (pure producer, stable) to 1 (pure consumer, unstable). Builder ≈ 0 (everyone uses its build); Reporter ≈ 1 (reads all, feeds none). Applies Martin's instability idea to file dataflow — *not* class-dependency counting. (`claims.compute_module_profiles`)

### A.5 The three claim metrics

- **Recon-read overlap (Claim 2 — duplicated analysis)** — *do modules redo each other's reading?* For each module take the set of files it read during analysis; the statistic is the average pairwise Jaccard overlap |A∩B|/|A∪B| between modules' read-sets. High = modules re-read the same files (duplicated work). (`claims.compute_task_overlap`)
- **Write-write overlap (Claim 3 — contention)** — *do modules edit the same files?* Per zone, the number of distinct files that ≥2 different modules both wrote/edited (HIGH-confidence writes only); `write_write_pairs` records which module pairs collide. (`claims.compute_file_overlap`)
- **Producer→consumer by zone** — the count of cross-module dataflow edges per zone — the legitimate handoff (one phase's output feeds the next). (`dsm.module_dataflow_matrix`)
- **Global-context write** — one entry a worker published to the shared blackboard: a `DecisionRecorded` (key→value+rationale) or `ArtifactStored` (key→description), attributed to the author's module. Measures how much each module relies on shared state. (`claims.compute_global_context`)