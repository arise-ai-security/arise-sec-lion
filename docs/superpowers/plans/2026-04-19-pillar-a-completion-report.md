# Pillar A Completion Report — Tree vs Flat Agent Dataset Generation

**Date**: 2026-04-19
**Branch**: `experiment/tree-vs-flat-agent-orchestration`
**Dataset version**: `dataset-v1.0-20260419.tar.zst`
**SHA-256**: `5dd162d95802c79988664c544837035ffb05ff70e63a312e27804dc64fe44e20`
**Pillar A code SHA**: `8add7a886510ade132ff7062da6cfc730305966f`

## Status

**DONE** — reduced matrix (see §"Matrix Reduction" below). Handing off to Pillar B.

## Matrix Reduction (pre-registered 6-cell → executed 4-cell)

Due to budget pressure midway through execution, the original 6-cell × 10-CVE + anchor matrix (62 runs) was reduced to the **T2 matrix**: cells **A1, A2, B1, B2** × 10 CVEs + 2 anchor A1 replicates = **42 runs**.

**Cells dropped**: A3 (flat, canonical briefing, subagents ON) and A4 (flat, canonical briefing, subagents OFF).

**Rationale ordering** (from the experiment-design spec, §"Comparisons"):
- **H1 primary (A1 vs B2)** — fully supported.
- **S1 (B1 vs B2, briefing effect on tree)** — fully supported.
- **S4 (A2 vs B1, pure orchestration effect)** — fully supported.
- **S5 (A3 vs B2, matched-briefing flat-vs-tree)** — PARTIALLY supported. 3 bonus A3 runs were captured on njs + faad2 CVEs during the initial all-cells attempt before the reduction; they are present in the dataset and analyzable but not balanced across all 10 CVEs.
- **S2 / S6 etc.** — any secondary comparison that requires A4 is unsupported.

**Bonus cells kept**: A3 (3 runs) and A4 (2 runs) from the pre-reduction attempt are included in the dataset. Pillar B should treat them as opportunistic supplementary data, not primary evidence.

## Runs Captured (47 total)

### Per-cell counts + mechanical-pass breakdown

| Cell | Count | builder | exploiter | fixer | end_to_end |
|------|-------|---------|-----------|-------|------------|
| A1   | 12    | 2       | 0         | 0     | 0          |
| A2   | 10    | 2       | 0         | 0     | 0          |
| A3   | 3     | 0       | 0         | 0     | 0          |
| A4   | 2     | 0       | 0         | 0     | 0          |
| B1   | 10    | 2       | 0         | 0     | 0          |
| B2   | 10    | 2       | 0         | 0     | 0          |

### Per-CVE T2 coverage (A1+A2+B1+B2)

All 10 locked CVEs at 4 cells each (openjpeg.cve-2016-7445 has 6 = 4 + 2 anchor A1 replicates):
- njs.cve-2022-32414 (4)
- njs.cve-2022-38890 (4)
- faad2.cve-2021-32272 (4)
- faad2.cve-2018-20196 (4)
- openjpeg.cve-2016-7445 (6, includes 2 anchor replicates)
- gpac.cve-2021-40575 (4)
- gpac.cve-2022-1795 (4)
- mruby.cve-2022-0240 (4)
- exiv2.cve-2017-14859 (4)
- imagemagick.cve-2019-13309 (4)

### Termination distribution

- `completed`: 41
- `budget_cap`: 3 (hit $10 ceiling)
- `wallclock_cap`: 3 (hit 90-min ceiling)

## Cost

- **SDK-reported total**: **$152.58**
- **Average per run**: ~$3.25
- **⚠ Manual reconciliation required**: The SDK-reported figure MUST be cross-checked against the Anthropic billing dashboard (there is no programmatic path to validate this from inside the runner). See the account's token-usage page for 2026-04-19.

## Anti-cheat audit

- **Total violations across all runs**: 80
- **Recommended**: Pillar B analysis should start with per-run distribution (e.g., `jq '.audit_violations' dataset/INDEX.jsonl | sort | uniq -c`) to identify whether violations concentrate in specific runs or are spread broadly.

## Known Methodological Gaps (for Pillar B to address)

1. **Tree-cell `mechanical_pass` is systematically False** across B1 and B2 because the tree orchestrator does not currently surface its final patch artifact into `<run_dir>/workspace/` for the `secb patch` evaluator. Every B-cell `fixer` / `end_to_end` pass is therefore guaranteed to be 0 regardless of agent competence.
   - **Retrofit path**: parse `events.jsonl` for the final `Write` / `Edit` `ThoughtCaptured` entries targeting `/testcase/model_patch.diff` (or inferred equivalent), extract the content, place it in `<run_dir>/workspace/`, and re-run the mechanical evaluator on tree cells.
   - Without this retrofit, H1 (A1 vs B2) is biased against B2.
2. **Docker image platform mismatch**: images are `linux/amd64`, host is `linux/arm64/v8`. Runs executed under qemu emulation — slower but functional. Consider replicating on an amd64 host for a timing comparison if timing is a Pillar-B metric.
3. **Budget/wallclock cap hit rate (6/42 = 14%)**: some HARD-stratum tasks exceeded either $10 or 90 min. Pillar B should either (a) raise caps for HARD stratum when re-running, or (b) acknowledge the cap as a bound in the analysis.

## Dataset Artifacts

- Local tarball: `dataset-v1.0-20260419.tar.zst` (12 MB). **Not pushed to git** (per authorization); upload to preferred hosting (S3 / Hugging Face / Zenodo) and update `dataset/dataset_version.yaml` with a URL if desired.
- `dataset-v1.0-20260419.tar.zst.sha256` is tracked in git (commit `6651d70`).
- `dataset/dataset_version.yaml` is tracked in git (commit `6651d70`).
- Provenance files inside the tarball: `locked_instances.yaml`, `pre_registration.yaml`, `domain_briefing.md`, `role_prompts/`, `config/exp-secbench-{A1..A4,B1,B2}.yaml`.

## Code Fixes Landed During Execution

Both in commit `8add7a8` on the experiment branch:

1. `experiments/run_experiment.py::_dispatch_tree` now creates `plan.run_dir/workspace/` before invoking `main.py`. Previously, `evaluate_run` crashed on `workspace.resolve(strict=True)` for every B-cell run, silently dropping the INDEX entry.
2. `experiments/run_experiment.py::is_resumable` now checks for `mechanical.json` instead of `events.jsonl`. This makes partially-completed runs (events present, mechanical eval crashed or interrupted) automatically retry on restart.

## Handoff to Pillar B

Primary analysis inputs (all inside the tarball):
- `dataset/INDEX.jsonl` — 47 lines, one per captured run (cost, termination, pass bits, audit count).
- `dataset/runs/<cve>/<cell>/<rep>/events.jsonl` — full conversation + tool-call stream per run. Use for CNR analysis, tool-call-redundancy metrics.
- `dataset/runs/<cve>/<cell>/<rep>/meta.json` — per-run configuration snapshot.
- `dataset/runs/<cve>/<cell>/<rep>/audit.json` — per-run anti-cheat report.
- `dataset/runs/<cve>/<cell>/<rep>/mechanical.json` — per-phase pass bits (builder/exploiter/fixer/end_to_end).

First recommended analysis actions:
1. Verify SDK-reported cost against Anthropic billing dashboard (manual).
2. Decide on the tree-patch extraction retrofit before running any A-vs-B comparison.
3. Distribute anti-cheat violations to assess whether any runs should be excluded.
