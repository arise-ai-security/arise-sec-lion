# Smoke findings — cost-family studies (2026-06-09)

2-instance smoke (`openexr.cve-2020-16589`, `faad2.cve-2018-20196`), 1 replicate, run
concurrently (`smoke-all-2026-06-09.sh`, each study `--parallel 2`). **All 8 runs
`exit_status=success`.** Every number below is recomputed from the Postgres `events`
table + `runs/*/run_manifest.json` via `study_sql`; nothing is estimated. Snapshots:
`experiments/shared/datasets/smoke-2026-06-09-metrics/*.json`.

## Headline: the hypothesis is inverted

Target ordering **N1,N2 > B3 > B4** (B4 cheapest). Observed (2-instance sums, USD):

| cell | raw_usd | norm_usd | agents/run | wall_s (per run) |
| --- | --- | --- | --- | --- |
| **N1** | **1.8997** | **2.9129** | 1 | 817 / 1235 |
| **N2** | 2.7230 | 4.2822 | 1 | 978 / 2766 |
| **B3** | 4.5040 | 9.6278 | 5 | 1849 / 1550 |
| **B4** | 4.6162 | 10.1770 | 5 | 2004 / 1643 |

Observed ordering is **N1 < N2 < B3 < B4** on both raw and normalized cost — the exact
reverse of the hypothesis. `norm_usd` re-prices every call at `claude-sonnet-4-6` rates
(provider cache-discount ratios preserved), so this is cross-provider-fair; the B-cells'
2× normalized penalty is real, not a pricing artifact.

**Cost recomputation is trustworthy.** `study_sql check`: recomputed USD == provider-reported
`cost_usd` at **0.0 drift** for all three models (`claude-sonnet-4-6` 5.3475/5.3475,
`gpt-5.3-codex` 4.6227/4.6227, `gpt-5.4-mini` 3.7727/3.7727). Billing semantics encoded:
`prompt_tokens` is the total input inclusive of cache tokens for every stream here
(LiteLLM normalizes Anthropic usage to OpenAI-style totals); uncached = prompt − cache_read
− cache_write.

## Why B costs more than N — the spend decomposition

From `cost.json`, the B-cells pay a **sonnet orchestration layer on top of a gpt worker layer
that already costs about what N costs**:

| cell | sonnet (orchestration) raw | gpt (workers) raw | total raw |
| --- | --- | --- | --- |
| B3 | 2.6953 | 1.8087 | 4.5040 |
| B4 | 2.6522 | 1.9640 | 4.6162 |
| N1 | — | 1.8997 (gpt-5.3-codex) | 1.8997 |

The gpt worker layer alone (B3 $1.81, B4 $1.96) is already ≈ N1's entire cost ($1.90). The
sonnet boss/assess layer (~$2.65) is **pure additive overhead** the N-cells don't pay. That
single fact makes the inversion structural, not incidental: B can only beat N if the
orchestration layer's cost is driven below ≈0 — impossible — OR if it makes the worker layer
dramatically cheaper/fewer, which it currently does not (B workers cost the same as N despite
the boss having already done recon).

## Cache hit by level (`cache.json`)

| cell | role/level | hit rate |
| --- | --- | --- |
| N1 | session (gpt) | 96.8% |
| N2 | session (gpt) | 96.3% |
| B3 | boss (sonnet, depth 0) | 61.8% |
| B3 | depth-1 (gpt workers) | 91.1% |
| B4 | boss (sonnet, depth 0) | 59.5% |
| B4 | depth-1 (gpt workers) | 92.0% |

The hypothesis needs orchestration nodes (boss/managers) to have the **highest** reuse. They
have the **lowest** (~60%). Grounded mechanism: each agent makes exactly **one** sonnet
`TokensConsumed` call (boss=decompose, BEF=assess; verified per-aggregate counts), so the 60%
is *within* one multi-iteration tool-calling loop. The stable prefix (system+role+CVE first
block + tools array) caches across iterations, but the conversation tail is rewritten every
iteration by observation-masking and condensation, so ~40% of input is re-billed uncached. The
gpt workers, running one long single-session loop, get OpenAI's automatic prefix cache at ~92%.

## Tool-call economy is inverted (`tools.json`)

Best scenario: boss most, manager next, worker least. Observed the opposite — the leaf workers
do the most tool calls:

| cell | boss (depth 0) | depth-1 workers |
| --- | --- | --- |
| B3 | 80 recon | 175 recon + 90 file_editor + 111 MCP + 37 grep |
| B4 | 80 recon | 167 recon + 92 file_editor + 125 MCP + 59 grep |
| N1 | 78 MCP + 17 file_editor (single agent) | — |
| N2 | 96 MCP + 29 file_editor + 9 grep + 2 Task (single agent) | — |

The boss issues ~80 recon probes, then each BEF worker issues ~170 *more* recon probes —
re-discovering what the boss already found. The boss's recon is not effectively suppressing
worker recon.

## Redundant cross-worker file reads (`files.json`)

Best scenario: one file read by one worker per subtree. `FileEditorAction command=view` reads
(recon-probe paths are absent from the event schema, so excluded — a measurement gap to close):

| cell | task | files read | read by >1 worker | redundant % |
| --- | --- | --- | --- | --- |
| N1/N2 | both | 2–6 | 0 | 0% (single agent) |
| B3 | openexr | 30 | 5 | 16.7% |
| B3 | faad2 | 17 | 4 | 23.5% |
| B4 | openexr | 35 | 1 | 2.9% |
| B4 | faad2 | 10 | 5 | 50.0% |

The BEF workers re-read the same files; the sibling-handoff / shared-context machinery is not
preventing it.

## Structural finding: B4's manager tier never formed

`decisions.json` (operation types per agent depth): in **both** B3 and B4, all 8 depth-1 BEF
agents emit `task_assessment → worker_execution` and **never** `task_decomposition`; only the
boss (depth 0) decomposes. So B4 (`max_depth=2`) ran with the **same 5-agent, 2-level shape as
B3** — boss + 4 BEF children, all executing as workers. The manager-level prompt-cache reuse
the hypothesis hinges on **never had a chance to materialize**: there were no depth-2 workers
under a caching manager. B4 ≈ B3 (raw $4.62 vs $4.50; the $0.11 delta is noise). Any test of
"managers maximize cache reuse" first requires making depth-1 BEF nodes actually decompose.

## Improvement plan

> Synthesized by the `cost-hypothesis-analysis` workflow (5 parallel opus dimension analyses →
> 5 adversarial opus verifications → opus synthesis; 11 agents, 817k tokens). Every claim
> below traces to a verified finding; refuted claims were dropped, partial ones downgraded.

### Verdict

**The hypothesis `N1,N2 > B3 > B4` cannot hold under the current B-cell design, and B4-cheapest
is the least achievable part.** The inversion has two arithmetically distinct, additive causes,
and the B4 premise is structurally void. Verification corrected two of my seed hypotheses:

- **The 5-min TTL is NOT the boss-cache cause** (refuted). Each B boss aggregate has exactly
  **one** `TokensConsumed`/`PromptSent`; the 4 `ChildSpawned` + decomposition all fire in the
  same instant (t+261–305s). The boss's 60% hit is *within-loop* message-tail mutation, not
  cache expiry across workers. (TTL only matters for a future depth-2 manager calling
  sequentially across 25–33 min worker runs.)
- **68% of the sonnet tax is the depth-1 BEF *assessment* layer, not the boss** (re-localized).
  Of the $2.70 sonnet spend, only ~32% ($0.86) is the depth-0 boss; ~$1.84 is sonnet running as
  each BEF child's `task_assessment` brain *before* it spawns the gpt worker. Of the cache_write
  cost, 78% (200,820 of 257,850 tok) is the depth-1 assessment layer. Any boss-only fix touches
  <¼ of the tax.

### Why it inverted — ranked root causes (all verified `holds=true`)

1. **Worker-layer token VOLUME (drives the NORMALIZED inversion; the binding constraint).** The
   4 BEF children each run an independent full-context OpenHands/gpt-mini session (`decisions.json`:
   all 8 depth-1 agents `task_assessment → worker_execution`, never decompose). They carry
   cache_read 10.9M (B3) / 12.4M (B4) = **1.24× / 1.41× N2's 8.77M**. Re-priced at sonnet rates the
   worker layer **alone** is $6.93 / $7.52 — already bigger than all of N2 ($4.28) or N1 ($2.91).
   No sonnet-side fix can touch this. → `B*-…yaml:19-20` (worker.tool=openhands, gpt-5.4-mini).
2. **The added claude-sonnet-4-6 tier (drives the RAW inversion).** B bills a second model layer
   N never bills (N1/N2 `…yaml:14` "unused in flat mode"). It is **57–60% of raw B** ($2.70/$2.65)
   and exceeds B3's entire +$1.78 raw gap over N2. 68% of it is the depth-1 assessment layer.
   → `B3-…yaml:17-20`.
3. **The sonnet tier can't amortize its prefix (amplifies #2).** cache_write ($0.97) is its
   *largest* sub-cost, 3.36× cache_read, because of the single-breakpoint loop, not TTL:
   `litellm_adapter.py:116` breaks after the first user message, so the growing assistant+tool
   tail appended at `tool_calling_service.py:147,184` never carries `cache_control`. Boss hit
   61.8%/59.5% vs N 96.8%/96.3%. → `litellm_adapter.py:76-117`, `tool_calling_service.py:153-154`.
4. **B4's manager tier never materializes (voids the B4-cheapest premise).**
   `prompts/operations/assess_variable.j2:10-11` renders only a *soft* nudge "prefer executing
   directly" when `depth_remaining<=1`; at B4 depth-1, `depth_remaining=1` and `can_spawn_child()`
   is `True` (`limits.py:58-67`, 1<2) — Sonnet was *free* to decompose but the prompt talked it
   out. The captured `ComplexityEvaluated` reasoning literally says "1 level remaining… I must
   execute directly" while acknowledging "domain rules require decomposing Exploiter into exactly
   6 workers." Result: B4 ≡ B3 (5 billed agents), no manager cache reuse to measure.
   → `B4-…yaml:16`, `assess_variable.j2:10`.
5. **No boss→worker recon channel + no sibling file manifest (stacks redundant tax onto #1).**
   Boss recon lives only in the transient messages list and is discarded (`tool_calling_service.py:98-204`);
   it becomes telemetry-only Probe events, never a SharedStore artifact (`agent_orchestrator.py:911,1184-1194`;
   boss writes 0 artifacts/0 decisions). Briefing (`node_message.py:70-86`) and `sibling.j2:8-23`
   carry only prose summaries, no file list (`PeerStatus` has no files field). So leaf workers do
   the *most* tool calls (175/167 recon + 90/92 file + 111/125 MCP vs boss 80) and re-read the same
   files (16.7–50%). Second-order, but it inflates #1.

### Improvement plan (ordered by impact-per-effort)

| # | Change | File | Effect | Effort / Risk |
| --- | --- | --- | --- | --- |
| 1 | **Move orchestration off Sonnet to a Haiku-class model** (boss + manager) | `B3-…yaml:17`, `B4-…yaml:18-19` | Sonnet tier raw $2.70→~$0.95; B3 raw ~$4.50→~$2.8, B4→~$2.9 — near N2's $2.72. **Zero effect on normalized** (re-pricing equalizes models). Verify decomposition quality stays `success`. | S / med |
| 2 | **Collapse the BEF fan-out toward one shared-context worker** (fewer subtasks / shared session) | `B*-…yaml:19-20`, `decomposition_static.j2` | The **only** lever on the normalized inversion. Worker cache_read 10.9M/12.4M → toward N2's 8.77M drops normalized worker layer $6.93/$7.52 → ~$5.0–5.6. Also shrinks raw worker $1.81/$1.96 → ~$1.4. | L / high |
| 3 | **Persist boss recon as a SharedStore artifact + add a sibling file-read manifest** | `agent_orchestrator.py:911`, `sibling.j2:8-23` | Cuts worker recon ~170→~80, collapses redundant reads (B4 faad2 50%→~0). Trims the #1 volume. Necessary, not sufficient. | M / med |
| 4 | **Make the sonnet prefix cacheable**: 2nd breakpoint on the growing tail, stop in-place history mutation, 1h TTL for sequential tiers | `litellm_adapter.py:76-117`, `tool_calling_service.py:153-154` | Boss+assess hit 60%→~90%, cache_write collapses; sonnet raw ~$2.70→~$1.8–2.0. 78% of the gain is at depth-1, so apply there too. Redundant if #1 (Haiku) is taken; stackable if Sonnet kept for quality. | M / med |
| 5 | **Form the B4 manager tier — experiment-only, NOT a cost win**: raise `max_depth` to 3, or gate the soft nudge for domain MUST-decompose roles | `B4-…yaml:16` or `assess_variable.j2:10` | Makes B4 ≠ B3 so manager-cache reuse is *observable*. Honest expectation: this **adds** a sonnet tier → B4 cost goes **up** vs B3 unless reuse exceeds the added tier (implausible at these volumes with `max_concurrent_workers=1`). Option B (gate the nudge) is the correct long-term fix; Option A is the one-line experiment. | S / low |

### Can the hypothesis hold?

- **RAW B < N: hard but reachable** — only by cutting the sonnet tier hard (fix #1, Haiku-class
  orchestration). The worker layer alone is already $1.81/$1.96, leaving <$0.9 headroom under N2
  ($2.72) for *all* orchestration spend; Sonnet can't fit, Haiku can.
- **NORMALIZED B < N: essentially unreachable** while B keeps any orchestration layer on top of a
  worker layer that already matches N. The normalized worker layer alone ($6.93/$7.52) exceeds N2's
  entire $4.28; even at parity worker volume (~$4.3) you still add the orchestration tier on top.
  This is the architecture billing two layers where N bills one.
- **B4 < B3 ("B4 cheapest"): impossible until the manager tier forms (fix #5), and doubly
  undermined even then** — `max_concurrent_workers=1` (`config.yaml:52`) makes depth-2 workers
  sequential, so a manager's 5-min ephemeral cache expires between its children (needs 1h TTL,
  fix #4); and a real manager tier *adds* sonnet spend, pushing B4 above B3 unless reuse savings
  exceed it (not plausible here).

**Bottom line for "making the hypothesis hold":** fixes #1 + #2 + #3 can likely recover **raw**
`N > B`. **Normalized** `N > B` and **B4-cheapest** are not recoverable without abandoning the
two-layer sonnet-on-gpt design itself — e.g. orchestrating on a Haiku-class model *and* collapsing
the BEF fan-out into a single shared-context worker, at which point B stops being the tree the
hypothesis describes. The cleanest honest experiment is: apply #5 (form the tier) + #4 (1h TTL,
tail caching) and *measure* whether manager cache reuse is real — but expect it to raise cost, and
treat the manager-cache-reuse claim as a mechanism to demonstrate, not a cost win to bank.

### Re-validation after each fix (ground truth = DB + runs only)

```bash
cd /Users/cheshire/dev/arise-sec-lion; set -a; source deployment/.env; set +a; export POSTGRES_HOST=localhost
ALL=n1-openhands-linear,n2-openhands-subagents,b3-boss-bef-direct,b4-boss-manager-worker
uv run python -m experiments.shared.scripts.study_sql cost  --studies $ALL --json  # raw vs N 1.90/2.72, norm vs 2.91/4.28
uv run python -m experiments.shared.scripts.study_sql cache --studies $ALL --json  # B boss 0.62/0.59 → ~0.90; cache_write ↓
uv run python -m experiments.shared.scripts.study_sql files --studies $ALL --json  # redundant_read_ratio → ~0
uv run python -m experiments.shared.scripts.study_sql tools --studies $ALL --json  # depth-1 recon 175/167 → ~80
uv run python -m experiments.shared.scripts.study_sql decisions --studies b4-boss-manager-worker --json  # depth==2 rows appear (fix #5)
uv run python -m experiments.shared.scripts.study_sql runs --studies $ALL --json   # regression guard: all exit_status=success
uv run python -m experiments.shared.scripts.study_sql check --studies $ALL --json  # ledger sanity: recomputed == reported
```

Full per-dimension findings + verification verdicts: `shared/datasets/smoke-2026-06-09-metrics/`
and the workflow transcript (`wf_10f1d6bd-320`).
