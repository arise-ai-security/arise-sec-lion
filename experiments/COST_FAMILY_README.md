# Current studies — N1 / B3 / B4

Three studies that share one curated 50-CVE task set and isolate where multi-agent
coordination cost comes from. The family hypothesis is a **normalized-cost ordering**:

> N1 > B3 > B4   (the full Boss→Manager→Worker tree should be cheapest, because
> manager-level prompt-cache reuse is maximized)

| Study folder | Cell | Topology | Boss | Manager | Worker | Subagents | Judge |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `n1-openhands-linear` | N1 | flat (1 agent) | — | — | OpenHands · gpt-5.3-codex | off | n/a (flat) |
| `b3-direct-compact` | B3 | Boss → 9 direct compact roles | gpt-5.4 | omitted | same 5 LLM roles + 4 procedures as B4 | — | **off** |
| `b4-boss-manager-worker` | B4 | Boss → 4 host-created phase Managers → host-created compact roles; LLM recovery after required failure | gpt-5.4 | gpt-5.4 | mini coding; Codex reasoning; 4 host procedures | — | **off** |

Design decisions baked into the configs:

- **Same input for N and B.** N1 runs `orchestration.mode: flat`, which routes the
  identical SEC-bench BEF 4-phase pipeline prompt (`flat_pipeline.j2` → build/exploit/fix/report,
  the same phase partials the B-cell workers use) plus the same CVE context through the unified
  `main.py run` entrypoint, and writes the same `/testcase` deliverables (`model_patch.diff`,
  `repo_changes.diff`, `security_report.md`, …). So N-vs-B differences are coordination structure,
  not input.
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

`experiments/shared/datasets/cve50-2026-06-09.lock.yaml` records the seed (`20260609`), the
50 selected instance ids used by the current studies, and the 2-instance smoke
pair.

Smoke pair: `openexr.cve-2020-16589`, `faad2.cve-2018-20196` (chosen as the first two seeded
instances with local `secb-tools` images, second from a different project).

## Running

```bash
# one study, the 2 smoke instances, --parallel 2
experiments/<study>/smoke.sh

# all three studies concurrently (one matrix process each, --parallel 2 → 6 runs in flight)
experiments/run-cycle.sh <cycle-label>     # logs: temp/<cycle-label>/<study>.log

# full 50-instance run for one study
set -a; source deployment/.env; set +a; export POSTGRES_HOST=localhost
uv run python -m experiments.shared.scripts.run_matrix --study <study> --parallel 4 --replicates 1
```

`POSTGRES_HOST=localhost` is required because `deployment/.env` pins the compose-internal
name `db`, but the matrix runs `main.py` on the host against the published port.

## Analysis (ground truth = DB events + runs/ only)

Use the `study-sql` skill / `experiments.shared.scripts.study_sql`:

```bash
set -a; source deployment/.env; set +a; export POSTGRES_HOST=localhost
ALL=n1-openhands-linear,b3-direct-compact,b4-boss-manager-worker
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
