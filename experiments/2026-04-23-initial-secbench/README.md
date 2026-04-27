# 2026-04-23 Initial SEC-bench Study

SEC-bench study for the next matrix. Every cell receives the CVE context and
the same SEC-bench task framing; treatments vary Claude Code topology, our
hierarchy, verifier use, and Qwen/OpenHands workers.

## Layout

```
2026-04-23-initial-secbench/
├── manifest.yaml          # cells, dataset pointer, replicates
├── dataset.yaml           # 10 CVEs + source.paths
├── configs/               # pinned configs for each cell (A1, A2, B1, B2, C1, C2)
├── scripts/               # collect, render, analysis scripts
├── templates/             # report.md.j2
├── artifacts/             # copied run traces and testcase artifacts
└── reports/               # generated tables, figures, report
    ├── report.md
    ├── tables/{summary,run_metrics,totals}.csv
    └── figures/cells-overview.svg
```

## Matrix

- **A1**: Claude Code CLI, Task/subagents allowed, using global Claude config.
- **A2**: Claude Code CLI, `Task` blocked, using global Claude config.
- **B1**: Our hierarchy with Claude Code workers, judge disabled.
- **B2**: Our hierarchy with Claude Code workers, judge enabled.
- **C1**: Our hierarchy with Qwen via OpenHands, judge disabled.
- **C2**: Our hierarchy with Qwen via OpenHands, judge enabled.

The “no CVE info” condition is intentionally removed. With `replicates: 1`
and 10 CVEs in `dataset.yaml`, a full run is **10 instances per cell**,
or 60 jobs across all six cells. Setting `--replicates 10` would mean 100
jobs per cell and 600 total jobs.

## Running

Validate the study definition:

```bash
uv run python -m experiments.shared.scripts.validate_manifest --study 2026-04-23-initial-secbench
```

Smoke one instance for each suite family:

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1,B1,C1 \
  --tasks openjpeg.cve-2016-7445 \
  --replicates 1 \
  --parallel 1 \
  --dry-run
```

Run that smoke for real:

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1,B1,C1 \
  --tasks openjpeg.cve-2016-7445 \
  --replicates 1 \
  --parallel 1 \
  --no-render \
  --no-continue-on-error
```

Run the full 10-instances-per-cell matrix:

```bash
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-04-23-initial-secbench \
  --cells A1,A2,B1,B2,C1,C2 \
  --replicates 1 \
  --parallel 1
```

## Regenerating the reports

```bash
uv run python experiments/2026-04-23-initial-secbench/scripts/collect.py
uv run python experiments/2026-04-23-initial-secbench/scripts/plot_success.py
uv run python experiments/2026-04-23-initial-secbench/scripts/render_report.py
uv run python -m experiments.shared.scripts.validate_reports --study 2026-04-23-initial-secbench
```

Every generated file carries either YAML frontmatter (for `.md`) or a
`.generated.json` sidecar entry (for binaries) proving it was produced by
a committed script from recorded inputs. The pre-commit hook
`validate-experiment-reports` enforces this on every commit.
