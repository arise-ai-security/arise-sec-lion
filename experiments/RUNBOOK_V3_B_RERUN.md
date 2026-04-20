# V3 B-Cell Re-Run Runbook

B1 + B2 rerun with the Claude SDK adapter fix (`tool_name`/`tool_input` capture).
Same 10 locked CVEs, same budget/wallclock caps ($10 / 5400s).

**What this fixes:** v1/v2 B-cells emitted `tool_name="Tool"` / `tool_input={}` for
all worker tool calls. Redundancy metrics, security-tool adoption, per-tool breakdown,
and audit-cheating detection were unmeasurable. The adapter hook now reads
`input_data["tool_name"]` / `input_data["tool_input"]` correctly.

---

## Phase 1: Pre-flight (setup on remote machine)

### 1.1 Clone and install

```bash
git clone <repo-url> arise-sec-lion && cd arise-sec-lion
git checkout experiment/tree-vs-flat-agent-orchestration

# Python deps
uv sync --frozen

# Analysis deps (for post-run)
uv pip install scipy statsmodels matplotlib seaborn tiktoken pandas numpy
```

### 1.2 Environment variables

```bash
cp deployment/.env.example deployment/.env
# Edit deployment/.env:
#   POSTGRES_PASSWORD=<secure-password>
#   OPENAI_API_KEY=sk-...
#   ANTHROPIC_API_KEY=sk-ant-...   (required for Claude workers)
```

Also export for the host shell (run_experiment needs DB access):

```bash
export POSTGRES_PASSWORD=<same-password>
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

### 1.3 Start infrastructure

```bash
cd deployment && docker compose --profile local up -d db && cd ..
# Wait for DB healthy:
docker compose -f deployment/docker-compose.yml ps
```

Only the `db` service is needed. The experiment runner runs on the host, not inside
a container, and talks to Postgres on `localhost:5432`.

### 1.4 Pull base images and build secb-tools layer

```bash
uv run python -m experiments.setup_images \
    --config experiments/configs/v3_b_rerun.yaml

# Verify all 10 images exist:
docker images | grep secb-tools
```

Expected output: 10 images like `secb-tools:njs.cve-2022-32414`, etc.

If the base images fail to pull (DockerHub rate limit), retry with `--pull-only`
first, then re-run without it.

### 1.5 Verify Docker socket access

The tree runner spawns SEC-bench containers via Docker-out-of-Docker. Confirm:

```bash
docker run --rm hello-world
```

If running on a remote VM with rootless Docker, ensure the socket path matches
`DOCKER_HOST` or the default `/var/run/docker.sock`.

### 1.6 Dry-run validation

```bash
uv run python -m experiments.run_experiment \
    --locked-instances experiments/configs/v3_b_rerun.yaml \
    --cells B1,B2 \
    --output-dir dataset-v3-rerun/runs \
    --concurrency 1 \
    --halt-on-anomaly 2>&1 | head -50
```

This will start the first run. If it progresses past "Starting run:" without
errors, Ctrl-C and proceed to the real run. If it fails on DB connection, image
pull, or config parsing, fix before continuing.

---

## Phase 2: Execution

### 2.1 Launch the experiment

```bash
mkdir -p dataset-v3-rerun

nohup uv run python -m experiments.run_experiment \
    --locked-instances experiments/configs/v3_b_rerun.yaml \
    --cells B1,B2 \
    --output-dir dataset-v3-rerun/runs \
    --concurrency 5 \
    --continue-on-anomaly \
    > dataset-v3-rerun/experiment.log 2>&1 &

echo $! > dataset-v3-rerun/experiment.pid
```

`--continue-on-anomaly` keeps the queue running even if individual CVEs hit
anomalies. Each anomaly is still recorded in `<run-dir>/anomaly.json`.

Concurrency 5 assumes Anthropic Tier 2+ (>=2M TPM) and >=32GB RAM.
Drop to 2 on smaller machines or Tier 1 accounts.

### 2.2 Monitor progress

```bash
# Tail the log
tail -f dataset-v3-rerun/experiment.log

# Count completed runs (each run gets an INDEX.jsonl entry)
wc -l dataset-v3-rerun/INDEX.jsonl

# Expected: 20 total (10 CVEs x B1 + 10 CVEs x B2; anchor replicates=1 adds no extras)
# Check which CVEs are done:
cat dataset-v3-rerun/INDEX.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    print(f\"{r['cell']:3s} {r['cve_id']:40s} {r['termination_reason']}\")" | sort
```

### 2.3 Resume after interruption

The runner is resume-safe: runs with an existing `events.jsonl` are skipped.
Simply re-run the same command from 2.1 (without nohup if debugging):

```bash
uv run python -m experiments.run_experiment \
    --locked-instances experiments/configs/v3_b_rerun.yaml \
    --cells B1,B2 \
    --output-dir dataset-v3-rerun/runs \
    --concurrency 5 \
    --continue-on-anomaly
```

### 2.4 Anomaly triage (if --halt-on-anomaly was used)

If the runner halts, check:

```bash
find dataset-v3-rerun/runs -name anomaly.json -exec cat {} \;
```

Common anomalies and fixes:
- `malformed_json_after_retries`: BOSS JSON parse failure. Check model output
  in `stdout_stderr.log`. Usually transient; re-run resumes past completed runs.
- `worker_stuck`: Worker exceeded wallclock. Check if the Docker container is
  still running (`docker ps`). Kill stale containers and re-run.

---

## Phase 3: Post-run analysis

### 3.1 Project tree events from Postgres

For each B-cell run, the events live in Postgres. Project them to `events.jsonl`
for offline analysis:

```bash
for run_dir in dataset-v3-rerun/runs/*/*/*/; do
    if [ ! -f "$run_dir/events.jsonl" ] || [ ! -s "$run_dir/events.jsonl" ]; then
        echo "Projecting $run_dir ..."
        uv run python -m experiments.tree_projection --run-dir "$run_dir"
    fi
done
```

### 3.2 Verify tool-name capture (the whole point of v3)

```bash
# Should show actual tool names, NOT just "Tool":
for run_dir in dataset-v3-rerun/runs/*/B1/*/; do
    cve=$(basename $(dirname $(dirname $(dirname "$run_dir"))))
    echo "=== $cve ==="
    python3 -c "
import json
with open('${run_dir}events.jsonl') as f:
    tools = set()
    for line in f:
        ev = json.loads(line)
        if ev.get('event_type') == 'tool_use':
            tools.add(ev.get('payload',{}).get('tool_name','?'))
    print('  tools:', sorted(tools))
"
done
```

If you still see only `"Tool"`, the adapter fix is not applied -- check the
`infrastructure/adapters/worker/claude_sdk_adapter.py` hook.

### 3.3 Run analysis scripts

All analysis scripts live in `docs/artifacts/scripts/`. They read from `dataset/`
(v1 A-cells) and can be pointed at the v3 dataset for B-cells.

```bash
# Recompute metrics over the combined v1 (A cells) + v3 (B cells) dataset
uv run python docs/artifacts/scripts/compute_metrics.py
uv run python docs/artifacts/scripts/make_figures.py

# V2-style comparison (A1/A2 from v1 vs B1/B2 from v3)
uv run python docs/artifacts/scripts/v2_analysis.py

# Per-tool breakdown (now measurable for B cells)
uv run python docs/artifacts/scripts/a_cell_tool_breakdown.py

# Redundancy analysis (now measurable for B cells)
uv run python docs/artifacts/scripts/b_prompt_redundancy.py

# Phase reached vs passed
uv run python docs/artifacts/scripts/phase_reached.py
```

Note: some scripts have hardcoded dataset paths (e.g. `dataset/` for A-cells,
`dataset-v2-20260420/` for B-cells). You may need to update the `DATASET_ROOT`
constant in each script to point at `dataset-v3-rerun/` for the new B-cell data.

### 3.4 Validate key metrics

Check that v3 B-cells now produce non-zero values for previously-broken metrics:

```bash
python3 -c "
import json
with open('dataset-v3-rerun/INDEX.jsonl') as f:
    for line in f:
        r = json.loads(line)
        print(f\"{r['cell']:3s} {r['cve_id']:40s} cost=\${r.get('total_cost_usd',0):.2f}  wall={r.get('wallclock_seconds',0):.0f}s  events={r.get('event_count',0)}\")
"
```

### 3.5 Archive the dataset

```bash
tar --zstd -cf dataset-v3-rerun-$(date +%Y%m%d).tar.zst dataset-v3-rerun/
sha256sum dataset-v3-rerun-*.tar.zst > dataset-v3-rerun-$(date +%Y%m%d).tar.zst.sha256
```

---

## Quick reference

| Step | Command | Expected time |
|------|---------|---------------|
| Install | `uv sync --frozen` | 30s |
| DB up | `cd deployment && docker compose --profile local up -d db` | 10s |
| Build images | `uv run python -m experiments.setup_images --config experiments/configs/v3_b_rerun.yaml` | ~30min (first time) |
| Run experiment | `uv run python -m experiments.run_experiment --locked-instances experiments/configs/v3_b_rerun.yaml --cells B1,B2 --output-dir dataset-v3-rerun/runs --concurrency 5 --continue-on-anomaly` | 3-8 hours |
| Project events | `for run_dir in dataset-v3-rerun/runs/*/*/*/; do uv run python -m experiments.tree_projection --run-dir "$run_dir"; done` | ~5min |
| Analysis | `uv run python docs/artifacts/scripts/compute_metrics.py` + `make_figures.py` | ~2min |
