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
uv sync --frozen          # host-side Python harness; Postgres ships via Docker (step 2)
```

## 2. Migrate DB + start the server

```bash
cp deployment/.env.example deployment/.env
```

Fill these required keys in `deployment/.env` (the shipped `.env.example` is stale — it
comments out `ANTHROPIC_API_KEY`, which B3/B4 need):

```dotenv
POSTGRES_PASSWORD=your_secure_password   # any non-empty string
OPENAI_API_KEY=sk-...                    # all cells (gpt-5.x workers)
ANTHROPIC_API_KEY=sk-ant-...             # B3/B4 (claude-sonnet boss/managers)
```

```bash
docker compose --profile local up -d     # Postgres (schema auto-migrates on first boot) + API server
curl -s localhost:8000/api/health        # verify the server is up
```

## 3. Shell env for host runs (every new shell)

```bash
set -a; source deployment/.env; set +a
export POSTGRES_HOST=localhost               # .env points at the compose-internal 'db'
export ARISE_SUBPROCESS_TIMEOUT_SECONDS=15600  # B4 trees run long; widen the per-run cap
```

## 4. CVE images — auto-pulled, no build needed

The 50 instances are pinned in
`experiments/shared/datasets/cve50-2026-06-09.lock.yaml` (all four cells run the
identical set). **You do not need to build them.** On the first run that needs
an image, the system pulls the prebuilt `cheshire0814/secb-tools:<id>-patch` from
Docker Hub and retags it locally — config key `security.tools_image_registry`
(default `cheshire0814`). Nothing to do here; skip to step 5.

To pre-warm the cache (optional — pulls all 50 up front instead of lazily):

```bash
uv run python -c "import yaml; \
print('\n'.join(yaml.safe_load(open('experiments/shared/datasets/cve50-2026-06-09.lock.yaml'))['selection_order']))" \
  | sed 's#^#cheshire0814/secb-tools:#; s#$#-patch#' | xargs -n1 -P4 docker pull
```

Can't pull (offline, or a different CPU arch)? Build the set locally instead:
`deployment/build-all-images.sh -j 4 --lock experiments/shared/datasets/cve50-2026-06-09.lock.yaml`.

## 5. Run all four cells in parallel

Each cell's `dataset.yaml` lists its 50 `default_cves`, so omit `--tasks` for the
full set. Background each cell so N1/N2/B3/B4 run concurrently:

**You can run these line by line with Claude Code as an observer.**

```bash
uv run python -m experiments.shared.scripts.run_matrix --study b4-boss-manager-worker --parallel 4 > runs-b4.log 2>&1 &
uv run python -m experiments.shared.scripts.run_matrix --study b3-boss-bef-direct     --parallel 4 > runs-b3.log 2>&1 &
uv run python -m experiments.shared.scripts.run_matrix --study n1-openhands-linear   --parallel 6 > runs-n1.log 2>&1 &
uv run python -m experiments.shared.scripts.run_matrix --study n2-openhands-subagents --parallel 6 > runs-n2.log 2>&1 &
```

- One cell or a subset: `--study X [--tasks a.cve-1,b.cve-2]`. `--continue-on-error`
  is on by default; results land in `runs/<root_id>/` and the events DB.
- Adjust "--parallel" parameter accordingly. 

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
