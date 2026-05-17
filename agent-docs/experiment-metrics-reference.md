# Experiment Metrics Reference

Per-column documentation for the CSVs written by
`experiments/<study>/scripts/collect.py`:

- `reports/tables/run_metrics.csv` — one row per run.
- `reports/tables/summary.csv` — one row per cell (cohort across tasks/replicates).
- `reports/tables/totals.csv` — one row covering all cells.

Every value is derivable purely from data already on disk:
- The event log: `events.jsonl` projected from the Postgres event store.
- The run manifest: `run_manifest.json`.
- The testcase artifacts directory: `testcase/`.

No new event types were introduced; this reference reflects the schema as of
the 2026-05-12 postcondition extension.

---

## How the three files relate

For per-run boolean columns (e.g. `judge_passed`, `*_artifacts_present`,
`structural_check_passed`), the cell-level summary value is the count of runs
in that cell whose per-run value was 1.

For per-run integer counts (e.g. `tool_call_count`, `event_count`), the cell
total is the plain sum.

For per-run dict columns (`cost_by_model`, `cost_by_operation`,
`tool_calls_by_type`), the cell total merges keys: same-key values are
summed, distinct keys are unioned.

Sentinels (-1 for `run_duration_seconds`, `judge_score`, `total_agents`,
`completed_agents`, `failed_agents`) propagate stickily: a single -1 in a
cohort poisons the cell total so consumers cannot mistake "one run never
finished" for "the cohort ran in zero seconds."

---

## Identity / scope columns

| Column | Where | Description |
|---|---|---|
| `cell` | both | Cell name from `manifest.yaml` (e.g. `A1`, `B1`, `C1`). |
| `task` | run_metrics.csv | Task identifier (CVE id), e.g. `openjpeg.cve-2016-7445`. |
| `replicate` | run_metrics.csv | Replicate index (0-based). |
| `run_id` | run_metrics.csv | Run UUID. Primary key. |
| `runs` | summary.csv | Number of enrolled (projection_status != failed) runs in the cell. |
| `scope` | totals.csv | Always `all_cells` for the totals file. |

---

## Termination

| Column | Source | Aggregation | Values |
|---|---|---|---|
| `run_terminated_status` | `run_manifest.json["exit_status"]` | per-run: raw string; per-cell: count of runs with status `success` | **Type asymmetry**: the per-run column is a string (e.g. `success`/`failed`); the per-cell column is an integer (count of `success`). Same column name, different types — a downstream consumer summing the per-cell column gets "runs that succeeded", not a stringified status. The asymmetry is intentional to preserve a single field name across CSVs, but downstream analyses must read the documentation. Observed per-run values in 2026-05-12 data: `success`, `failed`. The original spec mentioned `completed/timeout/crashed`; neither variant appears in real data. The column passes the manifest value through verbatim. |
| `run_completed_count` | Count of `RunCompleted` events | sum | Normally 0 (sentinel: run terminated before completion event) or 1. >1 indicates an upstream bug emitting duplicate completion events; logged. |
| `run_duration_seconds` | `RunCompleted.duration_seconds` | sum (sticky -1.0 sentinel) | -1.0 when no `RunCompleted` observed. |

---

## Postcondition flags (smoke-test-2026-05-12 follow-up)

| Column | Source | Aggregation | Values | Threshold |
|---|---|---|---|---|
| `builder_artifacts_present` | filesystem: `testcase/` | per-cell sum of 1s | 0/1 per run. 1 iff `base_commit_hash` AND `packages.txt` AND (`repo_changes.diff` nonempty OR `src/build.sh` exists). | non-empty = stat().st_size > 0 |
| `exploiter_artifacts_present` | filesystem: `testcase/` | per-cell sum of 1s | 0/1 per run. 1 iff `repro.sh` nonempty AND at least one `poc.*` file nonempty. | non-empty = stat().st_size > 0 |
| `fixer_artifacts_present` | filesystem: `testcase/` | per-cell sum of 1s | 0/1 per run. 1 iff `model_patch.diff` nonempty. | non-empty = stat().st_size > 0 |
| `judge_ran` | `VerificationPassed` / `VerificationFailed` events with non-sentinel feedback | per-cell sum of 1s | 0/1 per run. 1 iff a real judge LLM call fired (feedback != `"Passed structural checks (no success criteria defined for judge evaluation)"`). | — |
| `judge_passed` | computed from `judge_ran` + real judge score | per-cell sum of 1s | 0/1 per run. 1 iff `judge_ran=1` AND real-judge-score >= 60. | `JUDGE_PASS_THRESHOLD = 60` in `run_metrics.py` — mirrors `verification_pipeline.py:87` so reports agree with runtime verdicts. |
| `structural_check_passed` | `VerificationPassed` with the structural-check sentinel feedback | per-cell sum of 1s | 0/1 per run. Surfaced separately from `judge_ran` so `score=100` from the fallback can never masquerade as a real verdict. |
| `judge_score` | `VerificationPassed.score` / `VerificationFailed(failed_stage="judge").score` | sum with sticky -1 sentinel | -1 when no judge-class event observed. **Important**: also takes the value `100` from the structural-check sentinel. Use `judge_ran` to disambiguate. |

---

## Run-level counts from `RunCompleted`

| Column | Source | Aggregation | Values |
|---|---|---|---|
| `total_agents` | `RunCompleted.total_agents` | sum with sticky -1 sentinel | -1 when no `RunCompleted` observed. |
| `completed_agents` | `RunCompleted.completed_agents` | sum with sticky -1 sentinel | Same. |
| `failed_agents` | `RunCompleted.failed_agents` | sum with sticky -1 sentinel | Same. |

---

## Retry / re-decomposition

| Column | Source | Aggregation | Values |
|---|---|---|---|
| `retry_count` | Count of `RetryScheduled` events | sum | Integer ≥ 0. |
| `redecomposition_count` | Count of `RedecompositionTriggered` events | sum | Integer ≥ 0. |

---

## Cost breakdown (JSON columns)

Both columns are emitted as JSON strings in the CSV so a consumer can
`json.loads(row["cost_by_model"])` to recover the dict.

| Column | Source | Aggregation | Values |
|---|---|---|---|
| `cost_by_model` | `WorkerCostRecorded.usage_metrics[*].(model, accumulated_cost_usd)` when present; else top-level `(WorkerCostRecorded.model, WorkerCostRecorded.cost_usd)`. Plus `TokensConsumed.(model, cost_usd)`. | merge same-keyed floats | `{model_name: cost_usd}`. Empty `{}` when no cost events recorded. |
| `cost_by_operation` | `TokensConsumed.(operation, cost_usd)` | merge same-keyed floats | `{operation: cost_usd}`. Observed operations: `task_assessment`, `task_decomposition`. (Original spec listed `complexity_evaluation` / `worker_execution`; neither appeared in 2026-05-12 events.) Empty `{}` when no `TokensConsumed` events. |

### Why the `usage_metrics` fallback matters

Empirically ~55% of `WorkerCostRecorded` events in the 2026-05-12 smoke
test had `usage_metrics=[]`. Following the spec literally (`sum from
usage_metrics`) would have produced 0 attribution for those events. The
fallback preserves correctness.

---

## Token counts

| Column | Source | Aggregation |
|---|---|---|
| `tokens_prompt` | `TokensConsumed.prompt_tokens` + `WorkerCostRecorded.prompt_tokens` | sum |
| `tokens_completion` | Same for `completion_tokens` | sum |
| `tokens_reasoning` | `WorkerCostRecorded.reasoning_tokens` | sum |
| `tokens_cache_read` | `WorkerCostRecorded.cache_read_tokens` | sum |
| `tokens_cache_write` | `WorkerCostRecorded.cache_write_tokens` | sum |
| `tokens_total` | `TokensConsumed.total_tokens` plus the audit-N-3 reconstructed sum (prompt+completion+cache_read+cache_write+reasoning) from `WorkerCostRecorded` | sum |

---

## Worker activity counts

| Column | Source | Aggregation |
|---|---|---|
| `event_count` | All events in the run | sum |
| `prompt_sent_count` | Count of `PromptSent` events | sum |
| `tool_call_count` | Count of `ThoughtCaptured` events with `output_type == "tool_use"` | sum |
| `probe_count` | Count of `ProbeStarted` events | sum |
| `tool_result_count` | Count of `ThoughtCaptured(output_type="tool_result")` + `ProbeCompleted` | sum |
| `thinking_event_count` | Count of `ThoughtCaptured(output_type="thinking")` | sum |
| `thinking_chars` | Total `len(content)` across thinking events | sum |
| `worker_output_event_count` | Count of `ThoughtCaptured` events (any output_type) | sum |
| `worker_cost_event_count` | Count of `WorkerCostRecorded` events | sum |
| `tool_calls_by_type` | `ThoughtCaptured.tool_name` (or `unknown`) | merge counts |
| `cheating_attempt_count` | Strict/narrow BUG-METRIC1 detector — see comments in `run_metrics.py` | sum |

---

## Cost columns (USD)

| Column | Source | Aggregation |
|---|---|---|
| `llm_cost_usd` | Sum of `TokensConsumed.cost_usd` | sum (rounded to 6dp) |
| `worker_cost_usd` | Sum of `WorkerCostRecorded.cost_usd` | sum (rounded to 6dp) |
| `total_cost_usd` | `llm_cost_usd + worker_cost_usd` rounded after summing | derived |

---

## Provenance / file pointers

| Column | Where | Description |
|---|---|---|
| `events_jsonl` | run_metrics.csv | Absolute or repo-relative path to the events.jsonl used to compute the row. |
| `artifacts_path` | run_metrics.csv | Repo-relative path to the copied `experiments/<study>/artifacts/...` directory. |
| `deliverables_present` | both | Count of `run_manifest.json["deliverables"]` entries set to true. (Already present pre-extension.) |
| `event_files` | summary.csv | Count of runs in the cell that have a readable `events.jsonl`. |
| `artifact_files_copied` | summary.csv | Count of files copied into the per-run artifacts directory. |

---

## Audit harness

`experiments/shared/scripts/metrics_audit.py` re-derives the per-run
counters above from a deliberately parallel `events.jsonl` reader and
compares to the CSV. Exit code 0 means every audited column matches; the
script does NOT share code with `metrics_from_events`, so a divergence
in either implementation will surface as a discrepancy rather than
remaining invisible.

Run:

```
python -m experiments.shared.scripts.metrics_audit --study <study-id>
```

The audit only covers the integer columns it can independently
recompute. Filesystem-derived booleans (`*_artifacts_present`,
`run_terminated_status`) and float costs (subject to rounding semantics)
are out of scope.
