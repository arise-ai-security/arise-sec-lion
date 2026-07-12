# RCA — the 4 failed B4 runs (gate-fix measurement, 2026-06-16)

**Scope:** B4-after = 5/9 strict; the 4 failures: matio, md4c, gpac, openexr.
**Source of truth:** Postgres `arise_events` + `runs/<id>/` artifacts only. Method: `secbench-run-rca`
skill — grounded acquire → multi-viewpoint workflow (41 agents) + Claude adversarial-verify → CO review
→ N1 differential. Every claim cites an event field or artifact.

Run IDs: matio `2710ba3f` · md4c `5951c0f8` · gpac `aae0b37a` · openexr `ca659583`.
N1 comparisons: matio `cb3e18b6` · md4c `e5f2e32b` · gpac `bb340721` · openexr `1f7c06df`.

## BLUF

| run | failed gate | root cause (grounded) | class | flippable |
|---|---|---|---|---|
| **openexr** | cve_reproduced + Exploiter gate | crash **WAS reproduced** — `repro_run_1.log` = exact golden crash (`heap-overflow READ in TileOffsets::operator()→writeTileData→copyPixels`), but the Exploit-Validator **never wrote** its required `exploit_validation_results.txt` (file ABSENT, 0 `SourceFileEdited`) → mechanical Exploiter gate E4 (needs `VERDICT: PASS`, criteria.py:79,624-629) **and** the cve_reproduced judge both fail | deliverable-completion | **YES (emit verdict) → 6/9** |
| **gpac** | execution_provenance | worker ran `secb build/repro/patch` via `nohup … &` (detached); results went to files written by the background process, not the agent's tool-result transcript → provenance unverifiable from the un-forgeable stream | provenance / exec-pattern | yes (both arms) |
| **md4c** | cve_reproduced | inlining collapsed golden frame `md_analyze_line`(md4c.c:5985)→`md_parse` (2 hops); fails strict_v2 one-hop for **both arms** (N1 re-judged F) | build-fidelity | build-flags only (both arms) |
| **matio** | cve_reproduced + patch | worker reproduced a **different bug** (stack-overflow vs golden heap-**WRITE**, same fn `Mat_VarReadNextInfo5`); its own validator self-FAILED correctly; N1 fails too | intrinsic-hard | no cheap fix |

## Cross-run generic root causes (the actionable fixes)

### 1. Role-gating gap in `builder.j2` + `fixer.j2` (HIGH, generalizes to ALL B4 runs)
The role-scoped worker body (gated on the computed `worker_role`/`wr`, withholding foreign-role
deliverables) exists **only in `exploiter.j2`**. Grounded marker count:
`builder.j2=0, exploiter.j2=5, fixer.j2=0`. `prompt_strategy.py:~300` computes `worker_role`+
`foreign_deliverables` for **all** workers, but `builder.j2`/`fixer.j2` ignore them and render the
whole-phase runbook to every leaf. Confirmed in events: the matio `[Build-Verifier]` leaf
(`cb4e9697`) `worker_execution` prompt renders "1. Determine base commit … 7. Declare executable
paths … write /testcase/binary_paths.txt" (Build-Setup/Build-Compiler deliverables) with 0
role-scoping markers, while its sibling `[Exploit-Validator]` correctly shows "### Your role:".
→ **Fix (B-only, `prompts/domains/secbench/worker/{builder,fixer}.j2`):** extend the `exploiter.j2`
role-gating block to Builder and Fixer. This is the single highest-leverage change for the weak worker.

### 2. Ownership abdication in the Fixer phase (HIGH)
`patch_validation_results.txt` (owner = **Patch-Validator**) is written by **Patch-Creator** in md4c
and openexr — the patch author self-validates its own `model_patch.diff` and writes its own
`VERDICT: PASS` (grader bias). Owner roles edit zero files. (Same owner-abdication pathology as
`role-bleed-diagnosis.md`, now in Fixer.) → **Fix:** Finding-1's role-gating stops Patch-Creator from
being told to write Patch-Validator's file (prompt-side); an optional orchestration ownership guard
(reject `SourceFileEdited` whose role ≠ path owner) is the stronger backstop. *(CO assessing layer.)*

### 3. Deliverable-completion: Exploit-Validator didn't emit its verdict (HIGH — the openexr flip)
openexr reproduced the exact golden crash (`repro_run_1.log`, deterministic) but the Exploit-Validator
**never created** `/testcase/exploit_validation_results.txt` (file ABSENT — 0 `SourceFileEdited`). That
file is a REQUIRED deliverable: the mechanical Exploiter gate E4 needs it to contain `VERDICT: PASS` +
`DETERMINISM_RUNS: 3/3` (criteria.py:79,593,624-629), so its absence fails the Exploiter phase; the
`cve_reproduced` LLM judge (which DOES read repro logs, criteria.py:926-939) also returned F. The crash
was reproduced but the run got no credit because the validator role produced nothing. → **Fix:** the
Exploit-Validator MUST write its verdict file even on a clean PASS (completion requirement), reinforced
by Finding-1 role-gating. (A judge fallback to repro logs is possible, but the mechanical deliverable
contract legitimately requires the file — fix emission, don't loosen the gate.) Flips openexr →
**B4 6/9 = N1**, conditional on emission. Open: WHY it didn't emit (iteration budget / prompt
non-compliance) needs the openexr Exploiter transcript — a viewpoint the classifier killed.

### 4. Detached `secb` execution defeats provenance (gpac; both arms)
gpac ran `nohup bash -c 'secb build; …' &` and `secb patch; … secb repro > fix_run_$N.log &`
(transcript, FIXER phase). The launches appear in `ThoughtCaptured`, but exit codes / crash output
were written by the **detached** process to files — the agent may seal before/without the result in
its transcript, so the provenance judge can't verify execution. → **Fix (B-only prompts):** run `secb`
**synchronously** (or `wait` + echo the exit/verdict into the agent's own output) so provenance is in
the un-forgeable stream.

### 5. Build-fidelity / inlining (md4c; both arms)
Optimized build inlines `md_analyze_line`+`md_process_doc` into `md_parse`, so the observed first app
frame is `md_parse` — 2 hops from the golden `md_analyze_line` → fails strict_v2's exact-or-one-hop.
N1's own validator observed the same `md_parse`. → **Fix:** build with frame-preserving flags
(`-fno-inline-functions`/`-O1`) so the golden frame resolves — affects both arms. (A prior build-flag
band-aid was reverted as md4c-overfit; a *generic* frame-preservation flag is the principled version.)
Trade-off: strict_v2 was deliberately tightened to one-hop; do NOT re-loosen it for this.

### 6. Eval-consistency (md4c)
N1's baseline (`n1_strict_fix.json`) judged `md_parse` as `cve_reproduced=True`; current strict_v2
judges it **False** (re-judge of N1 `e5f2e32b` confirmed `overall=0, cve_repro=0`). N1's *overall*
md4c was already a fail, so **N1 stays 6/9** — but for apples-to-apples, N1 must be re-scored under the
committed criteria, not the older lenient run.

## Tooling correction (found by the RCA's own handoff viewpoint)
`rca_tool handoff` initially reported empty `<decision>`/`<output>` channels — a **false negative**:
`load_run` only loaded the `ChildSpawned` tree, missing the uuid5-derived **SharedStore aggregate**
where `DecisionRecorded`/`ArtifactStored` live. **Fixed** (`load_run` now merges
`shared_context_aggregate_id(root)`). Post-fix, matio shows roles DID publish (Patch-Validator 3
decisions; Forward-Instrumentator/Root-Cause-Analyst artifacts). The earlier "handoff filesystem-only"
claim was the artifact; corrected.

## Per-run evidence

- **matio** (`2710ba3f`): `exploit_validation_results.txt` → `VERDICT: FAIL … stack-buffer-overflow
  instead of heap-buffer-overflow/write … CRASH_FUNCTION_OBSERVED: Mat_VarReadNextInfo5`. Golden =
  heap-overflow WRITE in same fn. Different bug; correct self-rejection. N1 (`cb3e18b6`) also F.
- **md4c** (`5951c0f8`): observed first app frame `md_parse`; golden `md_analyze_line` (md4c.c:5985).
  N1 (`e5f2e32b`) identical (`md_parse`). Both fail under strict_v2.
- **gpac** (`aae0b37a`): `patch_validation_results.txt` claims PASS/clean/3-3-no-crash; transcript shows
  `secb` launched detached via `nohup … &`; provenance judge F. N1 (`bb340721`) also F provenance.
- **openexr** (`ca659583`): `repro_run_1.log` = golden crash exactly (`TileOffsets::operator()` heap
  READ); `exploit_validation_results.txt` empty. N1 (`1f7c06df`) PASS (validator wrote the verdict).

## CO (Codex) review verdict (`task-mqh8pn8h`, read-only)

- **Finding 1 (role-gating gap): CONFIRMED.** Cited `prompt_strategy.py:300-304` (vars computed) +
  `:332-350` (same ctx to all 3 templates); `exploiter.j2:10,13-31,136-152,213-218` (gated);
  `builder.j2:6-13,91-99` + `fixer.j2:19`/`phases/fix.j2:61-72` (ungated, whole-phase). **Correction
  (adopted):** do it **phase-specifically**, not a blind copy — Build-Verifier owns no required
  deliverable (`roles.py:72-99`); keep the whole-phase fallback for phase-level `[Builder]`/`[Fixer]`
  + flat-baseline prompts.
- **Finding 2 (ownership abdication): PARTIAL/CONFIRMED.** CO independently found the same abdication in
  other runs (`runs/334fd378…/events.jsonl:157,767-769`; `runs/legacy/ecc93c5d…:257,791-793`).
  **Corrections (adopted):** (a) prompt-gating is necessary but **not sufficient** — verdict files are
  agent-written/forgeable (`SYSTEM_REFERENCE.md:760-780`); (b) an ownership guard on `SourceFileEdited`
  alone is **too narrow** — it misses shell-redirect writes (`events.py:542-552`,
  `openhands_adapter.py:855-885`); (c) the guard's owner-map belongs in **`plugins/security`**
  (from `roles.py`/`deliverables.py`) with a **generic core port** doing enforcement — mirror the
  decomposition-validator split (`core/ports/decomposition_validator_port.py`), never hardcode SEC-bench
  paths in core (`architecture.md:267-273`).
- CO **refuted my "empty file" wording** for openexr → corrected: the file is **ABSENT** (verified: no
  file, 0 edit events). Substance (validator emitted no verdict) holds; framing tightened in Finding 3.

## Methodology + open gaps
- Multi-viewpoint workflow: 41 agents; the Anthropic cyber-classifier killed the exploit-content
  viewpoints (gpac + openexr hardest — most structural viewpoints there died), so those two rely on
  main-thread grounded analysis. matio/md4c structural viewpoints survived + Claude-verified.
- CO (Codex) review of Findings 1–2: *in flight* (`task-mqh8pn8h`).
- Open: confirm N1 gpac also detaches `secb` (expected, since provenance fails N1 too); quantify how
  many *passing* B4 runs already had Builder/Fixer role-bleed (Finding 1 may be improving them less
  than it could).

## Recommended fix order (most leverage first, all generic)
1. Extend role-gating to `builder.j2` + `fixer.j2` (Finding 1) — stops Findings 1+2 prompt-side.
2. Exploit-Validator MUST emit verdict + `cve_reproduced` judge fallback to repro logs (Finding 3) →
   flips openexr to 6/9.
3. Synchronous `secb` in worker prompts (Finding 4).
4. Frame-preserving build flags (Finding 5) — re-test md4c + regression-check.
After 1–3, re-run the 9-instance matrix; expected B4 ≥ 6/9 (openexr flips; Builder/Fixer role-bleed
removed may lift others).
