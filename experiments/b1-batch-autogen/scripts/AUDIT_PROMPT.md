# Portable Audit Prompt — B1 Fix-Validator FP/FN/TP/TN

Drop this prompt into another Claude/GPT session (or hand it to an autonomous
agent) on a machine that has a clone of this repo with the `runs/` directory
populated. It will reproduce the FP / FN / TP / TN classification produced by
`experiments/b1-batch-autogen/reports/b1_fix_validator_audit_report.md`.

The agent must follow it literally — do **not** trust prose summaries inside
validation files, because those files are the artifacts under audit.

---

## Inputs (all paths are repo-relative; agent should resolve to absolute)

- `runs/` — directory of run artifacts. Each subdirectory is a UUID containing
  `run_manifest.json`, `effective_config.yaml`, `events.jsonl`, and a
  `testcase/` folder with the Phase 1-4 deliverables.
- `experiments/b1-batch-autogen/scripts/audit_fix_validator.py` — mechanical
  claim + evidence sweep.
- `experiments/b1-batch-autogen/scripts/audit_builder_success.py` — Builder
  success verifier (Phase 1).
- `experiments/b1-batch-autogen/scripts/audit_builder_locations.py` — lists
  every expected built executable per CVE (Phase 1 detail).
- `prompts/domains/secbench/flat_pipeline.j2` — the 4-phase contract the
  Worker was asked to follow. The validator file naming and the 3-run
  mandate are specified there.

## Definitions

For each unique B1 task (deduplicated by `(study_id, task)`):

- **Predicted PASS** — `patch_validation_results.txt` (or any of the
  fallback names — see `PATCH_V_NAMES` in `audit_fix_validator.py`) contains
  a heuristic PASS marker: `VERDICT: PASS`, `ALL TESTS PASSED`,
  `Validation Summary: ... PASS`, `PATCH VALIDATED`, etc. (See
  `RESULT_PASS_RE` in `audit_fix_validator.py`.)
- **Predicted FAIL** — same scan returns `VERDICT: FAIL`,
  `STILL CRASHES`, `DID NOT FIX`, etc. (See `RESULT_FAIL_RE`.)
- **Predicted AMBIGUOUS / MISSING** — neither a PASS nor a FAIL marker
  found in any of the candidate validation files.
- **Ground truth: fix works** — every post-patch evidence file
  (filename matches `audit_fix_validator.POST_PATCH_NAME_RE`) shows
  `exit 0` and zero ASan/MSan/UBSan ERROR lines, **and**
  `model_patch.diff` is non-empty.
- **Ground truth: fix fails** — at least one post-patch evidence file shows
  an `==N==ERROR: <Sanitizer>:` marker on the same bug class as the CVE.
- **Inconclusive** — no post-patch evidence file exists, or all candidate
  files predate `model_patch.diff` (i.e. they are pre-patch baselines that
  happened to be named ambiguously).

|                                | Truth: fix works | Truth: fix fails |
|--------------------------------|------------------|------------------|
| Predicted PASS                 | TP               | FP               |
| Predicted FAIL / AMBIG / MISS  | FN               | TN               |

## Procedure

### Step 1 — Mechanical sweep (no LLM judgment, fully deterministic)

```bash
python3 experiments/b1-batch-autogen/scripts/audit_fix_validator.py
```

This walks every run directory, dedupes to one entry per task, classifies the
verdict claim, and lists every post-patch text file containing a sanitizer
ERROR marker. Output:

- stdout: per-run table, FP-candidate listing, "claim PASS with no
  evidence" listing.
- `/tmp/b1_evidence_sweep.json`: per-run records with fields `task`,
  `run_id`, `exit_status`, `patch_claim`, `report_claim`, `patch_v_file`,
  `post_patch_san_files`, `pre_patch_san_files`, `other_san_files`.

### Step 2 — Deep-dive of each candidate (LLM judgment, required)

For every run where `post_patch_san_files` is non-empty AND
`patch_claim == "PASS"` OR `report_claim == "PASS"`, do the following:

1. Open `runs/<run_id>/testcase/model_patch.diff`. Confirm the patch is
   non-empty and modifies a function in the path the CVE report names.
   If `model_patch.diff` is missing or empty → mark **INCONCLUSIVE**.
2. For each file name in `post_patch_san_files`, stat its mtime and compare
   to `model_patch.diff`'s mtime. If the file is older than the patch, it
   is **a pre-patch baseline that happened to be named "post"**; ignore it.
3. For each file remaining, grep its contents for
   `==[0-9]+==ERROR:\s*(Address|Leak|Memory|Undefined|Thread)Sanitizer`.
   If the matched line is inside a section explicitly labelled
   `BEFORE PATCH`, `BASELINE`, `PRE-PATCH`, or appears in a code block
   quoted for context, ignore it.
4. If at least one post-patch file still has a live sanitizer ERROR after
   filtering, this run is **FP** (worker hallucinated success). Record:
   - the file name,
   - one full line of the ASan ERROR for the smoking gun,
   - which "switch tool" or "second validation" the worker performed to
     overwrite the failing verdict (this is the dominant FP pattern in B1).
5. Otherwise → **TP**. Record:
   - the file name that confirmed clean post-patch behavior,
   - one quoted line proving cleanliness (e.g. `ERROR SUMMARY: 0 errors`,
     `exit code: 0`, the patch's own throw/log message firing).

### Step 3 — FN sweep (symmetric)

For every run where `patch_claim != "PASS"` AND `report_claim != "PASS"`:

1. Open `model_patch.diff`. If missing/empty → not an FN candidate; skip.
2. List every post-patch evidence file using `POST_PATCH_NAME_RE`.
3. If at least one post-patch file shows zero sanitizer ERROR AND a clean
   exit (or the patch's own guard message) → **FN** (worker did fix it,
   prose was too weak for the scanner). Record file + quoted clean signal.
4. If every post-patch file still has a live sanitizer ERROR → **TN**
   (worker correctly recognised the fix failed).
5. If no post-patch file exists → **INCONCLUSIVE**.

### Step 4 — Report

Emit two tables:

```
| task | run_id (12c) | verdict (TP|FP|FN|TN|INCONCLUSIVE) | evidence_file | quoted_signal | notes |
```

and the confusion-matrix totals at the end:

```
                              Truth:fix_works   Truth:fix_fails
Predicted PASS                 TP=<n>            FP=<n>
Predicted FAIL/AMBIG/MISSING   FN=<n>            TN=<n>
Inconclusive                   I_works=<n>       I_fails=<n>
No binary/no patch (excl.)     -                 <n>
```

Compute Precision (`TP/(TP+FP)`), Recall (`TP/(TP+FN)`), F1, and the
hallucination rate (`FP/(TP+FP)`).

## Tips for the agent

- **Parallelise** the deep-dives. With ~50 FP candidates and ~30 FN
  candidates, spawn 3-4 parallel sub-agents each handling a chunk of
  runs. Aggregate at the end.
- **Don't read** non-text files (`*.bin`, `*.tiff`, `*.wav`, `*.pdf`,
  `*.mat`, `*.pgx`); they are inputs to the PoC, not evidence.
- **Don't trust** files larger than 5 MB as evidence — they are usually
  raw Valgrind dumps; the smoking gun is in smaller targeted ASan logs.
- **The dominant FP pattern in B1** is "switch tools when ASan refuses
  to go quiet" — when you see two adjacent validation files where the
  ASan log fails and a subsequent Valgrind/checklist log says PASS,
  treat that as FP regardless of the worker's overall verdict prose.
- **Pre-patch baseline files** sometimes have ambiguous names like
  `validation_repro_output.txt` — always compare timestamps to
  `model_patch.diff` to filter.
- **Output budget**: the final report should be a single Markdown table
  plus the confusion matrix. No prose padding.
