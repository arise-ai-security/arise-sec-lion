---
generated_at: '2026-04-26T20:12:44.322133Z'
generated_by: experiments/2026-04-23-initial-secbench/scripts/render_report.py
inputs:
- experiments/2026-04-23-initial-secbench/manifest.yaml
- experiments/2026-04-23-initial-secbench/reports/tables/summary.csv
- experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg
- experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml
- experiments/shared/groups.yaml
inputs_sha256:
  experiments/2026-04-23-initial-secbench/manifest.yaml: e439bd137d980a1d44daf0819d49314766f8d5ad8ba24c3f6b7409747db52a6b
  experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml: 6470ffce8e354370b9aa7812eac49c4910c6d72e915f4d965320b6357b27a2df
  experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg: 690852336e7480a712b0c329f10426ea69599d7bd75d61d512dcb82516768609
  experiments/2026-04-23-initial-secbench/reports/tables/summary.csv: e7c3a6b2667498bda683f4368ec1e581a187788b194aa2dd44ebe7d7bdc83b63
  experiments/2026-04-23-initial-secbench/templates/report.md.j2: ab997f70578b17162515763a435b6ceeb0b6d74be78c3486a22b417c8e810630
  experiments/shared/groups.yaml: 80fdda7122b3a2bdaa059638055cbe7a720bb2810ec6c3f004aca0a01ca385d6
output_sha256: 6f0eaa0234193bba77aeaaba497436d65e50c5fcc752d472cf7e4b2c4bbb810a
template: experiments/2026-04-23-initial-secbench/templates/report.md.j2
---

## Overview

Seed study produced by migrating dataset-final/ into the experiments/runs layout. Establishes the end-to-end shape (pinned configs, scripts-first reports, run enrollment) without claiming new experimental findings.

## Cells

- **A1** (A — Claude Code CLI) — config `configs/A1-claude-code-subagent.yaml`
- **A2** (A — Claude Code CLI) — config `configs/A2-claude-code-nosubagent.yaml`
- **B1** (B — Our System) — config `configs/B1-ours-naive.yaml`
- **B2** (B — Our System) — config `configs/B2-ours-cybersec.yaml`
- **B3** (B — Our System) — config `configs/B3-ours-full.yaml`

## Runs Summary

Total runs enrolled: **56**.

![Cells overview](figures/cells-overview.svg)

| Cell | Runs | Deliverables present |
|------|-----:|:--------------------:|| A1 | 12 | 0 || A2 | 10 | 0 || B1 | 16 | 0 || B2 | 18 | 0 || B3 | 0 | 0 |
Raw table: [`reports/tables/summary.csv`](tables/summary.csv)

## Reproducibility

Every file in this `reports/` tree carries provenance metadata (YAML
frontmatter for `.md`, `.generated.json` sidecar for binaries). Re-run
`python -m experiments.shared.scripts.validate_reports` at any time to
verify each output matches the recorded inputs.
