# 2026-04-23 Initial SEC-bench Study

Seed study produced by migrating `dataset-final/` into the new
experiments/runs layout. Demonstrates the end-to-end shape (manifest,
dataset, pinned configs, analysis scripts, scripts-first rendered report)
without claiming any new experimental findings — numbers are derived from
legacy runs migrated into `runs/_legacy/`.

## Layout

```
2026-04-23-initial-secbench/
├── manifest.yaml          # cells, dataset pointer, enrolled runs
├── dataset.yaml           # 10 CVEs + source.paths
├── configs/               # pinned configs for each cell (A1, A2, B1, B2, B3)
├── scripts/               # collect, render, analysis scripts
├── templates/             # report.md.j2
└── reports/               # generated tables, figures, report
    ├── report.md
    ├── tables/summary.csv
    └── figures/cells-overview.svg
```

## Regenerating the reports

```bash
python experiments/2026-04-23-initial-secbench/scripts/collect.py
python experiments/2026-04-23-initial-secbench/scripts/plot_success.py
python experiments/2026-04-23-initial-secbench/scripts/render_report.py
python -m experiments.shared.scripts.validate_reports
```

Every generated file carries either YAML frontmatter (for `.md`) or a
`.generated.json` sidecar entry (for binaries) proving it was produced by
a committed script from recorded inputs. The pre-commit hook
`validate-experiment-reports` enforces this on every commit.
