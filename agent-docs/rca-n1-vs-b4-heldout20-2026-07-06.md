# RCA — N1 vs B4 held-out-20 development batch (corrected)

## BLUF

The verified result is cost, not success: event-recorded cost was N1 `$1.009/run`
versus historical B4 `$2.178/run`, so historical B4 recorded 2.16× more cost.

The former `14/20` versus `15/20` strict-success claim is withdrawn. The old
success pipeline combined agent-authored verdicts, arm-dependent interpretation,
and known edge-case errors. It was not an independent SEC-bench-compatible outcome
measure and cannot support a paper claim.

This 20-task cohort is development data. It must not be reused for confirmatory
N1-versus-B4 inference.

## Verified record

| Item | Corrected value |
|---|---|
| Cohort | 20 paired tasks, 40 scored launches |
| Repository SHA | `c1e2b0f8eb8f254e76da2bcb56318eb690851fe5` for all 40 manifests |
| N1 event-recorded cost | `$20.178` total; `$1.009/run` |
| Historical B4 event-recorded cost | `$43.554` total; `$2.178/run` |
| Historical B4 topology | Intended 15-worker roster: 3 Builder + 6 Exploiter + 5 Fixer + 1 Reporter |
| Billed provider cost | Not established by this record |
| Context-cost savings | Not measured |

The recorded-cost totals come from `TokensConsumed` plus `WorkerCostRecorded`.
They are accounting events emitted by the runtime. They are not provider invoices,
so this document does not call them billed cost.

## What the old success tally got wrong

### Agent-authored verdicts were treated as outcomes

The old evaluator accepted `VERDICT: PASS`, determinism fields, and validation logs
written inside the agent workspace. Those files are diagnostic evidence, not an
independent final outcome. Final success must come from fresh host-owned replay.

### libredwg was scored differently by arm

For both libredwg tasks in the batch, the legacy phase scorer accepted the N1
Exploiter artifacts and rejected the B4 Exploiter artifacts because B4's own
validator wrote a non-PASS result. That is arm-dependent self-grading, not an
official mechanical comparison. The same task/base pair must receive the same
SEC-bench mechanical interpretation from independent host evidence regardless of
which arm produced the artifacts.

### Zero-byte PoC false negative

The old path treated an empty PoC as missing. A zero-byte file can be a valid PoC;
presence and host replay determine validity. The ImageMagick zero-byte case was
therefore a false negative under the old scorer.

### Exit-134 false positive

The old patch logic could treat a non-sanitizer exit as success even when the
patched program aborted with exit 134. Assertion abort, SIGABRT, fatal signal,
core dump, timeout, or sanitizer output must fail the Arise safety floor.

These defects invalidate the old strict-success table and its McNemar calculation.
They do not change the recorded-cost totals.

## Cost diagnosis

### Cache hit rate was not the differentiator

The event record showed high worker cache-read fractions in both arms: historical
B4 `95.8%` and N1 `97.3%`. The premise that hierarchy would become cheaper because
only B4 benefits from caching was false.

### Historical B4 paid for fan-out and fresh conversations

Historical B4 specified a 15-worker roster plus four phase controllers and the
BOSS. A run could execute fewer workers when a branch failed early, so averages
below 15 describe incomplete execution, not the configured roster.

Workers shared one container but opened fresh OpenHands conversations. The prior
configuration comment claiming conversation reuse was wrong. Fresh conversations
repaid the prompt context floor for each selected worker.

### Prompt growth existed; its savings were not measured

The append-only source block grew as observations accumulated. The former RCA's
exact first-to-last prompt-growth endpoints and its `22.2%` counterfactual saving
are removed: they were not produced by a controlled ablation and mixed source
growth with other prompt segments.

The defensible conclusion is narrower: run-global append-only context increased
later prompts. The net savings from replacing it with scoped, bounded packets are
unmeasured until the context-only ablation is run.

## Correct causal statement

Historical B4 recorded more cost because it replaced one long worker conversation
with a fixed fan-out of fresh worker conversations and repeatedly carried growing
context. High cache-hit rates reduced the price of repeated input but did not make
that input free.

No causal success statement is valid from this batch because:

- worker models differed between N1 and B4;
- final outcomes were not based on independent host replay;
- semantic judgement was not three-call plus human adjudication;
- the cohort had already been inspected and is now development data;
- the old outcome labels contain the zero-byte, exit-134, and libredwg defects.

## Required replacement evaluation

Success is:

```text
SEC-bench-compatible mechanical success
AND Arise safety/provenance floor
AND independent semantic/root-cause success
```

Mechanical evaluation must replay exported artifacts in fresh SEC-bench containers,
record strict/medium/generous sensitivity, and use medium as the patch primary.
Agent-authored verdict files remain diagnostic only.

Semantic evaluation uses three independent pinned-judge calls. Unanimous results are
provisional; 2–1 and judge-error cases enter blinded human adjudication, along with a
stratified 10% sample of unanimous cases.

The confirmatory comparison must use untouched paired tasks, frozen models and
configuration, randomized/interleaved order, all launches retained, paired McNemar
analysis, and paired bootstrap confidence intervals. The valid claim is system-level
B4 versus N1, not hierarchy-only causality.
