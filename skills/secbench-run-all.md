---
description: Run all SEC-bench CVE instances in batches, with resume support
argument-hint: [--batch-size 15] [--max-retries 3] [--status]
allowed-tools: [Read, Glob, Grep, Bash, Agent, TaskCreate, TaskUpdate]
---

# SEC-bench Run All

Runs all 200 CVE instances from the SEC-bench eval dataset in configurable batches (default 15 concurrent). Monitors for under-decomposition, auto-retries up to 3 times, and records every result to `~/secbench-all.csv`.

## Arguments

The user provided: $ARGUMENTS

- `--status` — just show current progress from CSV, don't launch anything.
- `--batch-size N` — override batch size (default 15).
- `--max-retries N` — override max retries for under-decomposed runs (default 3).

## Procedure

### Phase 1: Check resume state

Read `~/secbench-all.csv` if it exists and report progress:

```bash
wc -l ~/secbench-all.csv 2>/dev/null
# Breakdown by status
awk -F, 'NR>1 {s[$2]++} END {for(k in s) print k, s[k]}' ~/secbench-all.csv 2>/dev/null
```

If `--status` was passed, print the summary and stop.

### Phase 2: Pre-flight

1. Verify containers are running:
   ```bash
   cd /home/songli/arise-sec-lion/deployment && docker compose --profile local ps
   ```
2. Unless the user says otherwise, restart containers:
   ```bash
   cd /home/songli/arise-sec-lion/deployment && docker compose --profile local restart
   ```
3. Wait for health checks (db and api must be "healthy").

### Phase 3: Launch batch runner

Run the batch runner script in background:

```bash
cd /home/songli/arise-sec-lion && python scripts/batch_secbench.py \
  --batch-size <BATCH_SIZE> --max-retries <MAX_RETRIES> \
  2>&1 | tee ~/secbench-all.log
```

Use `run_in_background: true` for this command.

### Phase 4: Monitor progress

Every 2–5 minutes, check:

1. **CSV progress**:
   ```bash
   awk -F, 'NR>1 {s[$2]++} END {for(k in s) printf "%s: %d\n", k, s[k]}' ~/secbench-all.csv
   ```

2. **Active runs** (how many `python main.py run` processes inside arise-app):
   ```bash
   docker exec arise-app pgrep -f "main.py run" | wc -l
   ```

3. **Running containers**:
   ```bash
   docker ps --filter label=arise.root_id --format '{{.Names}}' | wc -l
   ```

4. **Tail log for errors**:
   ```bash
   tail -20 ~/secbench-all.log
   ```

Report a summary table to the user after each check.

### Phase 5: Post-run summary

After the script completes (no more `main.py run` processes), generate the final report:

```bash
echo "=== SEC-bench Full Run Summary ==="
echo "CSV: ~/secbench-all.csv"
echo ""
awk -F, 'NR>1 {s[$2]++; total++} END {
  for(k in s) printf "  %-20s %d (%.0f%%)\n", k, s[k], s[k]/total*100
  printf "  %-20s %d\n", "TOTAL", total
}' ~/secbench-all.csv
echo ""
echo "Under-decomposed retries:"
awk -F, 'NR>1 && $7>1 {printf "  %s: %s tries\n", $1, $7}' ~/secbench-all.csv
```

### CSV columns

| Column | Description |
|--------|-------------|
| `instance_id` | SEC-bench instance (e.g. `gpac.cve-2023-5586`) |
| `status` | `success`, `failed`, `under_decomposed`, `timeout`, `image_error`, `error` |
| `boss_id` | UUID of the BOSS agent for this run |
| `testcase_path` | Path to `runs/<boss_id>/testcase/` with deliverables |
| `num_agents` | Total agents in the hierarchy |
| `duration_seconds` | Wall-clock seconds |
| `tries` | Number of attempts (>1 means retried after under-decomposition) |
| `error_message` | Failure detail if applicable |
| `started_at` | ISO timestamp |
| `completed_at` | ISO timestamp |

### Resume behavior

The batch runner reads `~/secbench-all.csv` on startup and **skips** any instance with status `success` or `image_error`. All other statuses (`failed`, `under_decomposed`, `timeout`, `error`) will be re-attempted.

To force a full re-run, delete or rename the CSV first.

### Under-decomposition detection

A run is flagged as under-decomposed if:
- After 5 minutes of runtime, fewer than 8 agents exist in the hierarchy, OR
- The run completes with fewer than 8 agents regardless of exit status.

Under-decomposed runs are killed immediately and retried (up to `--max-retries`).
