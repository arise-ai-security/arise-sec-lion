---
generated_at: '2026-05-18T06:50:15.408024Z'
generated_by: experiments/shared/scripts/run_matrix.py
inputs: []
inputs_sha256:
  experiments/shared/templates/matrix-summary.md.j2: 8ac041afab74b6ab20a5ac2c612087088e847d3b445a2745a4dee1bda508c75e
output_sha256: b00c0e3584f9c9888f91bd76738e4b86b3aed9ed1825081a799b49669e3090a0
template: experiments/shared/templates/matrix-summary.md.j2
---

# Matrix Summary — a12-batch-autogen

Rendered at 2026-05-18T06:50:15.407786Z.

## Totals

- Jobs total: **8**
- Succeeded: **4**
- Failed: **4**
- Replicates per (cell, task): **1**

## Filters

- Cells: A1
- Tasks: libarchive.cve-2017-14503, libheif.cve-2023-49460, libheif.cve-2023-49464, libiec61850.cve-2021-45769, libiec61850.cve-2023-27772, openjpeg.cve-2016-7445, php.cve-2017-12933, php.cve-2018-12882

## Per-Cell Breakdown

| Cell | Succeeded | Failed |
|------|----------:|-------:|| A1 | 4 | 4 |
## Failed Jobs

| Cell | Task | Replicate | Error |
|------|------|----------:|-------|| A1 | libarchive.cve-2017-14503 | 0 | inner run exit_status=timeout || A1 | libiec61850.cve-2023-27772 | 0 | inner run exit_status=timeout || A1 | php.cve-2017-12933 | 0 | inner run exit_status=timeout || A1 | php.cve-2018-12882 | 0 | inner run exit_status=timeout |
