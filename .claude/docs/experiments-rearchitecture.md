# Experiments Re-Architecture

## 2026-07-13 N1/B3/B4 design

This is the canonical design and release gate for the next N1 vs B4 experiment. It is
not a claim that every item is implemented; current gaps are listed explicitly at the
end. The runtime is experiment-agnostic: source code does not branch on study or
treatment identifiers. Each study selects behavior only through its YAML configuration.

### Treatments

| Cell | Treatment | Purpose |
|---|---|---|
| N1 | `n1-openhands-linear`: one flat OpenHands session; OpenHands subagents and Host procedural dispatch explicitly disabled | naive control |
| B4 | `b4-adaptive-manager-v2`: BOSS -> four host-created phase Managers -> host-created compact role workers; after a required failure the LLM Manager proposes same-phase recovery roles and failure-specific instructions | adaptive hierarchical treatment |
| B3 | `b3-direct-compact-v1`: BOSS -> the same nine compact roles directly; Manager layer omitted | direct manager-layer ablation |

B3 changes only the Manager layer: `include_manager_layer: false`. Every other resolved
behavioral setting, including hierarchy limits, is identical to B4. The host
generically flattens the same compact phase DAG. B3 otherwise inherits B4's role prompts,
models, tools, procedures, domain context, retry policy, and budgets. Runtime briefing and
ancestry metadata necessarily differs because the Manager node is absent, so rendered prompts
are not byte-identical. Dependency-scoped source packets remain equivalent: grouping
predecessors expand to their terminal leaf sinks before entry workers receive context.

N1 remains flat. Runtime source does not branch on N1/B3/B4 study names or treatment
identifiers. Its committed configs and confirmatory launch validation fail closed unless
the mode is flat, OpenHands is selected, subagents are disabled, and procedural dispatch
is explicitly false.

### Confirmatory comparison boundary

N1 and B4 share the same solver-visible CVE context, domain goals, quality requirements,
canonical artifact contract, and post-run combined evaluator. N1 performs every in-run
build, reproduction, analysis, patch, and validation action through its normal OpenHands
tools. It receives no Host procedure execution, patch rendering, procedure failure digest,
or Host recheck. B4 deliberately adds those mechanics along with hierarchy, role/model
routing, scoped context, and recovery.

Therefore N1 versus B4 estimates the effect of the **complete B4 system**, not hierarchy
alone. Full prompts are not byte-identical because the execution systems differ; shared
phase semantics and artifacts are the controlled contract. B3 versus B4 is the
Manager-layer ablation.

### B4 adaptive route

The trusted host deterministically creates the four-phase DAG and each phase's compact
initial role route:

```text
BOSS
  -> Builder manager  -> Build-Setup -> Build-Executor -> Build-Verifier
  -> Exploiter manager -> Repro-Creator -> Exploit-Validator
  -> Fixer manager -> Root-Cause-Analyst -> Patch-Applier -> Patch-Validator
  -> Reporter manager -> Reporter
```

Hard phase dependencies are `Builder -> Exploiter -> Fixer -> Reporter`. A phase gate
must pass before its dependent phase can become runnable. A B4 Manager uses the existing
LLM decomposition path only after a required failure. It proposes same-phase role labels
and failure-specific task instructions from the supplied failure evidence.

The host never interprets failure keywords. It drops unsafe or invalid selections, closes
hard dependencies, binds each accepted role to its trusted catalog prompt, model, and
procedure, schedules execution, and records route provenance. It expands to the full
remaining same-phase set only when the Manager decision is explicitly invalid and leaves
no safe selection. This is a fail-safe, not normal routing. Completed roles are reused and
must not be respawned; failed compact roles may be reissued with revised instructions.

`PhaseRouteSelected.task_sources` records each role instruction as `host_policy`, `llm`,
or `host_repair`. Manager evidence references are copied into child `Briefing` values and
worker prompts, keeping recovery instructions traceable to the failed evidence.

B4 does not spawn every catalog role initially. B3 receives the same dependency-closed
nine-role compact route directly from the host, without phase-Manager role selection.

### Worker taxonomy and model policy

For B3/B4 roles, use two axes:

1. **Execution mechanism:** LLM or deterministic host procedure.
2. **Task character:** coding/execution, reasoning, hybrid, or synthesis.

`host procedure` means deterministic code executed by the trusted Arise process. It is
not a person, review queue, or other human-in-the-loop step.

N1 has no procedure-backed role. Its single OpenHands worker uses `gpt-5.3-codex`,
interprets the common phase contract, and issues `secb` commands itself.

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
`Build-Verifier`, `Exploit-Validator`, `Patch-Applier`, and `Patch-Validator` use no LLM
during each Host procedure attempt; their bounded failure path is the sole exception.

B3 uses the identical B4 per-role routing: the same five LLM-backed compact roles select
the same models, and the same four procedure-backed roles use no LLM during each Host
attempt. Their bounded failure path may invoke the single agentic repair described below.
Persist the selected model on every worker-cost event and in the run manifest.

The in-run procedure contract is also identical in B3 and B4. Reproducers declare a
structured direct argv: an exact prefix resolves the first Builder binary and selected
PoC, then the final `exec "$BIN"` may contain inert literal arguments and exactly one
`$POC`, or the sole supported stdin redirection. OpenEXR's
`exec "$BIN" -v "$POC" /dev/null` and FAAD2's
`exec "$BIN" "$POC" -o /dev/null` are valid; shell control, substitution, wrappers,
pipes, setup, and other redirection are not. The Host validates `repo_changes.diff` as
the byte-exact repository delta under the CVE work directory at the Builder producer
boundary; the external `/src/build.sh` recipe is a separate artifact and is never
synthesized into that diff. It freezes source/Builder state, every declared binary's
path/mode/digest, PoC, resolved argv/stdin mode, and any required project-local library's
SONAME, candidate, resolved target, target digest, and effective loader path, plus the
exploit verdict, plan, and patch identity. Procedure commands use
direct argv and a fixed launch environment; Host-owned deadlines guard only
external-process liveness, not predicate evaluation. A declared-binary startup probe streams
output; when the exact configured sanitizer marker appears, the Host stops the probe and
resets the container immediately, recording `STOPPED_AFTER_MARKER` rather than
`TIMED_OUT`. Reaching the deadline without that marker is a failure. Every startup-probe
outcome resets the container so detached descendants cannot survive. Every build, patch,
exploit-replay, and post-patch-replay timeout remains fatal and triggers container
recovery. Pre-patch success
requires complete oracle-matching signatures and consistent termination across 3/3, not
a nonzero exit. Post-patch clean replay accepts only exit 0 or the frozen dataset exit
oracle and rejects any sanitizer evidence.

`ProcedureEvidence` is Host-computed and stronger than an agent-authored verdict file,
but its commands still run in the mutable shared worker container. It is an in-run gate;
the fresh arm-independent evaluator below remains confirmatory authority.

Each procedure-backed worker starts with one zero-LLM Host attempt. Failure records Host
evidence and a bounded digest, then permits exactly one agentic repair. A failed repair is
terminal. If the repair completes, its `WorkCompleted` is provisional and the Host runs
exactly one procedure recheck. Within this recovery path, only recheck success completes
the role with protected Host evidence. Recheck failure is terminal with no further LLM
retry. B3 and B4 use this identical procedure-recovery lifecycle.

### Success architecture

No agent-authored `VERDICT: PASS`, `WorkCompleted`, report sentence, or file presence is
sufficient. Authoritative success is:

```text
success = official_mechanical
       AND safety_and_provenance
       AND host_regression
       AND semantic_accepted
```

All four terms are fail-closed and use the same frozen evaluator across arms.

#### Mechanical replay

- Require every common solver deliverable in both arms to be present and non-vacuous,
  except `repo_changes.diff`, which may be empty. `patch_plan.json` must pass the strict
  arm-independent schema before semantic judging can run.
- Accept a declared PoC even when its file is zero bytes; presence and path resolution,
  not non-empty content, determine eligibility.
- Start each evaluation from a fresh frozen base image/worktree.
- Run the exploit three independent times. Each replay must satisfy the frozen
  evaluator's own completion, timeout, and sanitizer checks, and all 3/3 must match
  the frozen CVE oracle on sanitizer class, access kind, and top frame/function.
- Apply the model patch, rebuild, and run the frozen post-patch evaluator three times.
  Any timeout,
  signal, assertion, sanitizer finding, exit outside the accepted frozen set, or crash fails.
- Run the declared regression suite separately. A fixed PoC does not imply regression
  safety.
- Preserve the exact argv, exit code, derived signal, timeout flag, output hash, image
  and commit identity, plus the upstream evaluator's raw JSON reports. The normalized
  adapter currently captures combined command output rather than separate stdout/stderr
  or a per-command duration field; documentation must not claim those absent fields.

#### Safety and provenance

- Protect the canonical host-owned paths enforced by the safety floor: PoC and binary
  pointers, the repro and patch wrappers, validation transcripts, the evaluation bundle,
  and the `secb`/compile shims. The external adapter transports the submitted PoC and patch
  separately, but the safety floor does not dynamically add the declared PoC payload path
  to its protected-path set; do not claim that stronger property.
- Bind both replay trios to the requested CVE instance and same frozen base commit. The
  upstream PoC and patch evaluators use different artifact transports, so do not claim
  byte-identical PoC execution without additional adapter evidence.
- Reject path escape, edits to the canonical protected set, stale workspaces, and missing
  command evidence. Both arms author the same PatchPlan edit-intent schema. B3/B4's in-run
  Patch-Applier validates and renders it; the Host independently binds the approved plan
  hash to its frozen replay identity in private approval evidence. Replay identity is not a
  solver-authored PatchPlan field. The arm-independent authoritative evaluator supplies the
  plan and diff to semantic review but does not mechanically compare them.
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
- root-cause analysis, solver-authored PatchPlan, rendered diff, patch/rebuild/replay evidence,
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

### Confirmatory protocol

- Use a new untouched cohort. Development CVEs used to tune prompts, routes, models, or
  thresholds are excluded. Declare the development enrollment locks in the
  preregistration; the launch adapter rejects any cohort overlap before launch.
- Put N1 and B4 in one immutable study manifest with identical `(task, base_commit,
  replicate)` assignments.
- Randomize instance order and which arm runs first inside each adjacent pair; freeze
  the seed.
- Enroll every launch by intention to treat. Failures, launch errors, and timeouts stay
  in the denominator.
- Reject missing arms, duplicates, and unequal task sets. Retain an evaluator failure as
  an ITT failure, report it separately through `evaluation_completeness_rate`, and block
  the superiority decision unless every assignment has a complete authoritative bundle.
  Never silently intersect, skip, or treat an incomplete evaluation as success.
- Preregister primary success endpoint, cost endpoint, superiority/non-inferiority
  margins, sample size/power, bootstrap seed and sample count, retry policy, and stopping
  rule before launching.
- Freeze treatment config, prompts, dataset roster, evaluator code/config hashes, model
  identifiers, dependency lock hash, container image digests, and Git commit.

### Release gate before any new N1 vs B4 run

| Status required | Gate |
|---|---|
| PASS | both N1 configs explicitly disable subagents and procedural dispatch; definition and confirmatory launch validation reject drift |
| PASS | N1 and B4 require the same root-cause analysis, PatchPlan, patch, reproducer, diagnostic verdicts, and report artifacts |
| PASS | B4 privately binds each approved plan's normalized hash and exact raw bytes to the frozen replay identity without changing the common solver PatchPlan schema |
| PASS | preregistration and reports identify N1-versus-B4 as a complete-system effect and reserve hierarchy attribution for B3-versus-B4 |
| PASS | prompts agree with the B4 Manager-recovery and B3 direct-compact policies |
| PASS | generic per-role `model_overrides` selects the documented models and is covered by adapter/config tests |
| PASS | four host procedures and their bounded repair lifecycle (initial Host attempt, one agentic repair, then one Host recheck after completed repair) are covered by timeout, signal, nonzero-exit, zero-byte-PoC, terminal-recheck, and evidence-integrity tests |
| PASS | automated heterogeneous judge panel replaces all human-audit paths |
| PASS | judge evidence includes frozen oracle and raw replay/regression evidence |
| PASS | authoritative evaluator performs 3x pre-patch and 3x post-patch replay plus regression |
| PASS | evaluator bundle persists raw evidence, versions, hashes, prompts, responses, retries, and timestamps |
| PASS | paired runner rejects missing/duplicate/unequal arms, reports evaluator completeness separately from cost completeness, blocks the decision on incomplete bundles, and never converts missing usage to zero |
| PASS | paired N1/B4 manifest and preregistration are validated, then frozen |
| PASS | targeted tests, full `uv run pytest`, `uv run pyright`, and architecture-boundary check pass |
| PASS | development-cohort dry run produces complete bundles with no human action |

### Current blockers (as-built after 2026-07-12 finalization pass)

Landed (verify against tests, not this list alone):

1. Per-role `worker.model_overrides` wired through config/bootstrap/OpenHands.
2. Zero-human heterogeneous panel: `claude-opus-4-8`, `gpt-5.5-2026-04-23`,
   `gemini/gemini-3.5-flash`; the evaluator requires three valid seats, then applies
   deterministic 2-of-3; the human audit queue is removed.
3. Six independent fresh replays (PoC×3, patch×3) with exact crash-signature oracle.
4. Confirmatory exact pairing rejects missing/duplicate/unequal arms.
5. Runnable `b4-boss-manager-worker`, `b3-direct-compact`, and `n1-openhands-linear`
   development studies exist; the N1/B4 confirmatory preregistration remains draft.
6. Missing cost no longer blocks primary ITT success; cost endpoints report unavailability.
7. Evaluator completeness is reported separately from cost completeness; an incomplete
   bundle remains an ITT failure and blocks the superiority decision.
8. Host regression contract + runner exist and fail closed without a frozen plan.
9. All 19 candidate regression plans use project-specific executable probes, are
   validated on base and gold-patched fresh containers, and are bound by a plan-set
   SHA-256; they must be regenerated for the replacement cohort.
10. N1's committed configurations explicitly disable procedural dispatch and subagents;
    flat-mode and confirmatory launch contracts reject procedure drift.
11. Root-cause analysis and the solver-authored PatchPlan are common N1/B4 artifacts;
    B4's replay-identity binding remains Host-private approval evidence.

Still open before confirmatory launch:

1. Replace the current 19-task candidate roster: all 19 tasks appear in declared
   development enrollments. The launch adapter now fails closed on this overlap.
2. Provider smoke for the three pinned judges (`claude-opus-4-8`,
   `gpt-5.5-2026-04-23`, `gemini/gemini-3.5-flash`) must return valid structured
   responses in this environment. A permanently unavailable Anthropic seat collapses
   the panel into OpenAI/Gemini unanimity and requires a new evaluator version — do
   not ship that way.
3. Development-cohort end-to-end pilot bundle must prove: six distinct container IDs,
   exact oracle 3/3, frozen host regression pass, all three judge seats valid,
   full provenance/hashes, no blinded treatment/model fields to judges.
4. Real Postgres-backed recovery-path validation must prove the required-failure ->
   Manager decision -> host validation -> scheduled role execution -> persisted provenance
   path. The confirmatory preregistration is draft and unfrozen until that validation passes.
Landed since the prior blockers list (verify against tests):

- `fresh_base` derived from six distinct container identities (not asserted).
- `ReferenceReplayResult` carries `replay_id`, `container_id`, `image_digest`,
  `instance_id`, and `base_commit`; independently derived replay-target identity fails
  closed on missing or mismatched instance/base provenance.
- Crash-signature oracle: SEGV `unknown` access; symbol-less `module+offset` frames;
  19-fixture preflight has zero incomplete signatures.
- Regression executes only inside a fresh patched SEC-bench container (host argv
  execution removed as a P0 defect).
- Verification status is intentionally non-numeric in this design document. Use the final
  task report for the current focused/full test, type-check, architecture-boundary, and
  `git diff --check` results; do not treat an earlier pass count as current evidence.

Do not start confirmatory experiments until every release-gate row passes.

---
