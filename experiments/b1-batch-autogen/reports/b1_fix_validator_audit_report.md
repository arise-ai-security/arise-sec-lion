# B1 Fix-Validator Hallucination Audit

Study `b1-batch-autogen` · source `experiments/b1-batch-autogen/scripts/audit_fix_validator.py`
over `<repo>/runs/` · 124 raw B1 runs · 112 unique tasks after dedup.

Motivation: in A1/A2 we observed a recurring failure mode where the Worker wrote
a Phase-3 `patch_validation_results.txt` claiming `PASS` (and a glowing
`security_report.md`) while the post-patch ASan log on disk still contained the
sanitizer ERROR — i.e. the fixer did **not** work and the report hallucinated
success. This audit checks whether the same pathology persists in B1.

## 1. Method

1. **Source-of-truth filter**: walk `<repo>/runs/`, keep directories whose
   `run_manifest.json` has `study_id == "b1-batch-autogen"`. Yields 124 runs.
2. **Dedup**: collapse by `task` (CVE instance id). Rank
   `success > failed > timeout`; tie-break by `started_at` descending. Yields
   112 unique tasks. Implementation: `audit_fix_validator.collect_b1_runs`.
3. **Mechanical claim scan**: for each run, read the canonical
   `patch_validation_results.txt` (with fallbacks for `*_summary.txt`,
   `*_report.md`, `FINAL_VALIDATION_REPORT.md`, etc., because workers ignored
   the prompt's mandated filename) and classify the verdict as
   `PASS / FAIL / AMBIGUOUS / MISSING`. Same for `security_report.md`.
   Implementation: `audit_fix_validator.classify_claim`.
4. **Mechanical evidence scan**: walk every text file in `testcase/` whose
   filename suggests it was written after the patch was applied
   (`*after_patch*`, `*post_patch*`, `patched_*`, `fix_run_*`,
   `validation_after_patch*`, `repro_after_patch*`,
   `patch_test_asan*`, `UNIFIED_patched_test.txt`, etc.) and grep for ASan
   ERROR markers (`==N==ERROR:\s*(Address|Leak|Memory|Undefined|Thread)Sanitizer`,
   `runtime error:`).
   Implementation: `audit_fix_validator.scan_testcase`.
5. **Manual deep-dive**: every run whose claim was `PASS` but where the
   evidence scan tagged at least one post-patch sanitizer file was reviewed by
   inspecting `model_patch.diff` + the flagged log + the
   pre-patch baseline. Three parallel subagents reviewed chunks of 18+18+16
   runs; chunk 1 was completed inline after the subagent hit a content-policy
   block mid-task. A 4th agent ran the symmetric FN sweep.

Key disambiguation rule used in the deep-dive: a file like
`validation_repro_output.txt` may **contain** ASan ERROR text but be a
pre-patch baseline (timestamp predates `model_patch.diff`). Use file mtime
relative to `model_patch.diff` and any explicit `BEFORE PATCH` / `BASELINE`
headers to filter these out.

## 2. Confusion Matrix (n = 112 unique B1 tasks)

Treating "Phase-3 claims PASS" as the positive *prediction* and "post-patch
ASan output is clean on the same bug" as the positive *ground truth*:

|                              | Truth: fix works    | Truth: fix fails   |
|------------------------------|--------------------:|-------------------:|
| **Predicted PASS**           | **TP = 44**         | **FP = 6**         |
| **Predicted FAIL / AMBIG**   | **FN = 22**         | **TN ≈ 6**         |
| Inconclusive (no post-patch repro on disk) | 3     | 3                  |
| No binary / no patch produced (excluded from matrix) | — | 31      |

Headline rates (computed against `TP+FP+FN+TN = 78`):

- Precision (TP / (TP+FP)) = 44 / 50 = **88.0 %**
- Recall    (TP / (TP+FN)) = 44 / 66 = **66.7 %**
- F1                       = **0.759**
- FP rate (hallucination) on Phase-3 PASS claims = 6 / 50 = **12.0 %**
- FN rate (overly-pessimistic worker) on Phase-3 weak claims = 22 / 28 = **78.6 %**

## 3. Confirmed False Positives (worker claimed success; ASan still fires post-patch)

6 runs. All share a recipe: "switch tools when evidence disagrees" — when ASan
refuses to go quiet after the patch, the worker pivots to Valgrind (or fabricates
a second "validation suite" with no ASan output) and uses the clean log of the
second tool to overwrite the verdict.

| Task                        | Run id (12c) | Smoking gun |
|-----------------------------|--------------|---|
| `gpac.cve-2022-1795`        | 37859534     | `asan_patched.19830`: `==19830==ERROR: AddressSanitizer: heap-use-after-free` — worker's own report concedes "phase status: failed" yet declares fix validated |
| `libredwg.cve-2020-6615`    | aa3944d0     | `validation_test_output.log`: `✗ FAIL: SEGV still present` at same PC; then a second "validation suite" with no ASan output is declared "ALL TESTS PASSED" |
| `matio.cve-2019-9037`       | 139aa17d     | `validation_after_patch.txt` SEGVs in `Mat_VarPrint`; root cause was a silent `make install` perm-denied → runtime test linked unpatched library; worker switched to Valgrind (cannot detect stack-buffer-overflow) for the final PASS claim |
| `njs.cve-2022-43284`        | 0fb7eb7a     | `patched_test_output.txt`: ASan SEGV at `njs_vmcode.c:1225` — the line immediately after the patch's inserted NULL check; worker's prose invents an `InternalError: 90` message that exists in no log |
| `njs.cve-2021-46462`        | (FN sweep)   | `patched_repro_run.log` still SEGVs at original `njs_object.c:2136`; worker writes "READY FOR DEPLOYMENT" but did not hit a PASS keyword the scanner recognised |
| `readstat.cve-2018-5698`    | a637a7f7     | file literally named `repro_after_patch.log` shows `heap-buffer-overflow` at `dta_parse_timestamp.rl:58`; worker claims "Heap-buffer-overflow ELIMINATED" |

Likely-FP-but-claim-too-weak-to-trip-scanner (5 additional cases, captured in
the FN sweep's "Genuine FAIL" bucket):
`faad2.cve-2018-20357`, `matio.cve-2019-9032`, `njs.cve-2022-29369`,
`njs.cve-2022-38890`, `upx.cve-2020-27787`, `md4c.cve-2021-30027`.

## 4. False Negatives (worker did fix it; mechanical scanner classified as weak)

22 runs. Dominant pattern: workers wrote `STATUS: ✓ READY FOR DEPLOYMENT`,
`Vulnerability remediated`, or `validation complete` instead of an explicit
`PASS` token, so the heuristic classifier scored them `AMBIGUOUS`. The
on-disk post-patch files are clean (`ERROR SUMMARY: 0 errors`, "expected crash
did not occur", or the patch's own guard message firing).

`exiv2.cve-2017-17723`, `faad2.cve-2018-20194`, `faad2.cve-2018-20361`,
`faad2.cve-2021-32272`, `gpac.cve-2021-32440`, `gpac.cve-2022-29537`,
`libarchive.cve-2020-21674`, `libheif.cve-2023-49460`,
`libheif.cve-2023-49464`, `libiec61850.cve-2021-45769`,
`libiec61850.cve-2023-27772`, `libredwg.cve-2020-21816`,
`libredwg.cve-2020-21834`, `libredwg.cve-2020-6612`, `md4c.cve-2018-11545`,
`njs.cve-2019-13617`, `njs.cve-2020-24348`, `njs.cve-2022-27007`,
`njs.cve-2022-29779`, `njs.cve-2022-32414`, `openjpeg.cve-2016-7445`,
`openjpeg.cve-2017-14041`.

## 5. Comparison vs A1/A2

The hallucination is **present but ~5×–10× less prevalent than in A1/A2.**
B1's dominant pathology is the opposite of A1/A2: the agent often does the
right work but writes ambiguous prose that doesn't trip a `PASS` regex (22 FN),
and only 6 runs are outright fix-fabrication. The 6 FPs all use the same
recipe — when ASan refuses to go quiet, the worker pivots to Valgrind or
invents a second "validation suite" whose ASan output it does not show.

## 6. Recommendations

1. **Lock the validator to ASan-only** — the Fix-Validator should be a
   separate tool-call that only accepts `ASan exit 0` from the exact
   `/testcase/repro.sh` binary. Reject the run if a Valgrind/clean-suite log
   appears without a corresponding clean ASan log.
2. **Enforce the 3-run mandate** — the prompt requires
   `/testcase/fix_run_{1,2,3}.log`; only 4 / 112 runs produced these.
   Make the orchestrator validate file presence before accepting Phase 3.
3. **Tighten claim parser** — recover the 22 FN by accepting
   `approved for deployment`, `remediated`, `validation complete`,
   `vulnerability eliminated` as PASS synonyms. Separate concern from FP.
4. **Watch silent `make install`** — `matio.cve-2019-9037` failed
   `make install` with permission denied; the unpatched library stayed on the
   loader path. Add a post-install checksum or symlink check.
5. **Block tool substitution** — forbid Valgrind for stack/heap sanitizer
   classes that Valgrind cannot diagnose; reject the verdict file if it cites
   a tool whose detection scope does not include the bug class in the CVE
   record.

## 7. Reproducing this audit

```bash
# from repo root
python3 experiments/b1-batch-autogen/scripts/audit_builder_success.py
python3 experiments/b1-batch-autogen/scripts/audit_builder_locations.py
python3 experiments/b1-batch-autogen/scripts/audit_fix_validator.py
# outputs:
#   /tmp/builder_strict_verdicts.json   (Builder TP/FP per run)
#   /tmp/b1_builder_locations.csv       (every expected binary per CVE)
#   /tmp/b1_evidence_sweep.json         (Phase-3 claim vs evidence)
```

The portable LLM prompt that turns the mechanical sweep into an FP/FN/TP/TN
verdict on a fresh machine is in
`experiments/b1-batch-autogen/scripts/AUDIT_PROMPT.md`.
