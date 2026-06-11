# Running the N1 / N2 / B3 / B4 cost study from scratch

Reproduces the four-cell topology cost study (50 CVE instances per cell) on a
fresh machine. All numbers come from the Postgres `events` table.

## What the cells are

| Cell | Topology | Worker model | Orchestration (boss/mgr) |
|------|----------|--------------|--------------------------|
| N1 | flat (one OpenHands agent) | gpt-5.3-codex | — (flat) |
| N2 | flat + subagents | gpt-5.3-codex | — (flat) |
| B3 | boss → BEF workers | gpt-5.4-mini | claude-sonnet-4-6 (boss) |
| B4 | boss → BEF managers → workers | gpt-5.4-mini | claude-sonnet-4-6 (boss+mgr) |

## 0. Prerequisites

- **Docker** (daemon running) — Postgres + one ephemeral container per CVE run.
- **uv** — the Python harness runs on the host (Docker-out-of-Docker spawns the
  CVE containers), so Docker alone is not enough. Install:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **git**, and ~**200–400 GB free disk** (50 SEC-bench images are large).
- **API keys**: `OPENAI_API_KEY` (all cells — gpt workers) and
  `ANTHROPIC_API_KEY` (B3/B4 — sonnet boss/managers). Budget real spend: this is
  200 full runs (50 × 4).

## 1. Clone + install

```bash
git clone <repo-url> arise-sec-lion && cd arise-sec-lion
uv sync --frozen
```

## 2. Postgres (Docker)

```bash
cp deployment/.env.example deployment/.env
# Edit deployment/.env: POSTGRES_PASSWORD, OPENAI_API_KEY, ANTHROPIC_API_KEY
docker compose --profile local up -d        # starts Postgres; schema auto-inits on first run
```

## 3. Shell env for host runs (every new shell)

```bash
set -a; source deployment/.env; set +a
export POSTGRES_HOST=localhost               # .env points at the compose-internal 'db'
export ARISE_SUBPROCESS_TIMEOUT_SECONDS=15600  # B4 trees run long; widen the per-run cap
```

## 4. Build the 50 CVE images (one-time, slow)

The 50 instances are pinned in
`experiments/shared/datasets/cve50-2026-06-09.lock.yaml` (all four cells run the
identical set). Each build pulls the `hwiwonlee/secb.eval.*` base image and
layers the tool stack (claude CLI, MCP server, valgrind), then smoke-tests it.
Use the bulk builder (`-j` for parallelism, continue-on-error by default):

```bash
FIXTURES=$(uv run python -c "import yaml; \
print(' '.join('plugins/security/tests/fixtures/'+i+'.json' for i in \
yaml.safe_load(open('experiments/shared/datasets/cve50-2026-06-09.lock.yaml'))['selection_order']))")
deployment/build-all-images.sh -j 4 $FIXTURES
# Verify count (expect 50):
docker images --format '{{.Repository}}:{{.Tag}}' | grep -c '^secb-tools:.*-patch'
```

(`deployment/build-all-images.sh -j 4` with no args builds every fixture in the
repo — overkill; pass the 50 above to build exactly the study set.)

## 5. Run each cell on all 50 instances

Each study's `dataset.yaml` already lists its 50 `default_cves`, so omit
`--tasks` to run the full set. `--continue-on-error` is on by default (a failed
instance is recorded and the matrix keeps going).

```bash
for STUDY in n1-openhands-linear n2-openhands-subagents b3-boss-bef-direct b4-boss-manager-worker; do
  uv run python -m experiments.shared.scripts.run_matrix --study "$STUDY" --parallel 2
done
```

- `--parallel 2` runs 2 instances at once. Raise cautiously — each run is a
  Docker container plus heavy LLM traffic; too much parallelism causes API
  timeouts and host saturation.
- To run one cell, or a subset of instances, use `--study X [--tasks a.cve-1,b.cve-2]`.
- Results land in `runs/<root_id>/` and the events DB.

## 6. Read the results (cost comparison)

```bash
uv run python -m experiments.shared.scripts.study_sql cost \
  --studies n1-openhands-linear,n2-openhands-subagents,b3-boss-bef-direct,b4-boss-manager-worker
```

Other subcommands: `cache` (hit rate by role/depth), `tools` (probe counts),
`files` (redundant cross-agent reads), `check` (recomputed-vs-billed USD audit),
`runs` (per-run status). Add `--json` to pipe into further analysis. These are
the only sanctioned source of cost numbers — do not read them from logs.

## Notes / gotchas

- **`POSTGRES_HOST`**: inside compose it's `db`; from host-launched runs export
  `localhost` (step 3) or the matrix can't reach Postgres.
- **N1 is slow and messy by nature** — one flat agent grinds the whole task up
  to 250 iterations; expect long wall-clock and occasional MCP tool-arg retries.
- **`run_matrix --tasks a,b`** has a quirk where two `--tasks` can collapse to a
  single job; for a guaranteed per-instance run use one `--study`+`--tasks`
  invocation per instance, or just omit `--tasks` to run the dataset.
- **Re-running** an instance appends new events; scope cost queries by run/time
  if you re-run, and use `study_sql runs` to see which `root_id` is which.
