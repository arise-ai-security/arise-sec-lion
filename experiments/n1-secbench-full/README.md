# N1 handoff

Run from the cloned repository root.

## 1. Environment and OpenAI key

```bash
python3 experiments/n1-secbench-full/setup.py
```

The committed `experiments/n1-secbench-full/.env.example` is the template. Setup creates
the ignored, study-only `experiments/n1-secbench-full/.env` and asks once for
`OPENAI_API_KEY`.

## 2. Change the model (optional)

Edit `experiments/n1-secbench-full/configs/N1-openhands-linear.yaml`. Change only
`worker.model`.

## 3. Run

```bash
python3 experiments/n1-secbench-full/run.py --parallel 2 --batch-size 30
```

This command prepares, runs, or resumes the experiment. Adjust `--parallel` and
`--batch-size` for the host.

Every invocation prints its log path and saves UTC-timestamped preparation and
run output under `_run_logs/n1-secbench-full/`. This is host-side diagnostic
logging only; it does not change event persistence or worker execution.
If the final summary reports failed tasks, the log also identifies each
canonical run and renders its persisted failure and lifecycle events, including
previously completed failures that were not rerun by the current invocation.

Smoke:

```bash
python3 experiments/n1-secbench-full/run.py --smoke --parallel 2 --batch-size 2
```

Force a fresh diagnostic attempt for an instance that already has a canonical
run:

```bash
python3 experiments/n1-secbench-full/run.py \
  --force \
  --parallel 2 \
  --batch-size 2 \
  --instances gpac.cve-2023-5586
```

`--force` requires `--instances` (or `--smoke`) so it cannot accidentally rerun
the full dataset. It provisions and executes a real additional attempt, including
normal model usage and cost. The command verifies that exactly one new
event-backed run was created per task and reports that attempt; ordinary
resume/export commands continue to use the original first launch as canonical.

## 4. Status

```bash
uv run python experiments/n1-secbench-full/status.py
uv run python experiments/n1-secbench-full/status.py --all   # full pending list
```

Prints done/pending counts, per-instance `exit_status` + `run_id`, key paths
(logs under `_run_logs/n1-secbench-full/`, manifests under `runs/<run_id>/`),
and the resume/export commands. Prefers event-backed records when Postgres is
up; otherwise falls back to filesystem manifests.

Re-run `run.py` to finish pending tasks only. Terminal failures stay done until
`--force --instances ...`. Kill mid-task → no terminal manifest → stays pending.

## 5. Export

After the run finishes:

```bash
python3 experiments/n1-secbench-full/export.py
```

The finished export is written to `~/n1-secbench-full-export`.
