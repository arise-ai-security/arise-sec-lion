# EXPERIMENT_MANUAL.md

How to design and run a comparative **A/B/C experiment** on SEC-bench from a fresh clone of this repo. Start here if you need to *compare treatments* (architectures, models, verifier on/off, …) across the CVE dataset. If you only want to run our system once against a single CVE for exploration, `README.md` §4 is enough.

---

## 1. What "experiment" means here

In this repo, an experiment is a **study** that compares variants of the system against a fixed CVE task set. The vocabulary is small and load-bearing — internalize it before touching any config:


| Term          | Definition                                                                                                                                                                         | Lives in                                                                                     |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **Group**     | A high-level approach class. Single uppercase letter. Examples:`A: Claude Code CLI`, `B: Our System`, `C: Our System + Qwen`.                                                      | `experiments/shared/groups.yaml`                                                             |
| **Cell**      | A specific variant within a group. Name must start with the group letter (e.g.`A1`, `A2`, `B1`, `B2`). Each cell has its own config YAML. **Cells are your independent variable.** | `experiments/<study>/manifest.yaml` + one YAML per cell under `experiments/<study>/configs/` |
| **Task**      | One SEC-bench CVE instance (e.g.`gpac.cve-2021-40575`).                                                                                                                            | `plugins/security/tests/fixtures/*.json`                                                     |
| **Replicate** | Repeated runs of a`(cell, task)` pair, used for noise control. (Currently 1)                                                                                                       | `experiments/<study>/manifest.yaml: replicates`                                              |
| **Study**     | One self-contained matrix: groups × cells × tasks × replicates. A folder on disk.                                                                                               | `experiments/<YYYY-MM-DD-slug>/`                                                             |

The matrix is dispatched by a single command:

```bash
uv run python -m experiments.shared.scripts.run_matrix --study <study-id>
```

Every `(cell, task, replicate)` run mints a new `BOSS_ID`, writes events to Postgres, and drops artifacts under `runs/<BOSS_ID>/`. Per-study derived artifacts (reports, enrollment lock, plots) land under `experiments/<study-id>/reports/`.

> If your change to the codebase isn't comparative — just "run once and look at the output" — you don't need this manual. Go to `README.md` §4.

---

## 2. Fresh-clone fast path (~15 min, prerequisites only)

This section assumes you have nothing built locally. It gets you to "ready to design a study". Each step is a pointer; details live in `README.md`.

```bash
# (1) Python deps
uv sync --frozen

# (2) Env file — fill POSTGRES_PASSWORD, HOST_PROJECT_ROOT, OPENAI_API_KEY,
#                ANTHROPIC_API_KEY as appropriate
cp deployment/.env.example deployment/.env

# (3) Stack up (db + api dashboard + app container)
docker compose --profile local up -d --build
docker compose --profile local ps    # wait until db + api show "healthy"

# (4) (Optional: re-run only if the data is deleted) CVE fixtures — pulls the 300+ HF instances to plugins/security/tests/fixtures/.
#     Already committed for most users.
uv run --with datasets python plugins/security/tests/fixtures/fetch_secbench.py

# (5) Docker images — one per fixture (~30-90 min @ -j 4). Skips already-built images.
deployment/build-all-images.sh -j 4

# (6) Smoke check — http://localhost:8000 should show an empty dashboard
open http://localhost:8000
```

For ad-hoc single-instance debugging (not an experiment), see `README.md` §4. Otherwise continue to §3.

---

## 3. Design a study

A study is three things on disk:

```
experiments/<YYYY-MM-DD-slug>/
├── manifest.yaml         # cells, dataset pointer, replicates
├── dataset.yaml          # which CVE instances this study runs over
└── configs/              # one YAML per cell declared in manifest.yaml
    ├── A1-claude-code-subagent.yaml
    ├── A2-claude-code-nosubagent.yaml
    ├── B1-ours-claude-noverifier.yaml
    ├── B2-ours-claude-verifier.yaml
    ├── C1-qwen-noverifier.yaml
    └── C2-qwen-verifier.yaml
```

Optionally also `scripts/{collect,plot_success,render_report}.py` — `run_matrix` auto-invokes them after dispatch if present.

A canonical, runnable template ships at `experiments/shared/templates/study/`. The fastest path is to copy it and edit four lines.

### 3.1. Copy the template

```bash
STUDY=2026-05-13-my-study              # pick a YYYY-MM-DD-<slug>
cp -r experiments/shared/templates/study "experiments/${STUDY}"

# Substitute the placeholder in manifest.yaml (macOS sed shown; drop the '' on Linux)
sed -i '' "s/STUDY_TEMPLATE/${STUDY}/g" "experiments/${STUDY}/manifest.yaml"
```

The template defines the canonical six-cell matrix (A1/A2/B1/B2/C1/C2) with tool allowlists, models, and iteration caps already pinned. **For most studies you only need to edit `manifest.yaml` (`created_at`, `hypothesis`) and `dataset.yaml` (which CVE instances).** Open the per-cell configs only when you're deliberately drifting a knob — and when you do, drift it the same way in every cell that's supposed to be paired.

The template lives under `experiments/shared/templates/` so `validate_manifest._discover_studies` skips it (the `shared` directory is excluded from study discovery). It will never pollute reports.

### 3.2. What's in `manifest.yaml`

The design contract: which cells exist, where each cell's config lives, what dataset to run, and how many replicates. Schema enforced by `validate_manifest.py`:

- `study_id` must equal the folder name.
- Each cell entry needs `group`, `runner`, `config`.
- `group` must be a single uppercase letter registered in `experiments/shared/groups.yaml`.
- Cell name must start with its group letter (`A1` under `group: A`, not `Foo1`).
- `runner` must be a registered runner. Currently only **`arise`** exists — it dispatches every cell through `harness.run_arise` → `main.py run`.
- `config` is resolved relative to `experiments/<study>/`; the file must exist.

Optional but useful:

- `headline_cells: [A1, A2, B1, B2, C1, C2]` — restricts the reports aggregate to a subset (e.g. exclude smoke cells while keeping them runnable for dev).

If you add a new group letter (`D`, `E`, …), register it in `experiments/shared/groups.yaml` first. That's a deliberate project-wide change — discuss it with the team. Reusing existing letters with new cells underneath is the common case.

### 3.3. What's in `dataset.yaml`

**The explicit list of CVE instance IDs this study runs over.** `run_matrix` enumerates `cells × dataset.default_cves × replicates` — without `dataset.yaml`, no tasks; without an instance in `default_cves`, that CVE is not in scope.

Three fields:

- `default_cves: [<instance-id>, …]` — the task set. Edit this to grow or shrink scope. The template ships 10 instances; the full dataset is 311 (see `plugins/security/tests/fixtures/`).
- `per_cell_overrides: {<cell>: {subset: [<instance-id>, …]}}` — optional per-cell scope narrowing. Each subset MUST be a subset of `default_cves` (validator rejects out-of-set entries).
- `source.paths: [<fixture-path>, …]` — one entry per CVE in `default_cves`, pointing at the JSON fixture under `plugins/security/tests/fixtures/`. Pre-flight (`ensure_task_coverage()`) verifies coverage and aborts before any worker starts if a fixture is missing.

### 3.4. What's in each cell config

Each cell config is a tiny overlay on `config/config.yaml`. The template's cells already pin everything load-bearing:

| Knob | A1 | A2 | B1 | B2 | C1 | C2 |
|---|---|---|---|---|---|---|
| `orchestration.mode` | `flat` | `flat` | `hierarchical` | `hierarchical` | `hierarchical` | `hierarchical` |
| `orchestration.skip_judge` | `true` | `true` | `true` | `false` | `true` | `false` |
| `boss.model` / `manager.model` | (n/a — flat) | (n/a — flat) | `claude-opus-4-5-20251101` | `claude-opus-4-5-20251101` | `claude-opus-4-5-20251101` | `claude-opus-4-5-20251101` |
| `worker.tool` | `claude_code` | `claude_code` | `claude_code` | `claude_code` | `openhands` | `openhands` |
| `worker.model` | `claude-sonnet-4-5-20250929` | `claude-sonnet-4-5-20250929` | `claude-sonnet-4-5-20250929` | `claude-sonnet-4-5-20250929` | `ollama_chat/qwen3:8b` | `ollama_chat/qwen3:8b` |
| `worker.allowed_tools` | `[Read, Write, Edit, MultiEdit, Bash, Glob, Grep, Task]` | `[Read, Write, Edit, MultiEdit, Bash, Glob, Grep]` | same as A2 | same as A2 | `[file_editor, glob, grep]` | same as C1 |
| `worker.tool_params.openhands.mcp_tools` | n/a | n/a | n/a | n/a | `[shell_in_container, valgrind_run, klee_run]` | same as C1 |
| `worker.disallowed_tools` | `[WebSearch, WebFetch]` | `[WebSearch, WebFetch, Task]` | `[WebSearch, WebFetch]` | `[WebSearch, WebFetch]` | `[]` | same as C1 |
| `worker.max_iterations_per_run` | `250` | `250` | `250` | `250` | `250` | `250` |
| `worker.timeout` (s) | `5400` | `5400` | `5400` | `5400` | `5400` | `5400` |

**Tool allowlists are pinned by `BUG-TOOL1` (`research/findings/00-synthesis.md`).** OpenHands native tools and SEC-bench MCP tools are separate channels: C cells put native OpenHands tools in `worker.allowed_tools` and MCP tools in `worker.tool_params.openhands.mcp_tools`. A1 vs A2 differs by `Task` (in `disallowed_tools` for A2) so the flat-mode subagent-note derivation, which reads `disallowed_tools` as its single source of truth, infers subagent-off correctly for A2.

**Iteration cap is 250 across all cells** (SecVerifier-parity budget). A-cells also set `claude_code.max_turns: 250` because the flat Claude CLI backend consumes that tool-specific cap. Setting either lower starves flat A-cells relative to hierarchical B/C cells, because A has no decomposition to split work across multiple workers.

**Fairness invariants — keep these constant across paired cells:** `worker.max_iterations_per_run`, A-cell `claude_code.max_turns`, `worker.timeout`, `worker.allowed_tools`, OpenHands `mcp_tools`, `worker.disallowed_tools`, the `--domain security` runner invocation, and the prompt set. Per-cell drift here invalidates the between-cell comparison.

### 3.5. Validate before running

```bash
uv run python -m experiments.shared.scripts.validate_manifest --study "${STUDY}"
```

Success is silent. Failure prints every problem at once (missing config files, unknown groups, naming violations, …). Also runs as a pre-commit hook.

---

## 4. Run the matrix

```bash
uv run python -m experiments.shared.scripts.run_matrix --study "${STUDY}"
```

### 4.1. What `--study <id>` does

The `<id>` is the folder name under `experiments/`. The driver uses it to:

1. Locate `experiments/<id>/manifest.yaml` and load it.
2. Validate the manifest (re-runs §3.5 internally — pre-commit + driver both call `validate_manifest`).
3. Resolve `manifest.dataset` (default `dataset.yaml`) relative to `experiments/<id>/` and load it.
4. Resolve every cell's `config` path relative to `experiments/<id>/`.
5. Sweep stale state in every selected cell's `output.directory` pool (orphan `ARISE_RUN_RESULT_PATH` files older than 1 h, dead PID locks).
6. Invoke per-study scripts at `experiments/<id>/scripts/{collect,plot_success,render_report}.py` if present.
7. Write outputs to `experiments/<id>/reports/{matrix-summary.md, enrollment.lock.yaml, …}`.
8. Tag the enrollment lockfile with `study_id: <id>` and a `design_sha` (git SHA of the manifest at run time) so later inspection can tell which manifest version produced these results.

### 4.2. End-to-end flow

The driver:

1. Validates the manifest (step 4.1.2).
2. Sweeps stale state (step 4.1.5).
3. Enumerates the cartesian product `cells × tasks × replicates`. Per-cell overrides narrow each cell's task list independently.
4. Dispatches each job via the cell's runner (`arise` → `main.py run` subprocess inside `arise-app`).
5. On completion: runs the shared `collect.py` (enrollment lockfile), then any per-study scripts (step 4.1.6), then `validate_reports`.
6. Writes `experiments/<id>/reports/matrix-summary.md` with per-cell success/failure counts and a failed-jobs table.

### Useful flags


| Flag                                             | Purpose                                                                                                                                                                                                 |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--cells A1,B2`                                  | Run only these cells (comma-separated).                                                                                                                                                                 |
| `--tasks gpac.cve-2021-40575,njs.cve-2022-28049` | Run only these tasks.                                                                                                                                                                                   |
| `--replicates N`                                 | Override`manifest.replicates`.                                                                                                                                                                          |
| `--parallel K`                                   | Up to`K` concurrent dispatches (`ThreadPoolExecutor`). Default `1` (sequential). Mind host CPU, Docker daemon, and per-provider rate limits. See `agent-docs/parallel-20-host-saturation-diagnosis.md`. |
| `--continue-on-error` / `--no-continue-on-error` | Default is continue; failed jobs are captured and the matrix proceeds. Use`--no-continue-on-error` for tight debugging loops.                                                                           |
| `--no-render`                                    | Skip the per-study render/validate step (partial reruns where study scripts aren't ready yet).                                                                                                          |
| `--dry-run`                                      | Validate + list selected jobs, dispatch nothing.                                                                                                                                                        |

### Resuming after partial completion

`run_matrix` is **not** idempotent at the (cell, task, replicate) granularity — re-invoking re-runs every selected job. To resume only the missing ones:

```bash
# 1. Which (cell, task, replicate) tuples already succeeded? The enrollment
#    lockfile (produced by collect.py) is the source of truth post-run.
cat experiments/<study>/reports/enrollment.lock.yaml

# 2. Pick the gaps. Run the gaps explicitly via --cells / --tasks filters.
uv run python -m experiments.shared.scripts.run_matrix \
  --study <study> \
  --cells B2 \
  --tasks gpac.cve-2021-40575 \
  --replicates 1
```

For ad-hoc CVE-only resume (no cell breakdown), the recipe in §8 below works too.

---

## 5. Inspect results

### 5.1. Matrix summary

`experiments/<study>/reports/matrix-summary.md` — per-cell success/failure, list of failed jobs with truncated error messages. Generated by `run_matrix`.

### 5.2. Enrollment lockfile

`experiments/<study>/reports/enrollment.lock.yaml` — the authoritative `(study_id, cell, task, replicate) → run_id` mapping. Generated by `collect.py` (invoked automatically by `run_matrix`; can also be run on its own):

```bash
uv run python -m experiments.shared.scripts.collect --study <study>
```

### 5.3. Per-run artifacts

For each `run_id` referenced in the lockfile:


| Path                              | Contents                                                                                                                                                              |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/<run_id>/testcase/`         | `security_report.md`, `model_patch.diff`, `repro.sh`, `base_commit_hash`, worker logs                                                                                 |
| `runs/<run_id>/src/`              | Host mirror of the vulnerable source tree                                                                                                                             |
| `runs/<run_id>/run_manifest.json` | Run metadata (`exit_status`, `models`, `summary_available`, `deliverables`). Does **not** carry `instance_id` — that lives in Postgres `RunStarted.domain_metadata`. |
| `runs/<run_id>/events.jsonl`      | Per-run projected event stream (also queryable in Postgres)                                                                                                           |

### 5.4. CLI views (inside `arise-app`)

```bash
docker exec arise-app python main.py list --limit 20
docker exec arise-app python main.py events --errors-only
docker exec arise-app python main.py summary
```

### 5.5. Dashboard

http://localhost:8000 — live tree, events, costs. Filterable by run.

### 5.6. Custom per-study scripts

If `experiments/<study>/scripts/{collect,plot_success,render_report}.py` exist, `run_matrix` runs each after dispatch (in that order) and `validate_reports` checks their outputs against the `*.generated.json` provenance schema. Write these when you need study-specific aggregations beyond the shared `collect.py`.

---

## 6. Share results

The full state is **two pieces**:

1. **Postgres `events` table** — the source of truth. Append-only event log; everything else replays from here.
2. **Host-side `runs/<run_id>/`** — bind-mounted to the host via `../runs:/app/runs`. Contains deliverables and per-run logs.

### Sender

```bash
set -a && source deployment/.env && set +a

# (a) Dump events
scripts/dump_events.sh                  # writes events-YYYYMMDDTHHMMSSZ.sql.gz

# (b) Tar run artifacts
tar -czf "runs-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" runs/

# (c) Optionally include the study folder for the design contract + reports
tar -czf "study-<id>-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" experiments/<study>/
```

### Receiver

After `docker compose --profile local up -d` (schema auto-applies on first start):

```bash
tar -xzf runs-20260513T164000Z.tar.gz
tar -xzf study-<id>-20260513T164000Z.tar.gz

set -a && source deployment/.env && set +a

# Fresh DB:
scripts/restore_events.sh events-20260513T164000Z.sql.gz

# DB already has events — choose one:
scripts/restore_events.sh --truncate events-20260513T164000Z.sql.gz   # wipe + replace
scripts/restore_events.sh --append   events-20260513T164000Z.sql.gz   # merge (errors on collisions)
```

Open http://localhost:8000 — every imported `run_id` appears in the agent list.

---

## 7. Reference — paths and sources of truth


| Path                                              | Role                                                                                 |
| ------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Postgres`events`                                  | **Source of truth.** Append-only, OCC on `(aggregate_id, sequence_number)`.          |
| `experiments/shared/groups.yaml`                  | Project-wide group letter → label registry.                                         |
| `experiments/shared/harness.py`                   | `run-arise` subcommand; wraps `main.py run` with per-job `ARISE_RUN_RESULT_PATH`.     |
| `experiments/shared/scripts/run_matrix.py`        | The matrix driver.                                                                   |
| `experiments/shared/scripts/validate_manifest.py` | Manifest schema validator (pre-commit +`run_matrix` entry).                          |
| `experiments/shared/scripts/collect.py`           | Builds`enrollment.lock.yaml` from `run_manifest.json` files.                         |
| `experiments/<study>/manifest.yaml`               | **Study design contract** — cells, dataset pointer, replicates, headline allowlist. |
| `experiments/<study>/dataset.yaml`                | Task set + fixture paths.                                                            |
| `experiments/<study>/configs/*.yaml`              | Per-cell config overlays.                                                            |
| `experiments/<study>/reports/`                    | All derived artifacts (`matrix-summary.md`, `enrollment.lock.yaml`, custom plots).   |
| `plugins/security/tests/fixtures/*.json`          | CVE instance metadata fed into the worker as`--domain-context-file`.                 |
| `runs/<run_id>/testcase/`                         | Per-run deliverables.                                                                |

---

## 8. Troubleshooting


| Symptom                                                      | Fix                                                                                                                                       |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `validate_manifest: cells.X1.group = 'X' not in groups.yaml` | Add the letter to`experiments/shared/groups.yaml` (team decision), or fix the typo.                                                       |
| `validate_manifest: cells.B1.config not found`               | Path is relative to`experiments/<study>/`. Check spelling and existence.                                                                  |
| `validate_manifest: name must start with group letter 'B'`   | Cell names must begin with their group letter. Rename`Foo1` → `B1`.                                                                      |
| `run_matrix: no jobs match the given filters`                | Filter set doesn't overlap with declared cells/tasks. Drop the filter or fix it.                                                          |
| `run_matrix` Docker exec error / stale code                  | `docker compose --profile local up -d --build app` to rebuild the app container.                                                          |
| `fetch_secbench.py: ModuleNotFoundError: datasets`           | Use`uv run --with datasets python …`. `datasets` is intentionally out of `pyproject.toml`.                                               |
| Bulk image build:`pull access denied`                        | The fixture's default`hwiwonlee/…` image doesn't exist for that CVE. Author a `songtli/…` override (see the `/secbench-fixture` skill). |
| `dump_events.sh: POSTGRES_PASSWORD must be set`              | `set -a && source deployment/.env && set +a` first.                                                                                       |
| `restore_events.sh` aborts on existing rows                  | Pass`--truncate` (wipe + replace) or `--append` (merge — errors on UNIQUE collisions).                                                   |
| Worker container can't reach Ollama on Linux                 | `extra_hosts: ["host.docker.internal:host-gateway"]` is already set in `deployment/docker-compose.yml`.                                   |
| Agent tree exploded past 25 nodes                            | Over-decomposition. See`README.md` §12; edit `prompts/domains/secbench/assess.j2`.                                                       |
| `--parallel >1` saturates the host                           | See`agent-docs/parallel-20-host-saturation-diagnosis.md`. Start at `--parallel 2` and scale.                                              |

### Resuming an aborted matrix (CVE-only granularity)

If you don't care about cell-level resume and just want "which CVEs are still missing across all of `arise-app`'s run history":

```bash
set -a && source deployment/.env && set +a

# (a) Done set — completed instance_ids in Postgres
docker exec arise-db psql -U arise -d arise_events -tA -c "
  SELECT DISTINCT payload->'domain_metadata'->>'instance_id'
  FROM events
  WHERE event_type = 'RunStarted'
    AND aggregate_id IN (
      SELECT aggregate_id FROM events
      WHERE event_type = 'RunCompleted' AND payload->>'status' = 'completed'
    )
    AND payload->'domain_metadata'->>'instance_id' IS NOT NULL
" | sort -u > /tmp/done.txt

# (b) Target set
ls plugins/security/tests/fixtures/ | sed 's/\.json$//' | sort > /tmp/all.txt

# (c) Diff
comm -23 /tmp/all.txt /tmp/done.txt > /tmp/todo.txt
echo "$(wc -l < /tmp/todo.txt) instances still to run"

# (d) Re-run only the missing ones via the matrix driver
uv run python -m experiments.shared.scripts.run_matrix \
  --study <study> \
  --tasks "$(paste -sd, /tmp/todo.txt)"
```

For cell-level granularity, prefer `experiments/<study>/reports/enrollment.lock.yaml` (§4 Resuming).

---

## See also

- `README.md` — conceptual overview, single-instance run, dashboard tour
- `README.md` §11 — prompt hierarchy (where to edit security prompts)
- `agent-docs/secbench-pipeline-tech-doc.md` — image build pipeline internals
- `agent-docs/parallel-20-host-saturation-diagnosis.md` — guidance for `--parallel`
- `research/02-experimental-design.md` — concrete hypotheses, IVs, DVs, threats to validity
