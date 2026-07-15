# RCA - B4 vs N1 held-out 18-run result

Date: 2026-06-18

Scope: 9 fresh held-out SEC-bench instances run as N1 and B4, 18 runs total. Source
of truth: Postgres events loaded by the RCA/evaluation tooling plus `runs/<run_id>/`
artifacts.

## Result

Current evaluator, using the saved strict judge verdicts from `_run_logs/new9_eval.json`
plus the deterministic floor in `experiments/shared/evaluation/criteria.py`:

| Cell | Strict success | Avg cost |
|---|---:|---:|
| N1 | 8/9 | $1.0319 |
| B4 | 6/9 | $2.2114 |

The original raw strict judge result was N1 7/9 vs B4 4/9. The deterministic
floor corrects stochastic judge false-negatives symmetrically: N1 +1 on upx, B4
+2 on njs and openjpeg. It does not change the conclusion: B4 remains worse
and costs about 2.1x N1.

## Causal chain

B4 success is lower because three B4 instances still fail after symmetric judge
correction:

1. libredwg fails reproduction fidelity: same sanitizer class and function, but
   observed WRITE vs golden READ, so it is not the oracle crash.
2. upx has valid exploit and patch artifacts, but the run fails mechanical
   completion gates.
3. yara reproduces the bug, but the patch targets load-time validation in
   `libyara/rules.c` while the post-patch crash still occurs at
   `libyara/exec.c:1426`.

Those failures happen because the experiment is not isolating topology. N1 is a
single flat OpenHands worker using `gpt-5.3-codex`; B4 decomposes through
`gpt-5.4` boss/managers but delegates execution to `gpt-5.4-mini` OpenHands
workers. The dominant variable is therefore "strong flat worker" vs "weak
decomposed leaf workers", not B4 topology alone.

## Evidence

- B4 config: full hierarchy with `boss.model: gpt-5.4`,
  `manager.model: gpt-5.4`, and `worker.model: gpt-5.4-mini`
  (`experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml:1-12`,
  `:36-40`).
- N1 config: flat OpenHands with `worker.model: gpt-5.3-codex`
  (`experiments/n1-secbench-full/configs/N1-openhands-linear.yaml:1-18`).
- Evaluator floor: only upgrades FAIL to PASS when the observed crash signature
  matches class, access kind, and top application frame; provenance requires a
  runtime seal plus ASan in harness-captured events
  (`experiments/shared/evaluation/criteria.py:733-904`).
- Floor tests explicitly cover njs/openjpeg matches and libredwg READ/WRITE
  mismatch rejection
  (`experiments/shared/evaluation/tests/test_crash_signature_floor.py:1-70`).
- libredwg B4 artifact says `VERDICT: FAIL`; the crash is deterministic in
  `htmlescape`, but observed access is WRITE while the bug report is READ
  (`runs/d675c29b-fc27-4f83-b996-9a65cf0b8788/testcase/exploit_validation_results.txt:1-10`).
- upx B4 artifacts say exploit validation PASS and patch validation PASS, but
  the recomputed strict result still fails because mechanical phase gates fail
  (`runs/0059d0a2-346d-4f4c-af72-74925d5d7867/testcase/exploit_validation_results.txt:1-10`,
  `runs/0059d0a2-346d-4f4c-af72-74925d5d7867/testcase/patch_validation_results.txt:1-14`).
- yara B4 artifact says `VERDICT: FAIL`; all three post-patch repro runs still
  hit heap-buffer-overflow in `yr_execute_code::OP_FOUND`, and the patch edits
  `libyara/rules.c`, not the crashing `exec.c` path
  (`runs/cac2d36e-ca67-45ac-8bb9-fb60d0a8b9e2/testcase/patch_validation_results.txt:1-14`,
  `runs/cac2d36e-ca67-45ac-8bb9-fb60d0a8b9e2/testcase/model_patch.diff:1-72`).

## Root-cause class

`weak-model-output`, with a secondary `eval/contract-artifact` issue already
fixed by the deterministic floor.

The evaluation problem was real but not the main result: stochastic judges
incorrectly failed byte-identical crash signatures on both arms. The floor fixes
that without helping one arm selectively. After the fix, the hypothesis
"B4 success rate > N1 and cost near same" is still unsupported.

## Best next test

Run a topology-controlled comparison:

1. B4 with `worker.model: gpt-5.3-codex`, keeping the hierarchy.
2. Optionally N1 with `worker.model: gpt-5.4-mini`, keeping flat topology.

Until worker model strength is controlled, the current data cannot support a
topology claim. It supports only this narrower claim: B4 as configured
underperforms N1 as configured.
