---
generated_at: '2026-04-26T19:25:45.339228Z'
generated_by: experiments/2026-04-23-initial-secbench/scripts/render_report.py
inputs:
- experiments/2026-04-23-initial-secbench/manifest.yaml
- experiments/2026-04-23-initial-secbench/reports/tables/summary.csv
- experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg
- experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml
inputs_sha256:
  experiments/2026-04-23-initial-secbench/manifest.yaml: a8cebe0c2d65d48b27bb811c703ed158c85f07b25540dd9152f4f8993203e0f2
  experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml: db3e3dcd714c98212ee1f5e21112789c9f6dfed2f037ab241c1e56f474990259
  experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg: 690852336e7480a712b0c329f10426ea69599d7bd75d61d512dcb82516768609
  experiments/2026-04-23-initial-secbench/reports/tables/summary.csv: e7c3a6b2667498bda683f4368ec1e581a187788b194aa2dd44ebe7d7bdc83b63
  experiments/2026-04-23-initial-secbench/templates/report.md.j2: bfcc3884cde9f92720f085d7a69b73093a786a1ae82e9231c6f2552ab09f56de
output_sha256: cba77700498e1891ad8e9fbc1f5077fdd598667458cc3f2018631809a2379ca1
template: experiments/2026-04-23-initial-secbench/templates/report.md.j2
---

## Overview

Seed study produced by migrating dataset-final/ into the experiments/runs layout. Establishes the end-to-end shape (pinned configs, scripts-first reports, run enrollment) without claiming new experimental findings.

## Cells

- **A1** (baseline-claude-code-subagent) — config `configs/A1-claude-code-subagent.yaml`
- **A2** (baseline-claude-code-nosubagent) — config `configs/A2-claude-code-nosubagent.yaml`
- **B1** (ours) — config `configs/B1-ours-naive.yaml`
- **B2** (ours) — config `configs/B2-ours-cybersec.yaml`
- **B3** (ours) — config `configs/B3-ours-full.yaml`

## Runs Summary

Total runs enrolled: **56** (0 legacy,
56 live).

![Cells overview](figures/cells-overview.svg)

| Cell | Runs | Deliverables present |
|------|-----:|:--------------------:|| A1 | 12 | 0 || A2 | 10 | 0 || B1 | 16 | 0 || B2 | 18 | 0 || B3 | 0 | 0 |
Raw table: [`reports/tables/summary.csv`](tables/summary.csv)

## Reproducibility

Every file in this `reports/` tree carries provenance metadata (YAML
frontmatter for `.md`, `.generated.json` sidecar for binaries). Re-run
`python -m experiments.shared.scripts.validate_reports` at any time to
verify each output matches the recorded inputs.
