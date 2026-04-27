# How to Run an Experiment

Step-by-step manual for the `experiments/` matrix runner. Every command runs from the repo root unless noted.

---

## 0. Prerequisites

### 0.1 One-time setup (per machine)

| Need | Command |
|---|---|
| Python deps | `uv sync --frozen` |
| Postgres event store | `cd deployment && docker compose --profile local up -d db` |
| `.env` populated | `cp deployment/.env.example deployment/.env` and fill in `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `POSTGRES_PASSWORD` |
| Host-side env (for matrix runner only) | `export POSTGRES_HOST=localhost` (the `.env` ships `POSTGRES_HOST=db` for in-Docker use) |

### 0.2 Build the secb-tools image per CVE

The matrix runner expects a `secb-tools:<project>.<cve>-patch` image for every CVE in `experiments/<study>/dataset.yaml`. Build with:

```bash
bash deployment/build-secbench-tools.sh <project>.<cve>            # shorthand
bash deployment/build-secbench-tools.sh path/to/cve.json           # JSON fixture
bash deployment/build-secbench-tools.sh hwiwonlee/secb.eval...     # full base image
```

Examples:
```bash
bash deployment/build-secbench-tools.sh openjpeg.cve-2016-7445
bash deployment/build-secbench-tools.sh gpac.cve-2023-5586 imagemagick.cve-2019-13309
```

What the script does:
1. Resolves the input to `hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch` and the target tag `secb-tools:<project>.<cve>-patch`.
2. Builds the layered image (valgrind, KLEE, claude CLI, Python venv with `mcp[server]`, MCP server bundle).
3. Smokes the image: `claude --version`, `from mcp.server.fastmcp import FastMCP`, `valgrind --version`. A failed smoke aborts so a broken layer never sits in cache.

Knobs: `FORCE_REBUILD=1` ignores existing-image cache; `SKIP_SMOKE=1` skips post-build verification.

### 0.3 Confirm the rebuild landed

```bash
docker run --rm --entrypoint /bin/bash secb-tools:<project>.<cve>-patch \
  -lc 'claude --version && /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print(\"MCP OK\")"'
```

---

## 1. Writing a cell config YAML

Cell configs live at `experiments/<study>/configs/<cell>.yaml`. Each is an overlay over `config/config.yaml` — only specify what differs.

### 1.1 Required structure

```yaml
extends: config/config.yaml

overrides:
  orchestration.mode: flat            # flat | hierarchical
  orchestration.skip_judge: true      # B-cell verifier toggle (hierarchical only)

  worker.tool: claude_code            # claude_code | openhands
  worker.model: claude-sonnet-4-6     # any LiteLLM-routable model id
  worker.allowed_tools: ["*"]         # tool allowlist (["*"] = unrestricted)
  worker.disallowed_tools: []         # tool blocklist
  worker.timeout: 5400                # per-worker wall budget, seconds
  worker.max_iterations_per_run: 40

  worker.tool_params:
    openhands: null                   # MUST null out the inactive slot
    claude_code:                      # tagged-union: only the active slot is populated
      output_format: stream-json
      include_partial_messages: true
      max_turns: 40
      use_global_config: false        # false = scratch CLAUDE_CONFIG_DIR (recommended)

  output.directory: ./runs            # run pool root
  domain.plugin: security             # selects SEC-bench plugin
```

### 1.2 Per-cell archetypes

| Cell | Distinguishing overrides |
|---|---|
| **A1** (Claude CLI, subagents on) | `orchestration.mode: flat`, `worker.tool: claude_code`, `worker.disallowed_tools: []` |
| **A2** (Claude CLI, subagents off) | same as A1 + `worker.disallowed_tools: ["Task"]` |
| **B1** (our system, judge off) | `orchestration.mode: hierarchical`, `orchestration.skip_judge: true`, `worker.tool: claude_code` |
| **B2** (our system, judge on) | same as B1 + `orchestration.skip_judge: false` |
| **C1/C2** (Qwen via OpenHands) | `worker.tool: openhands`, `worker.model: ollama_chat/qwen3.5:397b-cloud`, `worker.tool_params.openhands: {timeout_seconds: 5400, max_iterations_per_run: 40}` |

### 1.3 Wiring the cell into the matrix

Edit `experiments/<study>/manifest.yaml`:

```yaml
study_id: <study>
schema_version: 2
hypothesis: <one-sentence reason this study exists>
dataset: dataset.yaml
replicates: 1
exclude_legacy_migration: true
cells:
  A1: {group: A, runner: aris, config: configs/A1-claude-code-subagent.yaml}
  A2: {group: A, runner: aris, config: configs/A2-claude-code-nosubagent.yaml}
  B1: {group: B, runner: aris, config: configs/B1-ours-claude-noverifier.yaml}
  B2: {group: B, runner: aris, config: configs/B2-ours-claude-verifier.yaml}
  C1: {group: C, runner: aris, config: configs/C1-qwen-noverifier.yaml}
  C2: {group: C, runner: aris, config: configs/C2-qwen-verifier.yaml}
```

Group letters and runners must be registered in `experiments/shared/groups.yaml` and `experiments/shared/runners/`.

Validate before running:

```bash
uv run python -m experiments.shared.scripts.validate_manifest --study <study>
```

---

## 2. Triggering experiments (with parallelism)

### 2.1 Dry run (lists jobs, exits without dispatching)

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1,B1,C1 \
  --tasks openjpeg.cve-2016-7445 \
  --replicates 1 --parallel 1 --dry-run
```

### 2.2 Smoke (one cell × one task, real run)

```bash
set -a && source deployment/.env && set +a && export POSTGRES_HOST=localhost
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1 --tasks openjpeg.cve-2016-7445 \
  --replicates 1 --parallel 1 --no-render --no-continue-on-error
```

### 2.3 Full matrix in parallel

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1,A2,B1,B2,C1,C2 \
  --replicates 1 \
  --parallel 4
```

`--parallel <N>` uses a thread pool capped at N. Pick N based on:
- Docker concurrency on the host (each run starts a per-run secb-tools container).
- LLM rate limits (Anthropic / Ollama).
- Postgres connection cap.

A safe starting point on a workstation is `--parallel 4`. The matrix preserves deterministic per-job order in the summary regardless of completion order.

### 2.4 Useful flags

| Flag | Effect |
|---|---|
| `--cells A1,B2` | Only run the named cells. |
| `--tasks gpac.cve-2023-5586` | Restrict tasks (comma-separated). |
| `--replicates N` | Override `manifest.yaml` replicate count. |
| `--continue-on-error` (default) | Capture per-job failures, keep going. |
| `--no-continue-on-error` | Abort on the first failure. |
| `--no-render` | Skip the per-study render/validate step (still runs `collect`). |
| `--dry-run` | List jobs that would run, do not dispatch. |

### 2.5 Exit codes

- `0` — every job's `run_manifest.json.exit_status` was `success`.
- `1` — at least one job failed (non-success exit_status, or runner exception).

The matrix reads each run's actual exit_status from `runs/<run_id>/run_manifest.json` — it does **not** trust just "the runner returned a UUID."

---

## 3. Generating reports

The matrix runner triggers reports automatically *unless* `--no-render` was passed. To run the report pipeline manually:

### 3.1 One-shot (recommended)

```bash
uv run python experiments/<study>/scripts/collect.py
uv run python experiments/<study>/scripts/plot_success.py
uv run python experiments/<study>/scripts/render_report.py
uv run python -m experiments.shared.scripts.validate_reports --study <study>
```

Order matters:
1. `collect.py` — walks every run pool (`runs/` plus `settings.output.directory`), copies traces into `experiments/<study>/artifacts/<cell>/<task>/replicate-<n>/<run_id>/`, writes `reports/tables/{summary,run_metrics,totals}.csv`.
2. `plot_success.py` — renders `reports/figures/cells-overview.svg` from `summary.csv`.
3. `render_report.py` — Jinja-templates `reports/report.md` from manifest + CSVs + figures.
4. `validate_reports.py` — recomputes the sha256 of every input listed in each output's frontmatter / `.generated.json` sidecar and fails if any drift is detected.

### 3.2 Re-trigger from inside the matrix run

The standard run already does steps 1–4. Keep `--no-render` only for partial reruns where you want the matrix to write `matrix-summary.md` without regenerating per-study tables.

### 3.3 What the reports contain

- `reports/report.md` — Jinja-rendered overview, cells table, runs summary, links to artifacts. Numeric values come exclusively from CSVs.
- `reports/tables/summary.csv` — per-cell aggregate (runs, deliverables, tool-call counts, tokens, cost, duration, tool_calls_by_type).
- `reports/tables/run_metrics.csv` — per-run row with the same metric set + run_id, exit_status, events.jsonl path, artifacts path.
- `reports/tables/totals.csv` — single-row grand total across all enrolled cells.
- `reports/figures/cells-overview.svg` — bar chart of runs/deliverables per cell.
- `reports/enrollment.lock.yaml` — lockfile of every run included in this study, with uv.lock sha for reproducibility.
- `reports/matrix-summary.md` — written by `run_matrix.py`, lists per-cell pass/fail counts and the failed-jobs table.

### 3.4 Provenance and tamper-checks

Every `.md` report carries YAML frontmatter (`generated_by`, `template`, `inputs`, `inputs_sha256`, `output_sha256`); every binary has a `.generated.json` sidecar entry. `validate_reports.py` recomputes both at validation time. **Numbers in the report MUST come from a script's CSV output** — manually-edited reports fail validation.

---

## Quick reference

```bash
# Build images for a smoke
bash deployment/build-secbench-tools.sh openjpeg.cve-2016-7445

# Validate the manifest
uv run python -m experiments.shared.scripts.validate_manifest --study <study>

# Smoke 1 cell × 1 task
set -a && source deployment/.env && set +a && export POSTGRES_HOST=localhost
uv run python -m experiments.shared.scripts.run_matrix \
  --study <study> --cells A1 --tasks openjpeg.cve-2016-7445 \
  --replicates 1 --parallel 1 --no-render --no-continue-on-error

# Full matrix in parallel
uv run python -m experiments.shared.scripts.run_matrix \
  --study <study> --cells A1,A2,B1,B2,C1,C2 --replicates 1 --parallel 4

# Regenerate reports manually
uv run python experiments/<study>/scripts/collect.py
uv run python experiments/<study>/scripts/render_report.py
uv run python -m experiments.shared.scripts.validate_reports --study <study>
```
