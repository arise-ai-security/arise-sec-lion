# Cost-family studies — N1 / N2 / B3 / B4

Four studies that share one curated 50-CVE task set and isolate where multi-agent
coordination cost comes from. The family hypothesis is a **normalized-cost ordering**:

> N1, N2 > B3 > B4   (the full Boss→Manager→Worker tree should be cheapest, because
> manager-level prompt-cache reuse is maximized)

| Study folder | Cell | Topology | Boss | Manager | Worker | Subagents | Judge |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `n1-openhands-linear` | N1 | flat (1 agent) | — | — | OpenHands · gpt-5.3-codex | off | n/a (flat) |
| `n2-openhands-subagents` | N2 | flat + native subagents | — | — | OpenHands · gpt-5.3-codex | **on** | n/a (flat) |
| `b3-boss-bef-direct` | B3 | Boss → BEF workers (`max_depth=1`) | sonnet-4-6 | (none spawn) | OpenHands · gpt-5.4-mini | — | **off** |
| `b4-boss-manager-worker` | B4 | Boss → BEF managers → workers (`max_depth=2`) | sonnet-4-6 | sonnet-4-6 | OpenHands · gpt-5.4-mini | — | **off** |

Design decisions baked into the configs:

- **Same input for N and B.** N1/N2 run `orchestration.mode: flat`, which routes the
  identical SEC-bench BEF 4-phase pipeline prompt (`flat_pipeline.j2` → build/exploit/fix/report,
  the same phase partials the B-cell workers use) plus the same CVE context through the unified
  `main.py run` entrypoint, and writes the same `/testcase` deliverables (`model_patch.diff`,
  `repo_changes.diff`, `security_report.md`, …). So N-vs-B differences are coordination structure,
  not input.
- **N2 subagent notification.** `worker.tool_params.openhands.enable_subagents: true` arms
  OpenHands' native task-delegation tool *and* appends `FLAT_SUBAGENT_NOTE` to the prompt
  (via `_flat_subagent_enabled`), so the agent is told it may delegate (bounded two-level tree).
- **Identical tools except subagent spawning.** Every cell's worker gets
  `allowed_tools: [file_editor, glob, grep]` + MCP `[shell_in_container, valgrind_run, klee_run]`.
  The only tool delta is N2's delegation tool.
- **B3/B4 judge OFF** (`orchestration.skip_judge: true`).
- **Boss = sonnet-4-6 in B3 and B4** (the plan's "change opus-4.8 → sonnet 4.6 for the boss").
- **B4 events flow to the DB** exactly as the B-cells do; N1/N2 emit `RunStarted` /
  `AgentCreated` / `WorkerCostRecorded` / `RunCompleted` to Postgres through the same pipeline,
  so all analysis grounds on DB events only.

## Task set

`experiments/shared/datasets/cve50-2026-06-09.lock.yaml` records the seed (`20260609`), the
50 selected instance ids (identical across all four `dataset.yaml`), and the 2-instance smoke
pair.

Smoke pair: `openexr.cve-2020-16589`, `faad2.cve-2018-20196` (chosen as the first two seeded
instances with local `secb-tools` images, second from a different project).

## Running

```bash
# one study, the 2 smoke instances, --parallel 2
experiments/<study>/smoke.sh

# all four studies CONCURRENTLY (one matrix process each, --parallel 2 → 8 runs in flight)
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
ALL=n1-openhands-linear,n2-openhands-subagents,b3-boss-bef-direct,b4-boss-manager-worker
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
