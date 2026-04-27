---
generated_at: '2026-04-27T01:05:41.234879Z'
generated_by: experiments/2026-04-23-initial-secbench/scripts/render_report.py
inputs:
- experiments/2026-04-23-initial-secbench/manifest.yaml
- experiments/2026-04-23-initial-secbench/reports/tables/summary.csv
- experiments/2026-04-23-initial-secbench/reports/tables/run_metrics.csv
- experiments/2026-04-23-initial-secbench/reports/tables/totals.csv
- experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg
- experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml
- experiments/shared/groups.yaml
inputs_sha256:
  experiments/2026-04-23-initial-secbench/manifest.yaml: df1ddb5751a3e861541f3e083468e3ad2d41398adb2b1389089233b5874ea6bb
  experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml: 48f503b4ea2b7bbd117331e653209fa0515ab17281f55e42782609dd9d570e94
  experiments/2026-04-23-initial-secbench/reports/figures/cells-overview.svg: 7613f02355ee4435c322bcf96b76600fcb7d454a76788e08a9417686a2f8d86e
  experiments/2026-04-23-initial-secbench/reports/tables/run_metrics.csv: 650bec1d7ce51e04249808d2e5b411ebb0876c1f433c8762407d788177e63f7f
  experiments/2026-04-23-initial-secbench/reports/tables/summary.csv: 6985b62e3e8095f80336d0bc79fe726a4c473640e0416505aedaab61b85bd07d
  experiments/2026-04-23-initial-secbench/reports/tables/totals.csv: 6ed0787a722de75afdf92a4ed327ae242b839449efbddd8c1c34a1932c44c967
  experiments/2026-04-23-initial-secbench/templates/report.md.j2: c3b51ac5f422fac9882cc189bf246d164596e5667f46e3b672d734acd7dac4d8
  experiments/shared/groups.yaml: 33dbec395e1176f11aeeb5385f0baaff79eb37a48df89815fddef5de16e7504e
output_sha256: e0f7c149d9274f4f1bbf058960470c5c5c8f8759ea939a7a369e519be440d262
template: experiments/2026-04-23-initial-secbench/templates/report.md.j2
---

## Overview

Next SEC-bench matrix after removing no-CVE-context treatments. Every cell receives the CVE context and shared SEC-bench task framing; treatments compare Claude Code subagent topology, our Claude-Code-backed hierarchy with verifier off/on, and our Qwen/OpenHands hierarchy with verifier off/on under shared artifact and event metrics scripts.

## Cells

- **A1** (A — Claude Code CLI) — config `configs/A1-claude-code-subagent.yaml`
- **A2** (A — Claude Code CLI) — config `configs/A2-claude-code-nosubagent.yaml`
- **B1** (B — Our System) — config `configs/B1-ours-claude-noverifier.yaml`
- **B2** (B — Our System) — config `configs/B2-ours-claude-verifier.yaml`
- **C1** (C — Our System + Qwen) — config `configs/C1-qwen-noverifier.yaml`
- **C2** (C — Our System + Qwen) — config `configs/C2-qwen-verifier.yaml`

## Runs Summary

Total runs in the scripted summary: **0**.

![Cells overview](figures/cells-overview.svg)

| Cell | Runs | Deliverables | Tool calls | Tool results | Thinking events | Thinking chars | Tokens | Cost USD | Duration s |
|------|-----:|-------------:|-----------:|-------------:|----------------:|---------------:|-------:|---------:|-----------:|
| A1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |
| A2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |
| B1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |
| B2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |
| C1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |
| C2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.000000 | 0.000 |

Raw table: [`reports/tables/summary.csv`](tables/summary.csv)
Per-run metrics: [`reports/tables/run_metrics.csv`](tables/run_metrics.csv)
Totals: [`reports/tables/totals.csv`](tables/totals.csv)

## Artifact Archive

Run traces and testcase deliverables copied by `scripts/collect.py` live under
[`artifacts/`](../artifacts/), grouped by cell, task, replicate, and run id.

## Qualitative Notes

Use the archived `events.jsonl`, `stdout_stderr.log`, `run_manifest.json`, and
testcase files for qualitative review. The numeric table above is generated
from the same event traces.

## Reproducibility

Every file in this `reports/` tree carries provenance metadata (YAML
frontmatter for `.md`, `.generated.json` sidecar for binaries). Re-run
`python -m experiments.shared.scripts.validate_reports` at any time to
verify each output matches the recorded inputs.
