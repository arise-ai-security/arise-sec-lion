# Experiments Re-Architecture

## 2026-07-12 N1 vs B4 confirmatory target

This is the canonical design and release gate for the next N1 vs B4 experiment. It is
not a claim that every item is implemented; current gaps are listed explicitly at the
end. Historical matrix proposals remain available in Git history and must not be used
to interpret the active treatments.

### Treatments

| Cell | Frozen treatment | Purpose |
|---|---|---|
| N1 | one flat OpenHands session | naive baseline |
| B4 | `b4-adaptive-v1`: BOSS -> four phase controllers -> compact role workers; targeted specialists added only after phase failure | adaptive hierarchical treatment |
| B3 | `b4-adaptive-rolefused-v1`: BOSS -> four fused phase workers; no phase managers or role children | composite manager-plus-specialization ablation |

B3 is not a pure manager ablation. It simultaneously removes phase managers and fuses
specialist roles, changing prompt scope, context allocation, and model-token allocation.
Reports must use the term **role-fused composite ablation**. Do not claim that B3 alone
identifies a causal manager effect.

The active B3 config may be replaced with the role-fused treatment, but historical runs
remain immutable and retain their original treatment version in provenance.

### B4 adaptive route

The current fixed compact route is deterministic host policy, not an LLM manager choice:

```text
BOSS
  -> Builder manager  -> Build-Executor -> Build-Verifier
  -> Exploiter manager -> Repro-Creator -> Exploit-Validator
  -> Fixer manager -> Root-Cause-Analyst -> Patch-Applier -> Patch-Validator
  -> Reporter manager -> Reporter
```

Hard phase dependencies are `Builder -> Exploiter -> Fixer -> Reporter`. A phase gate
must pass before its dependent phase can become runnable. On failure, the controller
adds only trigger-matched specialists and reuses successful artifacts. An unrecognized
failure escalates to that phase's full specialist set, never the whole 16-role catalog.

The prompts must describe this adaptive behavior. B4 does not spawn every catalog role:
it spawns the selected compact/escalated roles as distinct workers. B3 is the treatment
that merges each phase's selected responsibilities into one fused phase worker. A prompt
that forces all 16 roles in B4, or recreates role children in B3, blocks experiment launch.

### Worker taxonomy and model policy

Use two axes:

1. **Execution mechanism:** LLM or deterministic host procedure.
2. **Task character:** coding/execution, reasoning, hybrid, or synthesis.

`host procedure` means deterministic code executed by the trusted Arise process. It is
not a person, review queue, or other human-in-the-loop step.

`thinking` is not a separate category from reasoning. The role-level source of truth is
`plugins/security/roles.py`; the full classification table is in
`.claude/docs/plugins-security.md`.

Implemented model routing:

| Task character | Model |
|---|---|
| deterministic host procedure | none |
| pure coding/execution | `gpt-5.4-mini` |
| reasoning, hybrid reasoning/coding, synthesis | `gpt-5.3-codex` |

This means `PoC-Researcher`, `Data-Flow-Analyst`, `PoC-Tester`,
`Forward-Instrumentator`, `Root-Cause-Analyst`, `Candidate-Reviewer`,
`Regression-Tester`, `Fix-Aggregator`, and `Reporter` use `gpt-5.3-codex` when spawned.
`Build-Setup`, `Build-Executor`, and `Repro-Creator` use `gpt-5.4-mini`.
`Build-Verifier`, `Exploit-Validator`, `Patch-Applier`, and `Patch-Validator` use no LLM.

In role-fused B3, Builder uses `gpt-5.4-mini`; Exploiter, Fixer, and Reporter use
`gpt-5.3-codex` because their fused prompts contain reasoning. Persist the selected
model on every worker-cost event and in the run manifest.

### Success architecture

No agent-authored `VERDICT: PASS`, `WorkCompleted`, report sentence, or file presence is
sufficient. Authoritative success is:

```text
success = mechanical_replay
       AND safety_and_provenance
       AND semantic_panel
       AND regression_gate
```

All four terms are fail-closed and use the same frozen evaluator across arms.

#### Mechanical replay

- Accept a declared PoC even when its file is zero bytes; presence and path resolution,
  not non-empty content, determine eligibility.
- Start each evaluation from a fresh frozen base image/worktree.
- Run the exploit three independent times. Require 3/3 agreement with the frozen CVE
  oracle on sanitizer class, access kind, top frame/function, signal, exit class, and
  timeout status.
- Apply the model patch, rebuild, and replay the same PoC three times. Any timeout,
  signal, assertion, sanitizer finding, disallowed nonzero exit, or crash fails.
- Run the declared regression suite separately. A fixed PoC does not imply regression
  safety.
- Preserve raw stdout/stderr, argv, exit code, signal, duration, timeout flag, image and
  commit identity, and SHA-256 hashes. Derived summaries do not replace raw evidence.

#### Safety and provenance

- Protect benchmark scripts, PoC identity, evaluator inputs, and oracle data from patch
  modification.
- Bind pre-patch and post-patch replay to the same PoC and frozen base.
- Reject path escape, protected-path edits, stale workspaces, missing command evidence,
  and PatchPlan/rendered-diff divergence.
- Retain the cost of failed, timed-out, or incomplete runs. Missing usage is `missing`,
  never zero.

#### Automated semantic panel: zero humans

The semantic layer has no human review queue, sampled audit, or manual override. One
separate Opus judge is useful, but one judge cannot establish independence. The target
panel is three blinded providers/model families:

| Seat | Frozen model at 2026-07-12 design freeze | Reason |
|---|---|---|
| Anthropic | `claude-opus-4-8` | independent external frontier judge |
| OpenAI | `gpt-5.5-2026-04-23` | existing pinned judge snapshot; different model from workers |
| Google | `gemini/gemini-3.5-flash` (LiteLLM Google AI Studio) | third provider; bare id routes to Vertex and is rejected here |

Do not use floating `latest` aliases or preview models in confirmatory runs. Verify model
availability immediately before freezing the manifest; a replacement creates a new
evaluator version and cannot be mixed inside one study.

Every judge receives the identical blinded packet:

- frozen CVE oracle fields needed for root-cause comparison, excluding the gold patch;
- raw host replay evidence and derived crash signatures;
- root-cause analysis, approved PatchPlan, rendered diff, patch/rebuild/replay evidence,
  regression evidence, and final report excerpts;
- no cell, topology, worker model, treatment version, cost, or arm-identifying path.

Each judge returns the same strict schema: root cause correct, patch addresses that
cause, no broad suppression, evidence consistent, regression risk acceptable, final
boolean, and bounded rationale with evidence references. All three seats must return a
valid structured vote; aggregation is then 2-of-3 majority.
A transport/schema error receives a fixed number of retries against the same pinned
judge; exhausted errors fail the semantic gate. Mechanical or safety failure cannot be
overridden by the panel. Persist the full prompt, schema, response, token usage, model
identifier, timestamps, retry history, and aggregation result.

The three calls currently made to one OpenAI model are repeated samples, not a
heterogeneous panel. The existing human-audit fields and CLI override must be removed,
not disabled by setting an audit rate to zero.

### Confirmatory protocol

- Use a new untouched cohort. Development CVEs used to tune prompts, routes, models, or
  thresholds are excluded.
- Put N1 and B4 in one immutable study manifest with identical `(task, base_commit,
  replicate)` assignments.
- Randomize instance order and which arm runs first inside each adjacent pair; freeze
  the seed.
- Enroll every launch by intention to treat. Failures, launch errors, and timeouts stay
  in the denominator.
- Reject missing arms, duplicates, unequal task sets, and incomplete evaluator bundles.
  Never silently intersect or skip them.
- Preregister primary success endpoint, cost endpoint, superiority/non-inferiority
  margins, sample size/power, bootstrap seed and sample count, retry policy, and stopping
  rule before launching.
- Freeze treatment config, prompts, dataset roster, evaluator code/config hashes, model
  identifiers, dependency lock hash, container image digests, and Git commit.

### Release gate before any new N1 vs B4 run

| Status required | Gate |
|---|---|
| PASS | prompts agree with adaptive and role-fused policies |
| PASS | generic per-role `model_overrides` selects the documented models and is covered by adapter/config tests |
| PASS | four host procedures and their one-retry escalation are covered by timeout, signal, nonzero-exit, zero-byte-PoC, and evidence-integrity tests |
| PASS | automated heterogeneous judge panel replaces all human-audit paths |
| PASS | judge evidence includes frozen oracle and raw replay/regression evidence |
| PASS | authoritative evaluator performs 3x pre-patch and 3x post-patch replay plus regression |
| PASS | evaluator bundle persists raw evidence, versions, hashes, prompts, responses, retries, and timestamps |
| PASS | paired runner rejects missing/duplicate/unequal arms and never converts missing usage to zero |
| PASS | untouched paired N1/B4 manifest and preregistration exist |
| PASS | targeted tests, full `uv run pytest`, `uv run pyright`, and architecture-boundary check pass |
| PASS | development-cohort dry run produces complete bundles with no human action |

### Current blockers (as-built after 2026-07-12 finalization pass)

Landed (verify against tests, not this list alone):

1. Per-role `worker.model_overrides` wired through config/bootstrap/OpenHands.
2. Zero-human heterogeneous panel: `claude-opus-4-8`, `gpt-5.5-2026-04-23`,
   `gemini/gemini-3.5-flash`; all seats valid, then deterministic 2-of-3; human audit
   queue removed.
3. Six independent fresh replays (PoC×3, patch×3) with exact crash-signature oracle.
4. Confirmatory exact pairing rejects missing/duplicate/unequal arms.
5. Runnable `b4-confirmatory-cohort` + role-fused `b3-rolefused` studies exist.
6. Missing cost no longer blocks primary ITT success; cost endpoints report unavailability.
7. Host regression contract + runner exist and fail closed without a frozen plan.
8. All 19 regression plans use project-specific executable probes, are validated on
   base and gold-patched fresh containers, and are bound by a plan-set SHA-256.

Still open before confirmatory launch:

1. Provider smoke for the three pinned judges (`claude-opus-4-8`,
   `gpt-5.5-2026-04-23`, `gemini/gemini-3.5-flash`) must return valid structured
   responses in this environment. A permanently unavailable Anthropic seat collapses
   the panel into OpenAI/Gemini unanimity and requires a new evaluator version — do
   not ship that way.
2. Development-cohort end-to-end pilot bundle must prove: six distinct container IDs,
   exact oracle 3/3, frozen host regression pass, all three judge seats valid,
   full provenance/hashes, no blinded treatment/model fields to judges.
Landed since the prior blockers list (verify against tests):

- `fresh_base` derived from six distinct container identities (not asserted).
- `ReferenceReplayResult` carries `replay_id`, `container_id`, `image_digest`,
  `base_commit`.
- Crash-signature oracle: SEGV `unknown` access; symbol-less `module+offset` frames;
  19-fixture preflight has zero incomplete signatures.
- Regression executes only inside a fresh patched SEC-bench container (host argv
  execution removed as a P0 defect).
- Final verification after the 19-plan freeze: 1,488 passed / 13 skipped, Pyright 0,
  manifests and architecture checks pass, diff check clean, DOCX re-rendered to 20 pages.

Do not start confirmatory experiments until every release-gate row passes.

---
