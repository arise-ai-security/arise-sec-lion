# EXPERIMENT_MANUAL.md

How to run a comparative **A/B/C experiment** on SEC-bench. Assumes the study folder (`experiments/<study-id>/`) already exists with `manifest.yaml`, `dataset.yaml`, and `configs/*.yaml` in place.

For the 300-task N1 run, use the three-command
[`experiments/n1-secbench-full` runbook](experiments/n1-secbench-full/README.md).

---

## 1. Vocabulary

| Term          | Meaning                                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------- |
| **Group**     | High-level approach class — single uppercase letter (`A`, `B`, `C`).                              |
| **Cell**      | A variant within a group (`A1`, `B2`, …). Independent variable. One config YAML per cell.         |
| **Task**      | One SEC-bench CVE instance (e.g. `gpac.cve-2021-40575`).                                          |
| **Replicate** | Repeated run of a `(cell, task)` pair for noise control. **Use 1.**                               |
| **Study**     | One matrix: `cells × tasks × replicates`. Lives at `experiments/<study-id>/`.                     |

Every `(cell, task, replicate)` mints a `BOSS_ID`, writes events to Postgres, and drops artifacts in `runs/<BOSS_ID>/`. Per-study reports land in `experiments/<study-id>/reports/`.

---

## 2. One-time setup

```bash
uv sync --frozen
cp deployment/.env.example deployment/.env   # non-secret local configuration only
docker compose --profile local up -d --build
```

Never put provider keys or shared database credentials in `deployment/.env`. Retrieve
personal provider credentials from Bitwarden at process launch and export them only into
that process environment. If `bw` is locked, stop and unlock it; do not use a plaintext
fallback.

Dashboard at http://localhost:8000.

---

## 3. Build only the images this study needs

For the 300-instance N1 roster, publish one CVE-independent `/opt` payload once:

```bash
deployment/publish-secbench-tools-payload.sh --push
```

This maintainer-only command packages Node, Claude Code, and the security MCP
runtime under `/opt`, validates them on the pinned N1 ABI, and publishes one
image. Pin the printed digest in `deployment/secbench-tools-payload.lock` before
handoff.

The N1 runner still assembles one image per CVE locally. On a final-image cache
miss it pulls that CVE's `hwiwonlee` base and links in the shared payload, without
reinstalling the heavy common tools. It runs and evicts images in bounded waves;
the tagged payload stays local and is reused for the full roster.

For small local studies, `deployment/build-all-images.sh` remains available and
uses the same payload automatically.

`deployment/build-all-images.sh` accepts fixture paths as positional args and skips images already on disk. Feed it the fixtures listed in your study's `dataset.yaml`:

```bash
STUDY=<study-id>

grep -E '^  - plugins/security/tests/fixtures/' \
  "experiments/${STUDY}/dataset.yaml" \
  | awk '{print $2}' \
  | xargs deployment/build-all-images.sh -j 4
```

- Tags each image `secb-tools:<instance_id>-patch`.
- `-j 4` builds four concurrently; drop to `-j 2` if the host is constrained.
- Add `--dry-run` to preview targets without invoking docker.
- Add `--fail-fast` to stop on the first failure.

---

## 4. Run the matrix

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study "${STUDY}" \
  --parallel 4 \
  --replicates 1
```

The driver loads `manifest.yaml`, sweeps stale state, enumerates `cells × tasks × replicates`, dispatches via the `arise` runner (`main.py run` inside `arise-app`), and writes `experiments/${STUDY}/reports/{matrix-summary.md, enrollment.lock.yaml}`.

For the current development treatments:

```bash
experiments/b4-boss-manager-worker/smoke.sh
experiments/b3-direct-compact/smoke.sh
```

The N1-vs-B4 confirmatory study uses its separate paired, randomized, interleaved
runner. Its final roster must be untouched, but the current draft candidate roster
overlaps development enrollments and must be replaced before freezing. The runner
reads the seed, replicate count, and bootstrap count from `preregistration.yaml`; CLI
values cannot override them:

```bash
uv run python -m experiments.shared.evaluation.confirmatory_runner \
  --study b4-boss-manager-worker/confirmatory
```

The confirmatory preregistration is draft and unfrozen. Do not run that command until a real
Postgres-backed development run validates B4's required-failure recovery path, the
development pilot is complete, the cohort overlap is removed, and all three pinned semantic
judge seats pass live smoke.
The runner itself rejects cohort, oracle, regression-plan, arm, and preregistration drift
before the first launch.

### Useful flags

| Flag                                | Purpose                                                                          |
| ----------------------------------- | -------------------------------------------------------------------------------- |
| `--cells A1,B2`                     | Run only these cells.                                                            |
| `--tasks gpac.cve-2021-40575,…`     | Run only these tasks.                                                            |
| `--no-continue-on-error`            | Stop on first failure (default: continue).                                       |
| `--dry-run`                         | List selected jobs, dispatch nothing.                                            |

### Resuming a partial run

`run_matrix` re-runs every selected job — it's not idempotent at `(cell, task, replicate)`. To resume only gaps, read the lockfile and pass explicit `--cells` / `--tasks`:

```bash
cat "experiments/${STUDY}/reports/enrollment.lock.yaml"
uv run python -m experiments.shared.scripts.run_matrix \
  --study "${STUDY}" --cells B2 --tasks gpac.cve-2021-40575 --replicates 1
```

---

## 5. Inspect results

| Path                                                  | Contents                                                            |
| ----------------------------------------------------- | ------------------------------------------------------------------- |
| `experiments/<study>/reports/matrix-summary.md`       | Per-cell success/failure counts + failed-jobs table.                |
| `experiments/<study>/reports/enrollment.lock.yaml`    | `(cell, task, replicate) → run_id` mapping.                         |
| `runs/<run_id>/testcase/`                             | `security_report.md`, `model_patch.diff`, `repro.sh`, worker logs.  |
| `runs/<run_id>/run_manifest.json`                     | Run metadata (`exit_status`, `models`, `deliverables`).             |
| Postgres `events` table                              | Authoritative event stream; no `events.jsonl` projection is used.  |

CLI views from inside `arise-app`:

```bash
docker exec arise-app python main.py list --limit 20
docker exec arise-app python main.py events --errors-only
docker exec arise-app python main.py summary
```

Dashboard: http://localhost:8000.

---

## 6. Share results

The full state is **Postgres `events`** (source of truth) + **`runs/<run_id>/`** (deliverables).

```bash
# Sender
scripts/dump_events.sh                                          # events-YYYYMMDDTHHMMSSZ.sql.gz
tar -czf "runs-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" runs/
tar -czf "study-${STUDY}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" "experiments/${STUDY}/"

# Receiver (after docker compose --profile local up -d)
tar -xzf runs-*.tar.gz && tar -xzf study-*.tar.gz
scripts/restore_events.sh events-*.sql.gz                       # fresh DB
# or: --truncate (wipe + replace) / --append (merge)
```

---

## 7. Troubleshooting

| Symptom                                              | Fix                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Bulk build: `pull access denied`                     | Fixture's default `hwiwonlee/…` image doesn't exist. Author a `songtli/…` override (`/secbench-fixture` skill). |
| `run_matrix`: stale code / docker exec error         | `docker compose --profile local up -d --build app` to rebuild the app container.                 |
| `dump_events.sh`: `postgres-main` is not running    | `docker compose -f deployment/docker-compose.yml --profile local up -d db`.                      |
| `restore_events.sh` aborts on existing rows          | Pass `--truncate` (wipe + replace) or `--append` (merge).                                        |
| Worker can't reach Ollama on Linux                   | `extra_hosts: ["host.docker.internal:host-gateway"]` is already in `deployment/docker-compose.yml`. |
| `--parallel` saturates the host                      | See `agent-docs/parallel-20-host-saturation-diagnosis.md`. Drop to `--parallel 2`.               |

---

## See also

- `README.md` — single-instance debugging, dashboard tour
- `agent-docs/secbench-pipeline-tech-doc.md` — image-build pipeline internals
- `research/02-experimental-design.md` — hypotheses, IVs, DVs, threats to validity
