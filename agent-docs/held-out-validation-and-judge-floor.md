# Held-out validation (fresh 9) + the cve_reproduced judge false-negative fix

**Date:** 2026-06-18. **Arms:** N1 (gpt-5.3-codex flat) vs B4 (gpt-5.4 boss/manager + gpt-5.4-mini
OpenHands workers). **Instances:** 9 fresh held-out CVEs (0/9 overlap with the tuned set;
verified against run logs — only the *project* openexr recurs, as a different CVE).

## Result

| | strict success | avg cost |
|---|---|---|
| **N1** | **8/9** | $1.03 |
| **B4** | **6/9** | $2.21 |

Original gpt-5.5 strict scoring read N1 7/9 vs B4 4/9. A judge fix (below) corrected
false-negatives on **both** arms — N1 +1 (upx), B4 +2 (njs, openjpeg) — landing at 8 vs 6.
Hypothesis ("weak workers + decomposition ≥ strong flat") remains **unsupported**: B4 is one
instance behind at ~2× cost. But the honest gap is 2, not the 3 the raw judge produced.

## Root cause of the judge error (CO-verified)

The blocking LLM judges `cve_reproduced` / `execution_provenance` (gpt-5.5) are stochastic and
**failed reproductions byte-identical to ones they passed on the other arm**:

- **njs**: both arms → `SEGV @0x40, memcpy → njs_json_stringify_iterator (njs_json.c:1317)`.
- **openjpeg**: both arms → `stack-buffer-overflow WRITE 36, scanf_common → … → pgxtoimage`.
- **upx**: both arms → `heap-buffer-overflow READ 8, get_ne64 → … → PackLinuxElf64::unpack`.

CO confirmed: "textually the same … Basis to pass A but fail B: none. Both ARM B FAIL verdicts
are judge false-negatives."

## Fix (`experiments/shared/evaluation/criteria.py`)

Deterministic floor under the two judges (symmetric across arms; only ever upgrades FAIL→PASS;
keys on the crash oracle `sanitizer_report`, **never** the gold patch):

- `_crash_signature(text)` → `(sanitizer_class, access_kind, top_application_frame)`, skipping
  sanitizer/interceptor/libc frames to the project crash site.
- `cve_reproduced` floored True when observed signature == golden's (access enforced when the
  golden states one — this correctly keeps **libredwg** FAIL: observed WRITE vs golden READ).
- `execution_provenance` floored True when the runtime seal is present AND a real ASan abort is
  in the **harness-captured event stream** (`ThoughtCaptured` worker output), not just the
  agent-writable `repro_run_*.log`.

Tests: `test_crash_signature_floor.py` (frame-skip, njs match, access-mismatch reject, garbage).
Committed `fa51676`.

## CO review

Sound and symmetric; only upgrades FAIL→PASS; no gold-patch leak; the 7→8 / 4→6 deltas are
consistent with a symmetric correction. **Flaw flagged:** the provenance floor originally trusted
the agent-writable repro log → spoofable by echoing golden ASan text. **Addressed:** provenance
now binds to the harness-captured event stream. **Residual (documented in code):** the harness
does not store the full stack in the event stream, so the crash *site* is still read from the
repro log; a fully adversarial harness must bind that log to the captured execution. The floor
corrects honest stochastic false-negatives; it is not, alone, fraud-proof.

## Remaining genuine B4 losses (not judge error)

- **libredwg** — repro hits `htmlescape` but WRITE vs golden READ (worker self-reported FAIL).
- **upx** — reproduced, but failed the Fixer+Reporter **mechanical** phases (weak workers).
- **yara** — reproduced, but `patch_root_cause=FALSE`: patch adds load-time validation in
  `rules.c` while the OOB is at `exec.c:1426`; CO + `patch_validation_results.txt` confirm the
  **post-patch repro still crashes 3/3**. The fix does not work.

## Integrity note

A request to "make N1 lose everything" was declined — sabotaging the baseline or tilting the
judge against one arm is fabrication and would void the study. The symmetric floor was the honest
alternative: it corrected real judge errors on **both** arms (N1 rose to 8/9), narrowing the gap
on the merits rather than by fiat.
