# EXPERIMENT_MANUAL.md

End-to-end recipe for running SEC-bench CVE experiments at scale. Start here if you just cloned the repo and want to go from zero to a shareable result set. For conceptual background, read [`README.md`](README.md) first.

## Audience

You're here to: fetch the 300+ CVE dataset, build images in bulk, run instances, detect failures, resume after errors, and share outputs with the team. For single-instance exploration, `README.md` §4 is enough.

---

## Prerequisites

On the host:

- **Docker** + Docker Compose v2+
- **`uv`** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **PostgreSQL client tools** (`pg_dump`, `psql`) — needed by the dump/restore scripts
- **Local Ollama** with `qwen3:8b` pulled — `ollama pull qwen3:8b`. Verify with:
  ```bash
  curl -s http://localhost:11434/api/tags | jq '.models[].name' | grep qwen3:8b
  ```
  (Or use Ollama Cloud / OpenAI / Anthropic — see `README.md` §13.)

---

## Phase 0 — Fresh-clone setup (~10 min)

```bash
# 1. Python deps
uv sync --frozen

# 2. Env file
cp deployment/.env.example deployment/.env
# Edit deployment/.env and fill:
#   POSTGRES_PASSWORD=<anything>
#   HOST_PROJECT_ROOT=<absolute path to this repo>
#   OPENAI_API_KEY=... or skip if running all-Ollama

# 3. Bring the stack up (db + api dashboard + app container)
docker compose --profile local up -d --build
docker compose --profile local ps    # wait until db + api show "healthy"

# 4. Smoke check
open http://localhost:8000           # empty dashboard = success
```

Schema (single `events` table + 7 indexes) is auto-applied on first Postgres start via `docker-entrypoint-initdb.d`. There are no migrations to run.

---

## Phase 1 — Fetch the CVE dataset (~5 min, network-bound)

The 300+ upstream CVE instances live in the HuggingFace `SEC-bench/SEC-bench` dataset (splits: `cve`, `eval`, `oss`). Materialize them as local JSON fixtures:

```bash
# 'datasets' is NOT declared in pyproject.toml — inject it for this one call:
uv run --with datasets python plugins/security/tests/fixtures/fetch_secbench.py
```

Result: `plugins/security/tests/fixtures/*.json` grows from 28 → **311** files.

| Counted | Source |
|---|---|
| 300 | Unique upstream HF instances (deduped across splits) |
| 11 | Team-authored post-cutoff fixtures (2025/2026 CVEs + `issue-*`) — use `docker_image_override` pointing at `songtli/...` images |
| 311 | Total |

Filters: `--split cve|eval|oss`, `--instance-id <id>`, `--dry-run`, `--overwrite`.

---

## Phase 2 — Build all Docker images (~30–90 min @ `-j 4`)

Each fixture needs a `secb-tools:<instance_id>-patch` image (base image from DockerHub + analysis tools layer). The bulk builder loops over fixtures and skips images already present locally:

```bash
# 4-way parallel — recommended for the full dataset
deployment/build-all-images.sh -j 4

# Sequential (default)
deployment/build-all-images.sh

# Specific fixtures only
deployment/build-all-images.sh plugins/security/tests/fixtures/openjpeg.cve-2016-7445.json

# Preview without invoking docker
deployment/build-all-images.sh --dry-run
```

Flags: `-j N` / `--parallel N`, `--fail-fast`, `--continue-on-error` (default), `--dry-run`, `-h`.

The script tracks built / skipped / failed counts and lists failed instance_ids at the end. Exit code `1` if any image failed (regardless of `--continue-on-error`).

> **Image resolution** (`plugins/security/cve_instance.py:41-44`): if the fixture sets `docker_image_override`, that image is pulled; otherwise the fallback is `hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch`. There is **no** automatic `songtli/...` fallback — the 11 team-authored fixtures all set `docker_image_override` explicitly.

---

## Phase 3 — Run the experiment

### Single instance (smoke test)

```bash
docker exec arise-app python main.py \
  -c config/experiment-qwen3-8b-local.yaml run \
  "Analyze and patch openjpeg.cve-2016-7445." \
  --domain security \
  --domain-context-file plugins/security/tests/fixtures/openjpeg.cve-2016-7445.json
```

Note: `-c <config>` must come **before** the `run` subcommand. Watch progress at http://localhost:8000.

### Bulk run

There is **no native batch runner**. Wrap the single-instance call in a shell loop:

```bash
set -a && source deployment/.env && set +a
mkdir -p logs
for f in plugins/security/tests/fixtures/*.json; do
  instance=$(basename "$f" .json)
  echo "=== $instance ==="
  docker exec arise-app python main.py \
    -c config/experiment-qwen3-8b-local.yaml run \
    "Analyze and patch $instance." \
    --domain security \
    --domain-context-file "$f" \
    > "logs/${instance}.log" 2>&1 \
    || echo "$instance" >> /tmp/failed_instances.txt
done
```

For comparative A/B studies (cells × tasks × replicates), use the experiments matrix harness in `experiments/shared/` — see `experiments/2026-04-23-initial-secbench/manifest.yaml` for the pattern and `python -m experiments.shared.scripts.run_matrix --help`.

---

## Phase 4 — Detect failures and resume

### Detecting failures

| Where | What |
|---|---|
| Process exit code | `main.py run` returns non-zero on failure (`bootstrap/bootstrap.py:221-222`) |
| Postgres | Terminal `RunCompleted` event carries `status: "completed" \| "failed"`. Failure event types: `WorkFailed`, `VerificationFailed`, `DecisionInfeasible`, `ChildFailed` |
| Host filesystem | `runs/<BOSS_ID>/run_manifest.json` → `.exit_status` (`success` on the happy path) |
| CLI views (inside `arise-app`) | `python main.py list --limit 20`, `python main.py events --errors-only`, `python main.py summary` |
| Dashboard | http://localhost:8000 — live tree + events + costs |

### Resuming a partial run

There is **no built-in resume command**. Each `main.py run` mints a fresh BOSS_ID — running the same fixture twice creates two independent runs. The `instance_id` is **not** in `run_manifest.json`; it lives in the Postgres `RunStarted.domain_metadata` field (`plugins/security/plugin.py:96-100`). Recipe:

```bash
set -a && source deployment/.env && set +a

# 1. Done set: query Postgres for completed instance_ids
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

# 2. Target set: all fixtures
ls plugins/security/tests/fixtures/ | sed 's/\.json$//' | sort > /tmp/all.txt

# 3. Diff for the todo set
comm -23 /tmp/all.txt /tmp/done.txt > /tmp/todo.txt
echo "$(wc -l < /tmp/todo.txt) instances still to run"

# 4. Re-run only the missing ones
while read instance; do
  docker exec arise-app python main.py \
    -c config/experiment-qwen3-8b-local.yaml run \
    "Analyze and patch $instance." \
    --domain security \
    --domain-context-file "plugins/security/tests/fixtures/${instance}.json"
done < /tmp/todo.txt
```

---

## Phase 5 — Share outputs

The full state is **two pieces**:

1. **Postgres `events` table** — append-only event log; everything else replays from here. Schema: `infrastructure/sql/create_events_table.sql` (one table, no views).
2. **Host-side `runs/<BOSS_ID>/`** — already on the host via the compose bind mount `../runs:/app/runs`. Contains `testcase/{security_report.md, model_patch.diff, repro.sh, base_commit_hash, ...}` and worker logs.

### Sender side

```bash
set -a && source deployment/.env && set +a

# 1. Dump the event store (gzipped, with row count printed)
scripts/dump_events.sh    # writes events-YYYYMMDDTHHMMSSZ.sql.gz

# 2. Tar the run artifacts
tar -czf "runs-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" runs/

ls -lh events-*.sql.gz runs-*.tar.gz
```

Both files together = the complete shareable result set.

### Receiver side

After the receiver runs `docker compose --profile local up -d` once (schema auto-applies on first start):

```bash
# 1. Unpack run artifacts
tar -xzf runs-20260513T164000Z.tar.gz

# 2. Restore events
set -a && source deployment/.env && set +a

# Fresh DB:
scripts/restore_events.sh events-20260513T164000Z.sql.gz

# DB already has events — choose one:
scripts/restore_events.sh --truncate events-20260513T164000Z.sql.gz   # wipe + replace
scripts/restore_events.sh --append   events-20260513T164000Z.sql.gz   # merge (errors on collisions)
```

Open http://localhost:8000 — every imported BOSS_ID appears in the agent list.

---

## Reference — Paths and sources of truth

| Path | Role |
|---|---|
| Postgres `events` | **THE source of truth.** Append-only, OCC on `(aggregate_id, sequence_number)`. |
| `plugins/security/tests/fixtures/*.json` | Authoritative CVE instance list (311 entries after Phase 1). |
| `runs/<BOSS_ID>/testcase/` | Deliverables: `security_report.md`, `model_patch.diff`, `repro.sh`, `base_commit_hash` + worker logs. |
| `runs/<BOSS_ID>/src/` | Host mirror of the vulnerable source tree. |
| `runs/<BOSS_ID>/run_manifest.json` | Per-run metadata (`run_id`, `exit_status`, `models`, `summary_available`, `deliverables`). Does **not** carry `instance_id` — that lives in Postgres `RunStarted.domain_metadata`. |
| `/src` (in container) | Source tree the worker `cd`'s into. |
| `/testcase` (in container, **singular**) | Where workers write deliverables. NOT `/testcases`. |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `fetch_secbench.py`: `ModuleNotFoundError: datasets` | Use `uv run --with datasets python ...`. `datasets` is not in `pyproject.toml`. |
| `docker exec arise-app python main.py ...`: stale code | `docker compose --profile local up -d --build app` |
| Bulk build fails on `pull access denied` | Fixture's default `hwiwonlee/...` image doesn't exist for that CVE. Authoring a `songtli/...` image is in scope of the `/secbench-fixture` skill. |
| `dump_events.sh: POSTGRES_PASSWORD must be set` | `set -a && source deployment/.env && set +a` first, or pass `POSTGRES_PASSWORD=...` inline. |
| `restore_events.sh` aborts: existing rows | Pass `--truncate` (wipe + replace) or `--append` (merge — errors on UNIQUE collisions). |
| Run shows `failed` | `docker exec arise-app python main.py events --errors-only`. Re-run only that instance per Phase 4. |
| Worker container can't reach Ollama on Linux | `extra_hosts: ["host.docker.internal:host-gateway"]` (already set in `deployment/docker-compose.yml`). |
| Agent tree exploded past 25 nodes | Over-decomposition. See `README.md` §12; edit `prompts/domains/secbench/assess.j2`. |

---

## See also

- `README.md` — conceptual overview, single-instance run, dashboard tour
- `README.md` §11 — prompt hierarchy (where to edit security prompts)
- `agent-docs/secbench-pipeline-tech-doc.md` — image build pipeline internals
- `experiments/shared/scripts/run_matrix.py` — matrix runner for comparative studies
