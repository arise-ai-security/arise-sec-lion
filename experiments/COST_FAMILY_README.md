# Current studies — N1 / B3 / B4

Three study implementations isolate where multi-agent coordination cost comes from. N1 now
uses the complete 300-task SEC-bench eval manifest; B3 and B4 retain the curated 50-CVE
development set. Cross-arm estimates must use an explicitly paired task set, such as the
confirmatory manifest, rather than comparing unmatched default populations. The family
hypothesis on paired tasks is a **normalized-cost ordering**:

> N1 > B3 > B4   (the full Boss→Manager→Worker tree should be cheapest, because
> manager-level prompt-cache reuse is maximized)

| Study folder | Cell | Topology | Boss | Manager | Worker | Subagents | Judge |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `n1-secbench-full` | N1 | flat (1 agent) | — | — | OpenHands · gpt-5.3-codex | off | n/a (flat) |
| `b3-direct-compact` | B3 | Boss → 9 direct compact roles | gpt-5.4 | omitted | same 5 LLM roles + 4 procedures as B4 | — | **off** |
| `b4-boss-manager-worker` | B4 | Boss → 4 host-created phase Managers → host-created compact roles; LLM recovery after required failure | gpt-5.4 | gpt-5.4 | mini coding; Codex reasoning; 4 host procedures | — | **off** |

Design decisions baked into the configs:

- **Same domain input and output contract for N and B.** N1 runs
  `orchestration.mode: flat`, receives the shared SEC-bench phase goals and CVE context,
  and writes the same canonical `/testcase` artifacts. Its full prompt is necessarily
  flat and self-executing, while B4 adds role, handoff, and procedure instructions.
  N1-versus-B4 therefore estimates the complete B4-system effect: topology, model
  routing, Host procedures, context handling, recovery, and aggregate compute all differ.
  B3-versus-B4 is the Manager-layer ablation.
- **Identical worker tools.** Every cell's worker gets
  `allowed_tools: [file_editor, glob, grep]` + MCP `[shell_in_container, valgrind_run, klee_run]`.
- **B3/B4 orchestration judge OFF** (`orchestration.skip_judge: true`). The independent
  three-seat semantic evaluator remains part of authoritative experiment success.
- **Boss and B4 managers = `gpt-5.4`**. Coding workers use `gpt-5.4-mini`; reasoning,
  hybrid, and synthesis workers use `gpt-5.3-codex`; four frozen roles are host procedures.
- **B3 is the direct Manager-layer ablation.** It inherits B4's prompts, models, tools,
  procedures, context policy, retries, and budgets. Generic host flattening preserves the
  same compact DAG; only `include_manager_layer: false` differs behaviorally. Briefing
  and sibling context vary
  because the trees differ.
- **B4 events flow to the DB** exactly as the B-cells do; N1 emits `RunStarted` /
  `AgentCreated` / `WorkerCostRecorded` / `RunCompleted` to Postgres through the same pipeline,
  so all analysis grounds on DB events only.

## Task set

`experiments/shared/datasets/cve50-2026-06-09.lock.yaml` records the B3/B4 development seed
(`20260609`), their 50 selected instance ids, and the shared 2-instance smoke pair. The N1
full manifest contains the complete 300-task eval split, including both smoke tasks.

Smoke pair: `openexr.cve-2020-16589`, `faad2.cve-2018-20196` (chosen as the first two seeded
instances with local `secb-tools` images, second from a different project).

## Running

```bash
# N1, the 2 validation instances through the normal batch runner
uv run python experiments/n1-secbench-full/run_batch.py \
  --instances openexr.cve-2020-16589,faad2.cve-2018-20196 \
  --batch-size 2 --parallel 2

# B3/B4 retain their study-local smoke wrappers
experiments/<b3-or-b4-study>/smoke.sh

# all three validation runs concurrently
experiments/run-cycle.sh <cycle-label>     # logs: temp/<cycle-label>/<study>.log

# full B3/B4 50-instance development run
set -a; source deployment/.env; set +a; export POSTGRES_HOST=localhost
uv run python -m experiments.shared.scripts.run_matrix --study <study> --parallel 4 --replicates 1

# full N1 300-instance operational run
uv run python experiments/n1-secbench-full/run_batch.py --batch-size 30 --parallel 2
```

`POSTGRES_HOST=localhost` is required because `deployment/.env` pins the compose-internal
name `db`, but the matrix runs `main.py` on the host against the published port.

## Analysis (ground truth = DB events + runs/ only)

Use the `study-sql` skill / `experiments.shared.scripts.study_sql`:

```bash
set -a; source deployment/.env; set +a; export POSTGRES_HOST=localhost
ALL=n1-secbench-full,b3-direct-compact,b4-boss-manager-worker
uv run python -m experiments.shared.scripts.study_sql cost  --studies $ALL   # raw + normalized USD
uv run python -m experiments.shared.scripts.study_sql cache --studies $ALL   # hit rate by role/depth
uv run python -m experiments.shared.scripts.study_sql tools --studies $ALL   # tool calls by level
uv run python -m experiments.shared.scripts.study_sql files --studies $ALL   # redundant cross-worker reads
uv run python -m experiments.shared.scripts.study_sql check --studies $ALL   # recomputed vs reported USD
```

`cost` reports `raw_usd` (recomputed from verified provider price sheets), `norm_usd`
(every call re-priced at claude-sonnet-4-6 rates, each provider's cache-discount ratios
preserved — this is the cross-provider-fair number for the hypothesis), and `reported_usd`
(provider-billed, as a cross-check). `check` validates the recomputation: drift is 0.0 for
all three models on the smoke data.
