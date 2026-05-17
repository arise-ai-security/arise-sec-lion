---
generated_at: '2026-05-17T03:40:11.777624Z'
generated_by: experiments/shared/scripts/run_matrix.py
inputs: []
inputs_sha256:
  experiments/shared/templates/matrix-summary.md.j2: 8ac041afab74b6ab20a5ac2c612087088e847d3b445a2745a4dee1bda508c75e
output_sha256: 8619c3cd35c1bcda96bf6e2c9b06e72b6593e1b4c036d55c8c5f357251a1e1b9
template: experiments/shared/templates/matrix-summary.md.j2
---

# Matrix Summary — b1-batch-autogen

Rendered at 2026-05-17T03:40:11.777208Z.

## Totals

- Jobs total: **11**
- Succeeded: **3**
- Failed: **8**
- Replicates per (cell, task): **1**

## Filters

- Cells: B1
- Tasks: libredwg.cve-2021-39521, libredwg.cve-2021-42585, libredwg.cve-2022-33034, libredwg.cve-2022-45332, libredwg.cve-2023-36273, mruby.cve-2018-10199, mruby.cve-2018-11743, mruby.cve-2018-12247, mruby.cve-2018-12248, mruby.cve-2018-12249, mruby.cve-2018-14337

## Per-Cell Breakdown

| Cell | Succeeded | Failed |
|------|----------:|-------:|| B1 | 3 | 8 |
## Failed Jobs

| Cell | Task | Replicate | Error |
|------|------|----------:|-------|| B1 | libredwg.cve-2021-39521 | 0 | inner run exit_status=timeout || B1 | libredwg.cve-2021-42585 | 0 | inner run exit_status=timeout || B1 | libredwg.cve-2022-33034 | 0 | inner run exit_status=timeout || B1 | libredwg.cve-2022-45332 | 0 | inner run exit_status=timeout || B1 | libredwg.cve-2023-36273 | 0 | inner run exit_status=timeout || B1 | mruby.cve-2018-10199 | 0 | inner run exit_status=timeout || B1 | mruby.cve-2018-12248 | 0 | inner run exit_status=failed || B1 | mruby.cve-2018-14337 | 0 | inner run exit_status=failed |
