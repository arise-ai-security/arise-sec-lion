# SYSTEM_REFERENCE.md — Arise × SEC-bench

> **What this is.** A single, diagram-first reference for how Arise runs SEC-bench
> CVE patch tasks: the SEC-bench substrate, the Arise architecture (event sourcing,
> the container runtime, how the Arise image is built on top of SEC-bench, the prompt
> pipeline), the role/artifact contract, and the mechanical + qualitative success/fail
> criteria.
>
> **As-built vs target.** Parts I–IV describe the system **as it actually runs today**
> (verified against source on 2026-06-14, citations are `file:line`). Forward-looking
> work is isolated in **Part V — Target State & Known Gaps**, and target-only judges in
> Part IV are explicitly labelled *DEFERRED*.
>
> **Provenance.** Every claim below was checked against live code (serena symbol tools +
> `Read`/`Grep`) and the reference repos `../SEC-bench`, `../SecVerifier`. Where this
> document contradicts `CLAUDE.md` or older notes, the contradiction is called out
> (see §II.2 and §V.6) — the code is the source of truth.

---

## Table of contents

- [Part I — SEC-bench, the substrate](#part-i--sec-bench-the-substrate)
- [Part II — Arise architecture](#part-ii--arise-architecture)
- [Part III — Roles, phases & artifacts](#part-iii--roles-phases--artifacts)
- [Part IV — Success / Fail criteria](#part-iv--success--fail-criteria)
- [Part V — Target state & known gaps](#part-v--target-state--known-gaps)
- [Appendix A — Key-file index](#appendix-a--key-file-index)
- [Appendix B — Glossary](#appendix-b--glossary)

---

# Part I — SEC-bench, the substrate

## I.1 — What SEC-bench is, and the `*-patch` image

SEC-bench packages each CVE as a self-contained OSS-Fuzz-derived Docker image. Arise
consumes the **patch-task** variant (`:patch` tag): the vulnerable source at a chosen
base commit, the OSS-Fuzz build wrapper, and the testcase artifacts — but **not** the
fix.

```
hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch     ← the upstream patch image
├── /src/                     project source tree ($SRC), incl. /src/build.sh
│   └── <project>/            the working dir (CVEInstance.work_dir, usually /src/<project>)
├── /usr/local/bin/compile    OSS-Fuzz build wrapper: sets SANITIZER flags, runs $SRC/build.sh
├── /usr/local/bin/secb       SEC-bench command harness (build|repro|patch)  ← may be GOLDEN
└── /testcase/                SEC-bench artifacts
    ├── <poc files>           trigger inputs — arbitrary filenames, NOT always poc*
    ├── base_commit_hash      the vulnerable base commit
    └── repo_changes.diff     build/setup delta replayed to reconstruct the baseline
```

`CVEInstance.docker_image` constructs that name from the instance id
(`hwiwonlee/secb.eval.x86_64.{project_name}.{cve_id}:patch`), override-able via
`docker_image_override`. — `plugins/security/cve_instance.py:49-54`

## I.2 — The `secb` harness and who authors each script

`secb` is a thin dispatcher over three project-specific scripts. **The most important
fact for the whole system is *who writes each script*** — that is the boundary between
"infrastructure Arise controls" and "deliverable the agent must produce".

```
  secb build ──▶ /usr/local/bin/compile ──▶ $SRC/build.sh        (compile injects sanitizer flags)
  secb repro ──▶ /testcase/repro.sh      ──▶ <poc> against the built binary
  secb patch ──▶ /testcase/patch.sh      ──▶ apply repo_changes.diff, then model_patch.diff
```

| `secb` verb | Executes | Author of the executed script | Author of its inputs |
|---|---|---|---|
| `build` | `compile` → `$SRC/build.sh` | OSS-Fuzz (`compile`) is fixed; **agent** owns `/src/build.sh` | — |
| `repro` | `/testcase/repro.sh` | **agent (Exploiter)** writes it (Arise seeds an empty skeleton) | the selected PoC in `/testcase` |
| `patch` | `/testcase/patch.sh` | **Arise** (fixed harness) | `repo_changes.diff` (dataset) + `model_patch.diff` (**agent/Fixer**) |

> **Upstream vs Arise `secb` differ.** Upstream `secb_helper.sh.j2` implements `build()`,
> `repro()`, `patch()` as inline shell functions (and `build()` itself replays
> `repo_changes.diff`). Arise *replaces* this with a delegating wrapper (§II.4) whose
> `build` is `compile`-only and whose `repro`/`patch` delegate to separate `/testcase`
> scripts; the `repo_changes.diff` replay is moved into `patch.sh`. So "Arise `secb` ==
> upstream `secb`" is **false** — the verbs match by name, the bodies and authorship do
> not. — `../SEC-bench/secb/preprocessor/templates/secb_helper.sh.j2:4-68` vs
> `plugins/security/docker_runtime.py:43-103`

## I.3 — Directory contract

| Path | Owner | Meaning | In Arise runtime? |
|---|---|---|---|
| `/src` (`$SRC`) | OSS-Fuzz | Source root; `/src/build.sh`; `/src/<project>` working dir | ✅ bind-mounted rw |
| `/testcase` | SEC-bench | PoC inputs, `base_commit_hash`, `repo_changes.diff`, agent artifacts | ✅ bind-mounted rw |
| `/work` (`$WORK`) | OSS-Fuzz | Scratch / build-artifact dir (e.g. `/work/bin`) | ✅ bind-mounted rw |
| `/out` (`$OUT`) | OSS-Fuzz | Fuzz-target output dir | ❌ **Arise never mounts `/out`** |
| `/arise-run` | Arise | Arise-only run root (mcp config, worker scratch) | ✅ bind-mounted rw |

> **Correction vs older notes.** `/out`, `$SRC`/`$WORK`/`$OUT` are *upstream base-image*
> conventions, not Arise runtime facts; Arise defines and mounts only `/src`,
> `/testcase`, `/work`, `/arise-run`. — `plugins/security/container_runtime.py:28-33`,
> `plugins/security/docker_runtime.py:284-305`

## I.4 — "Golden" code in a patch image

A patch image may ship the *answer* in several forms. Arise must hide all of it from the
agent while preserving the genuine PoC inputs. Anything in this table is **golden** and
sealed/stripped by Arise (§II.5):

| Golden surface | Where it can live | Why it is the answer |
|---|---|---|
| Gold patch / candidate fixes | `CVEInstance.patch`, `candidate_fixes`; `/testcase/*.patch` | The fix itself |
| Golden `secb_sh` / `repro()` body | `CVEInstance.secb_sh`, baked `/usr/local/bin/secb` | Encodes the exact reproduction command |
| Golden repro script / `replay_build.sh` | baked `/testcase/repro.sh`, `$SRC/replay_build.sh` | Pre-written trigger / build |
| Pre-baked `model_patch.diff`, `gold*.patch` | `/testcase/` | The fix, again |

The genuine **PoC inputs** in `/testcase` are *not* golden and are preserved — the agent
is expected to find and use them (§III).

## I.5 — Upstream Builder / Exploiter / Fixer convention

| Phase | Upstream intent | Main deliverable |
|---|---|---|
| Builder | Make the vulnerable base commit build with sanitizer instrumentation; edit `build.sh` only as needed | `build.sh`, `repo_changes.diff` |
| Exploiter | Use the shipped PoC; make `secb repro` trigger the expected sanitizer error | filled `repro` command |
| Fixer | Produce `model_patch.diff`; verify the PoC no longer triggers the sanitizer | `model_patch.diff` |

Arise keeps these three phases (and adds a Reporter phase) but rebuilds the contract
around **its own** `secb` wrapper, a 16-role catalog, and an artifact contract — Parts
II–III.

---

# Part II — Arise architecture

## II.1 — Layered (hexagonal) map

`core/` must not import `infrastructure/` (the hard invariant, enforced by a pre-commit
hook); it still uses approved libraries such as Pydantic, so "stdlib-only" is aspirational,
not literal. All infrastructure is injected at `bootstrap/`.

```
                 ┌──────────────────────────────────────────────┐
                 │                 bootstrap/                     │  composition root —
                 │  composition.py · infrastructure.py            │  the ONLY cross-boundary
                 └───────┬───────────────┬───────────────┬───────┘  importer
                         │ injects        │ injects        │ injects
              ┌──────────▼─────┐  ┌───────▼────────┐  ┌────▼──────────────┐
              │   core/        │  │ infrastructure/│  │ plugins/security/ │
              │  domain/       │◄─┤ adapters       │  │ (cybersecurity    │
              │  application/  │  │ (Postgres event│  │  ONLY: secb run-  │
              │  ports/ (Proto)│  │  store, worker  │  │  time, roles,     │
              │  stdlib only   │  │  adapters)      │  │  CVE, prompts)    │
              └──────┬─────────┘  └────────────────┘  └─────────┬─────────┘
                     │ ports referenced by                       │ renders
              ┌──────▼──────┐  ┌──────────────┐  ┌───────────────▼─────────┐
              │presentation/│  │   query/     │  │ prompts/  (Jinja2,       │
              │ (CLI)        │  │ FastAPI+React│  │ 4-tier, no Python)       │
              │              │  │ CQRS read    │  │ domains/secbench/*       │
              └──────────────┘  └──────────────┘  └─────────────────────────┘
```

- `core/` — domain + application + ports. Never imports `infrastructure/`.
- `infrastructure/` — adapters implementing `core/ports/` protocols (Postgres event
  store, worker adapters).
- `plugins/security/` — **strictly cybersecurity**: the SEC-bench runtime, role catalog,
  `CVEInstance`, deliverables, prompt strategy. The *main* SEC-bench implementation — but,
  as built, security seams also live outside it: a `security` config section (`settings.py`),
  worker MCP config referencing `plugins.security.mcp` (`mcp_config.py:14,26`), and
  `run_invariants` deriving allowed bash commands from `settings.security`
  (`run_invariants.py:123-129`). Treat "plugins is the only place" as the intended boundary
  plus known drift, not a literal as-built fact.
- `bootstrap/` — wires plugins + infrastructure into core. Sole cross-boundary importer.
- `prompts/` — Jinja2 templates, 4-tier (§II.6). No Python.
- `query/` — FastAPI REST + React SPA, the read side of CQRS.

## II.2 — Event sourcing / CQRS

```mermaid
flowchart LR
    subgraph Write["Write side (commands)"]
        DM["domain method on aggregate<br/>(e.g. emit_runtime_surface_sealed)"]
        EM["_emit = _apply + append to _changes"]
        DM --> EM
    end
    EM -->|append-only| ES[("Postgres events table<br/>UNIQUE(aggregate_id, sequence_number)")]
    ES -->|load_from_history| RP["replay: fold events through @_apply<br/>version += 1 per event"]
    RP --> AGG["in-memory aggregate state"]
    ES -->|EVENT_TYPE_REGISTRY<br/>36 types| RD["Read side / CQRS<br/>hierarchy CTEs, BOSS projections,<br/>experiments eval (RunData)"]
    classDef store fill:#cde,stroke:#226;
    class ES store
```

- **Two event-sourced aggregates** (not one):
  - `AgentSession` — the **primary** aggregate; 33 of the 36 event types mutate it.
    — `core/domain/aggregates/agent_session.py`
  - `SharedStore` (= `ArtifactStore` + `DecisionLog`) — cross-agent shared state, with a
    `uuid5`-derived `aggregate_id` so it shares the same `events` table without colliding
    with `AgentSession`. 3 event types mutate it.
    — `core/domain/shared_context.py`
- **Replay & OCC.** State is never stored directly: an aggregate is rebuilt by
  `load_from_history(events)` — `AgentSession`'s first event must be `AgentCreated`,
  `SharedStore`'s must be `SharedContextCreated` (`shared_context.py:328`) — folding each
  event through a `@singledispatchmethod` `_apply` that mutates fields and bumps `version`. The
  sole concurrency guard is the `UNIQUE(aggregate_id, sequence_number)` constraint; an
  `asyncpg.UniqueViolationError` becomes a domain `ConcurrencyError`, and the losing
  writer reloads and retries. — `agent_session.py:558-573`,
  `infrastructure/adapters/postgres_event_store.py:179-280`
- **Postgres is the single source of truth.** The `events.jsonl` projection was retired
  (commit `2d669d0`); the harness no longer writes it and evaluators read DB events only.
  — `experiments/shared/harness.py:495-499`, `experiments/shared/evaluation/criteria.py:375-377`

> ⚠️ **Stale `CLAUDE.md`.** `CLAUDE.md` states "AgentSession is the only aggregate" and
> "All state from replaying ~31 frozen Pydantic events." Both are out of date: there are
> **36** registered event types (`postgres_event_store.py:51-97`) and **two** aggregates.
> Treat `AgentSession` as the *primary/central* aggregate. (See §V.6.)

### Event catalog (36 types)

All subclass `DomainEvent` (`model_config = {"frozen": True}`,
`core/domain/events/events.py:27`); all are registered in `EVENT_TYPE_REGISTRY`.

| Group | Events |
|---|---|
| Session lifecycle / hierarchy | `AgentCreated`, `TaskAssigned`, `StatusChanged`, `SubtasksDefined`, `ChildSpawned`, `ChildCompleted`, `ChildFailed`, `ComplexityEvaluated`, `RedecompositionTriggered`, `RetryScheduled`, `FailureDigestRecorded`, `LimitEnforced` |
| Assessment / decision | `ProbeStarted`, `ProbeCompleted`, `DecisionInfeasible`, `PromptSent` |
| Execution | `CodeGenerationStarted`, `ThoughtCaptured` |
| **Verification** | `VerificationPassed`, `VerificationFailed` (`failed_stage` ∈ structural/deterministic/execution/judge) |
| **Runtime-surface (anti-leak)** | `RuntimeSurfaceSealed` (carries `sealed_artifacts: list[SealedArtifact]`) |
| Completion / run-level / timing | `WorkCompleted`, `WorkFailed`, `RunStarted`, `RunCompleted`, `AgentExecutionStarted`, `AgentExecutionFinished`, `OperationStarted`, `OperationFinished` |
| Cost | `TokensConsumed`, `WorkerCostRecorded` |
| Code-prefix cache (AgentSession) | `SourceFileObserved`, `SourceFileEdited` |
| **Shared store** (`SharedStore`, not `AgentSession`) | `SharedContextCreated`, `ArtifactStored`, `DecisionRecorded` |

> **`ThoughtCaptured` is the persisted worker transcript.** Every tool call the worker
> makes — including each `secb …` bash command and its output observation — is streamed by
> the worker adapter as a `ThoughtCaptured` event (`content` + `output_type` ∈
> tool_use/tool_result, `tool_name`) and persisted to Postgres
> (`openhands_adapter.py:816-827`; `execution_service._persist_agent_events :1111-1116`). This is the
> event-sourced record of *what the agent actually executed* — see §IV.5 for how (and how
> weakly) it lets us prove `secb` really ran.

## II.3 — Execution modes: flat vs hierarchical

```
FLAT ARM (single agent runs all four phases in order)
   one WORKER ── Build ─▶ Exploit ─▶ Fix ─▶ Report
   no manager / sibling / handoff vocabulary

HIERARCHICAL ARM (BOSS → MANAGER → WORKER recursion)
                ┌──────── BOSS ────────┐         decomposition + verification
                │   assess + decompose │
        ┌───────┼───────────┬──────────┼─────────┐
     [Builder] [Exploiter] [Fixer]  [Reporter]   ← phase managers
        │           │          │          │
    leaf WORKERs prefixed with a [Role] from the catalog (§III)
```

Both arms share the **same phase contracts** (§II.6); the hierarchical arm adds role
decomposition on top. Worker leaves carry a `[Phase]`/`[Role]` bracket that routes them
to the right phase prompt (bracket-first detection prevents a Fixer leaf — which
legitimately mentions "exploit"/"repro" — from rendering the Exploiter prompt).
— `plugins/security/prompt_strategy.py:57-100`

## II.4 — The container runtime (`docker_runtime.py`) lifecycle

`DockerSecBenchRuntime` prepares one host workspace per run; `SecurityDomainPlugin` starts
one **long-lived** container for the first worker and **reuses** it (`_sessions[root_id]`,
`plugin.py:53-63,180-195`) for the rest of that run's Builder/Exploiter/Fixer/Reporter
workers — one container **per run**, not per worker. So Builder outputs persist for
downstream phases — a missing binary at the Exploiter step means the Builder genuinely
failed, not a handoff copy problem.

```
prepare_workspace(cve, run_output_path, image, root_id)          docker_runtime.py:152
  │  _ensure_image_exists(image)         inspect → pull <registry>/image → else "build it"
  │  docker create + docker cp  ─────────▶ host mirrors: runs/<id>/{src,testcase,work}
  │  _remove_forbidden_testcase_artifacts  delete model_patch.diff / gold*.patch / candidate_fix*
  │  _seed_runtime_scripts                 overwrite /testcase/repro.sh (exit-2 skeleton, 0755)
  │                                                  /testcase/patch.sh  (Arise harness, 0555)
  │  (computes <run>.sealed/secb-exec PATH only — sealed files are WRITTEN in start_session)
  ▼
start_session(cve, workspace, agent_id)                          docker_runtime.py:233
  │  assert every bind-mount target stays under this run's host_root
  │  re-seed patch.sh; WRITE sealed <run>.sealed/secb wrapper (_sealed_secb_wrapper, :265)
  │  docker run -d  <mounts below>  <image>  tail -f /dev/null
  │  git config safe.directory; chmod +x /src/build.sh; WRITE <run>.sealed/secb-exec (_write_exec_helper, :332)
  ▼
(B/E/F/R run via secb-exec → docker exec)                stop_session → docker rm -f
```

### Bind mounts (`start_session`, `docker_runtime.py:266-310`)

| Host source | Container target | Mode | Purpose |
|---|---|---|---|
| `host_source_dir` | `/src` | rw | Project source mirror |
| `host_testcase_dir` | `/testcase` | rw | SEC-bench artifacts + agent deliverables |
| `host_testcase_dir/patch.sh` | `/testcase/patch.sh` | **ro** | Read-only overlay of the patch harness |
| `host_work_root` | `/work` | rw | OSS-Fuzz scratch (`/work/bin`) |
| `host_root` | `/arise-run` | rw | Arise-only run root |
| `host_testcase_dir/patch.sh` | `/arise-run/testcase/patch.sh` | **ro** | Second RO overlay (via `/arise-run`) |
| `<host_root>.sealed/secb` | `/usr/local/bin/secb` | **ro** | Delegating `secb`, sourced from the **sealed** dir |

The sealed dir `<host_root>.sealed/` is a **sibling** of the run root with **no** bind
mount, so a root shell in the container has no filesystem route to the `secb` wrapper
source or the host-executed `secb-exec`. — `docker_runtime.py:546-566`

### The injected scripts (constants in `docker_runtime.py`)

- **`_SECB_WRAPPER`** (`:43-68`): `build` → `env -u REPLAY_ENABLED /usr/local/bin/compile`
  (stripping `REPLAY_ENABLED` so `compile` runs the agent-editable `$SRC/build.sh`, never
  a baked `replay_build.sh`); `repro` → `/testcase/repro.sh`; `patch` → `/testcase/patch.sh`.
- **`_REPRO_SKELETON`** (`:70-74`): prints "Arise seeded an empty /testcase/repro.sh;
  Exploiter must replace it" and `exit 2`. Non-golden by construction.
- **`_PATCH_SCRIPT`** (`:76-103`): applies `repo_changes.diff` idempotently (`git apply
  --check` → apply; reverse-check → "already applied"), then **requires** a non-empty
  `model_patch.diff` (`exit 1` otherwise) and applies it.

## II.5 — How the Arise image is built *from* the SEC-bench image

Arise does not author CVE images; it **layers tools on top** of the upstream patch image.

```
hwiwonlee/secb.eval.x86_64.<project>.<cve>:patch          (upstream SEC-bench, the FROM)
        │  deployment/build-secbench-tools.sh
        │     docker build -f deployment/secbench-tools.Dockerfile \
        │                  --build-arg BASE_IMAGE=<base> -t secb-tools:<id>-patch .
        ▼
secb-tools:<id>-patch        (local)
        │  deployment/push-study-images.sh   docker tag + docker push
        ▼
<registry>/secb-tools:<id>-patch   (e.g. cheshire0814/...)
        ▲
        │  at run time: DockerSecBenchRuntime._ensure_image_exists
        │     docker image inspect secb-tools:<id>-patch
        │       → miss + tools_image_registry set → docker pull <registry>/... → docker tag
        │       → else RuntimeError "Build it first with deployment/build-secbench-tools.sh"
```

The tag derivation `…:patch` → `secb-tools:<project>.<cve>-patch` is done by
`resolve_secbench_image()` (`plugins/security/image_resolver.py:26-42`); it returns the
base image unchanged when security tools are disabled.

**What the `deployment/secbench-tools.Dockerfile` layer adds** (`FROM ${BASE_IMAGE}`,
`USER root`):

| Layer | Adds |
|---|---|
| Core analysis tools (apt) | `valgrind`, `gdb`, `cppcheck`, `strace`, `ltrace`, `cflow`, `jq` + best-effort `libasan5/6`, `libubsan1` |
| KLEE (apt, best-effort) | `klee`, `klee-runtime`, `llvm`, `clang` (skipped if unavailable) |
| Verify | `valgrind --version` (+ optional `klee --version`) |
| **Claude Code CLI** | NodeSource `setup_20.x` → `nodejs` → `npm i -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}` |
| **security-tools MCP server** | `python3`/`venv` at `/opt/arise-mcp/venv`, `pip install "mcp[server]>=1.9.0"`, `COPY plugins/security/mcp/security_tools_server.py` |

A post-build smoke test asserts `claude --version`, the `FastMCP` import, and `valgrind
--version`. The registry default is `cheshire0814` (`config/config.yaml:151` →
`settings.security.tools_image_registry` → `bootstrap/composition.py:63-66`).

## II.6 — Prompt architecture & the system input contract

### 4-tier prompt (`core/application/services/prompt/prompt_builder.py:3-7, 275-293`)

```
Tier 1  system.j2            global hierarchy framing (BOSS→MANAGER→WORKER)
Tier 2  roles/worker.j2      persona / responsibility
Tier 3  operations/execution.j2   task-execution criteria / output format
Tier 4  domains/secbench/*   ← SecBenchPromptStrategy extender (CVE context)
        + volatile task/briefing block appended LAST
```

### Tier-4 composition — flat vs hierarchical (the shared-contract anchor)

```
FLAT  (extend_flat_prompt, prompt_strategy.py:337-364)
  _secb_runtime_contract.j2 → cve.j2 (phase=None) → flat_pipeline.j2
        └─ phases/_mindset.j2 + include phases/{build,exploit,fix,report}.j2
     (NO worker/*.j2; no manager/sibling/handoff wording)

HIER  (extend_worker_prompt, prompt_strategy.py:247-335)  — per worker leaf
  _secb_runtime_contract.j2 → cve.j2 (phase=branch) → worker.j2
        → worker/<branch>.j2  ──include──▶ phases/<branch>.j2  + role overlay steps
        → (optional) tools.j2
```

`phases/{build,exploit,fix,report}.j2` are `{% include %}`-ed by **both** arms and
rendered with identical context, so each phase's Goal + Deliverables + Validation block
is **byte-identical across arms** — proven by
`plugins/security/tests/test_prompt_normalization.py:106-148`. This is why the Builder-
Baseline and Fixer-Diff-Baseline procedures (which keep `model_patch.diff` free of
Builder hunks) live in the phase partials, not the worker overlays — so the flat arm
inherits them too.

### What the agent sees — the input contract

```
INJECTED (allowed)                              FORBIDDEN (never rendered)
  CVE display fields  ──▶ cve.j2                  _PROMPT_FORBIDDEN_FIELDS, stripped in
  contract context    ──▶ artifact paths,           CVEInstance.to_template_context():
                          phase_commands,             • patch          (gold fix)
                          required_files,             • candidate_fixes (fix bodies)
                          validation_required,        • secb_sh        (golden repro())
                          hierarchical_only, …      cve_instance.py:15-17, 106-110
```

**`_CVE_DISPLAY_FIELDS`** — the 11-field allowlist actually shown to the agent
(`prompt_strategy.py:29-41`, empty values dropped): `instance_id`, `cve_id`, `repo`,
`project_name`, `lang`, `sanitizer`, `base_commit`, `work_dir`, `bug_description`,
`sanitizer_report`, `bug_report`.

> `sanitizer_report` and `bug_report` are kept **on purpose** — they are the *expected-
> failure oracle* the Exploiter must match, not the answer. The fix (`patch`),
> alternative fixes (`candidate_fixes`), and the golden harness (`secb_sh`) are stripped.
> Golden strings appear in templates only as **prohibitions** in
> `_secb_runtime_contract.j2:11`. Verified absent from rendered prompts:
> `test_prompt_building.py:64-95, 119-148`.

### Retry semantics — what triggers a retry, and the cap

Only **WORKER** agents retry. **Two paths run in sequence** on a failed worker
(`execution_service.py:1139-1148`): (1) **verification-specific** — on a verification
failure with judge feedback, capped at `verification_max_retries` (default **2** ⇒ up to
**3** attempts); then if that declines, (2) the **generic `RetryPolicy`** — for any
still-failed worker, with budget = length of `orchestration.retry.model_escalation_chain`
plus model escalation + circuit breaker (`retry_policy.py:44,68-76`). With an empty
escalation chain (the common default) path (2) is a no-op and the 3-attempt verification
cap governs; with a configured chain, generic retries fire only while the existing
`agent.retry_count` is still below `len(model_escalation_chain)` — both paths share
`agent.retry_count` (`agent_session.py:371,627-633`), so it is **not** an extra budget
stacked on top of the 3-attempt cap. The diagram below shows path (1); path (2) is omitted
for clarity.

```mermaid
stateDiagram-v2
    [*] --> Running : CodeGenerationStarted
    Running --> Completed : worker run COMPLETED (agent_orchestrator.py:661)
    Completed --> Verify : VerificationPipeline.verify (verification_pipeline.py:133)
    Verify --> Passed : all stages + judge pass → VerificationPassed
    Verify --> Failed : any stage.check==False OR judge fail → VerificationFailed\nsets verification_feedback
    Failed --> RetryGate : _maybe_retry_verification (execution_service.py:1150)
    RetryGate --> Requeued : retry_count < 2 → schedule_retry (RetryScheduled)\nfeedback injected into retry prompt
    RetryGate --> TerminalFail : retry_count >= 2 (cap) OR role != WORKER → WorkFailed
    Requeued --> Running
    Passed --> [*]
    TerminalFail --> [*]
```

| Item | Value | Source |
|---|---|---|
| Config key / default / bounds | `verification_max_retries` / `2` / `ge=0,le=10` | `config/settings.py:486-489` |
| Attempt cap (verification path) | ≤ 3 (1 initial + 2 retries; `retry_count` 0→1→2) | `execution_service.py:1161` |
| Second path (generic) | `RetryPolicy` model-escalation, budget = `len(model_escalation_chain)`; runs after the verification-specific path declines; shares `agent.retry_count` (not an extra budget) | `execution_service.py:1144`, `retry_policy.py:44,68-76` |
| Trigger | a structural stage failing **or** the LLM judge failing on a COMPLETED worker | `verification_pipeline.py:138-172` |
| Enforcement site | `ExecutionService._maybe_retry_verification` | `execution_service.py:1150-1177` |

> The live verification loop checks **generic worker-output** stages
> (structural/deterministic/execution/judge); it does **not** re-check SEC-bench
> Built/Exploited/Fixed correctness (§IV.6).

---

# Part III — Roles, phases & artifacts

## III.1 — Phase ↔ `secb` mapping

| Phase | Drives | Required roles | Optional roles |
|---|---|---|---|
| **Builder** | `secb build` | Build-Setup, Build-Compiler, Build-Verifier | — |
| **Exploiter** | `secb repro` | Repro-Creator, Exploit-Validator | PoC-Researcher, Data-Flow-Analyst, PoC-Tester, Forward-Instrumentator |
| **Fixer** | `secb patch` → `secb build` → `secb repro` | Root-Cause-Analyst, Patch-Creator, Patch-Validator | Candidate-Reviewer, Regression-Tester, Fix-Aggregator |
| **Reporter** | (synthesis) | Reporter | — |

`plugins/security/roles.py` is the single source of truth (16 roles: **9 required, 7
optional**); `plugins/security/deliverables.py` is the artifact contract; a drift test
(`plugins/security/tests/test_roles.py`) keeps them — and the prompts — in lock-step.

## III.2 — Role dependency DAG

Solid `A → B` = **hard** edge (B `depends_on` A; B consumes an artifact A produces).
Dashed = **soft** input (`soft_depends_on`, advisory ordering). Blue = required, grey =
optional.

```mermaid
graph LR
  subgraph Builder
    BS[Build-Setup]:::req --> BC[Build-Compiler]:::req --> BV[Build-Verifier]:::req
  end
  subgraph Exploiter
    PR[PoC-Researcher]:::opt --> FI[Forward-Instrumentator]:::opt
    DFA[Data-Flow-Analyst]:::opt
    PT[PoC-Tester]:::opt
    RC[Repro-Creator]:::req --> EV[Exploit-Validator]:::req
    PR -.-> PT
    PR -.-> RC
    PT -.-> RC
  end
  subgraph Fixer
    RCA[Root-Cause-Analyst]:::req --> CR[Candidate-Reviewer]:::opt
    RCA --> PC[Patch-Creator]:::req --> PV[Patch-Validator]:::req --> FA[Fix-Aggregator]:::opt
    RT[Regression-Tester]:::opt
    FI -.-> RCA
    CR -.-> RT
  end
  REP[Reporter]:::req
  PV -.-> REP
  EV -.-> REP
  BV -.-> REP
  classDef req fill:#cde,stroke:#226,stroke-width:2px;
  classDef opt fill:#eee,stroke:#999,stroke-dasharray:3 3;
```

- **Required (9):** Build-Setup, Build-Compiler, Build-Verifier, Repro-Creator,
  Exploit-Validator, Root-Cause-Analyst, Patch-Creator, Patch-Validator, Reporter.
- **Optional (7):** PoC-Researcher, Data-Flow-Analyst, PoC-Tester, Forward-Instrumentator,
  Candidate-Reviewer, Regression-Tester, Fix-Aggregator.
- **Data-Flow-Analyst** is an isolated optional node (no hard/soft edges, no artifact).

> **Naming note.** The catalog has `Exploit-Validator` (required verdict) and `PoC-Tester`
> (optional empirical confirmation) — there is **no** `PoC-Validator` in source; that name
> appears only in historical `events.jsonl` run logs. Any rename must be an explicit
> catalog change with tests. — `roles.py:115,144`

## III.3 — Role catalog (full)

| Role | Phase | Req? | Hier-only artifact? | Produces |
|---|---|---|---|---|
| Build-Setup | Builder | ✅ | — | `base_commit_hash` |
| Build-Compiler | Builder | ✅ | — | `build.sh`, `repo_changes.diff`, `binary_paths.txt` |
| Build-Verifier | Builder | ✅ | — | — (runs binary under ASan; self-report) |
| PoC-Researcher | Exploiter | ⬜ | — | — (insight: `poc_operation_map.txt`) |
| Data-Flow-Analyst | Exploiter | ⬜ | — | — |
| PoC-Tester | Exploiter | ⬜ | — | — |
| Forward-Instrumentator | Exploiter | ⬜ | — | — (insight: `forward_instrumentation.log`) |
| Repro-Creator | Exploiter | ✅ | — | `poc_path.txt`, `repro.sh` |
| Exploit-Validator | Exploiter | ✅ | — | `exploit_validation_results.txt` |
| Root-Cause-Analyst | Fixer | ✅ | **yes** (`root_cause_analysis.txt`) | `root_cause_analysis.txt` |
| Candidate-Reviewer | Fixer | ⬜ | — | — |
| Regression-Tester | Fixer | ⬜ | — | — |
| Patch-Creator | Fixer | ✅ | — | `model_patch.diff` |
| Patch-Validator | Fixer | ✅ | — | `patch_validation_results.txt` |
| Fix-Aggregator | Fixer | ⬜ | — | `fix_summary.md` |
| Reporter | Reporter | ✅ | — | `security_report.md` |

The Root-Cause-Analyst emits a structured block with five keys
(`ROOT_CAUSE_BLOCK_KEYS`, `deliverables.py:170`): `PROPOSED_FIX_SITE`,
`PROPOSED_FIX_SITE_RATIONALE`, `ALTERNATIVE_SITE`, `ALTERNATIVE_SITE_REJECTED_BECAUSE`,
`RUNTIME_TYPE_TAG`.

## III.4 — Artifact contract

Status legend: **REQUIRED** = in `REQUIRED_FILES`; **VALIDATION** = verdict file in
`VALIDATION_REQUIRED` (mandatory on any path that includes the phase); **HIER-ONLY** =
mandatory only when the phase is decomposed; **EVIDENCE** = path constant exists but is
*not* mandated (lives in a role `insight`, not `produces`).

| Artifact (`/testcase/…` unless noted) | Producer | Status | Mechanical meaning |
|---|---|---|---|
| `base_commit_hash` | Build-Setup | REQUIRED | The vulnerable commit hash |
| `/src/build.sh` | Build-Compiler | REQUIRED | Project build recipe (sanitizer flags preserved) |
| `repo_changes.diff` | Build-Compiler | REQUIRED · **MAY_BE_EMPTY** | Builder source delta vs base; committed as `arise-builder-baseline` when non-empty |
| `binary_paths.txt` | Build-Compiler | REQUIRED | Exact Builder-produced executable path(s), one per line |
| `poc_path.txt` | Repro-Creator | REQUIRED | Pointer to the selected PoC (by content/CVE match, **not** a `poc*` glob) |
| `repro.sh` | Repro-Creator | REQUIRED | The `secb repro` backing script; invokes the Builder binary |
| `exploit_validation_results.txt` | Exploit-Validator | VALIDATION | Verdict block (PASS/FAIL + expected/observed sanitizer error, crash fn, determinism) |
| `root_cause_analysis.txt` | Root-Cause-Analyst | HIER-ONLY | 5-key fix-site block (mandatory only when Fixer is decomposed) |
| `model_patch.diff` | Patch-Creator | REQUIRED | git-apply-able Fixer diff (`git diff HEAD`, HEAD = builder baseline) |
| `patch_validation_results.txt` | Patch-Validator | VALIDATION | Verdict block (apply/build status + pre/post sanitizer + 3/3 no-crash) |
| `fix_summary.md` | Fix-Aggregator | EVIDENCE (optional role) | Concise validated fix narrative |
| `security_report.md` | Reporter | REQUIRED | Non-empty evidence-based report |
| `poc_operation_map.txt`, `forward_instrumentation.log`, `instrumentation_output.txt` | — (role insight only) | EVIDENCE | Novelty/evidence outputs, not key-file failures |

**Injected infra (not agent deliverables):** the delegating `/usr/local/bin/secb`, the
empty `/testcase/repro.sh` skeleton, the immutable `/testcase/patch.sh`.

> **Correction vs older notes:** `packages.txt` is **not** an optional artifact — it has
> **no** key in `deliverables.py` and is deliberately *excluded* from the mandatory
> contract (asserted absent: `test_roles.py:131`, `test_prompt_building.py:722`). Likewise
> there is no `poc*` glob — the contract uses the explicit `poc_path.txt` pointer.

## III.5 — Anti-leak model (allowed vs forbidden)

```
ALLOWED into prompt / container                FORBIDDEN to the agent
  • 11 CVE display fields (incl. the             • CVEInstance.patch / candidate_fixes / secb_sh
    sanitizer_report + bug_report ORACLE)          (stripped from template context)
  • /src/build.sh                                • baked golden secb / repro() body / replay_build.sh
  • shipped PoC files in /testcase                 (secb wrapper overlaid from sealed dir;
    (any filename)                                  REPLAY_ENABLED stripped)
  • user-facing secb build|repro|patch           • golden /testcase/repro.sh (re-seeded to exit-2)
  • the generic (non-golden) patch.sh            • model_patch.diff / gold*.patch / candidate_fix*
                                                   (deleted from the seeded testcase dir)
```

The prepared runtime-surface descriptor is recorded in the DB as a **single**
`RuntimeSurfaceSealed` audit event (`sealed_artifacts`: container_path + kind + non_golden +
sha256 for `repro_skeleton`, `patch_script`, `secb_wrapper`), emitted at run-preparation /
boss-creation time (`execution_service.py:263-273`) from deterministic constants — it is an
audit descriptor, **not** a per-overwrite event for each later container write. The actual
seeding/overwrite happens in the runtime (`docker_runtime.py:30-41, 201-231, 507-577`); the
`secb` wrapper in particular is installed later, in `start_session`, *after* this descriptor
event is emitted.

---

# Part IV — Success / Fail criteria

Arise judges a run in two planes a non-LLM reader can both reach:
1. **On-disk artifacts** under `runs/<run_id>/`, resolved from container paths via
   `container_path_to_disk` (`experiments/shared/evaluation/common.py:392`); non-vacuity
   = present AND `size>0`.
2. **The Postgres event stream** — the single source of truth (`events.jsonl` retired).

> ⚠️ **Read this before trusting any verdict.** `criteria.py:3-16` self-labels
> **OBSOLETE / KNOWN-WRONG — DO NOT TRUST THESE SUCCESS VERDICTS.** Only the file-presence
> /non-vacuity subset (`key_file_exists`, `artifacts_by_bef`, `success_criteria_by_bef`)
> is production-wired. Every Built/Exploited/Fixed *success* judge is currently UNWIRED
> (see the honesty table §IV.6). The gates below describe what a script *can*
> deterministically check; §IV.6 says what actually runs today.

## IV.1 — Pass/fail decision flow (target/advisory — not live enforcement)

The flow below is the *intended* gate sequence (phases run in catalog order
Builder → Exploiter → Fixer → Reporter). **As built it is mostly advisory:** only the
anti-leak seal (live in the runtime) and deliverable presence/non-vacuity (reported by
`bef.py`/`linear.py`) actually run; the Built/Exploited/Fixed *success* gates are
unwired/obsolete (criteria banner, §IV.6). Read it as the contract a future enforcement
pass would apply, not what fails a run today.

```mermaid
flowchart TD
    START([Run start: AgentCreated]) --> SEAL{Anti-leak gate}
    SEAL -->|RuntimeSurfaceSealed present;<br/>kinds = repro_skeleton,patch_script,secb_wrapper| BUILD
    SEAL -->|missing / forbidden artifact leaked| XLEAK([FAIL: answer-key leak])
    BUILD{Builder REQUIRED_FILES<br/>+ build.exit + real ELF}
    BUILD -->|base_commit_hash, build.sh, repo_changes.diff,<br/>binary_paths.txt present; build.exit = exit=0;<br/>binary_paths resolve to real ELF| EXPLOIT
    BUILD -->|missing deliverable / build.exit != 0 /<br/>binary_paths points at non-binary| XBUILD([FAIL: build])
    EXPLOIT{poc_path.txt + repro.sh<br/>+ exploit_validation_results.txt}
    EXPLOIT -->|poc_path resolves; repro.sh invokes Builder binary;<br/>VERDICT: PASS + DETERMINISM_RUNS: 3/3| FIX
    EXPLOIT -->|dangling PoC / broken handoff /<br/>verdict not PASS or not 3/3| XEXP([FAIL: exploit])
    FIX{model_patch.diff valid<br/>+ patch_validation_results.txt}
    FIX -->|diff --git + @@ hunk; patch_exit=0; VERDICT: PASS,<br/>PATCH_APPLY: clean, BUILD: success,<br/>REPRO_RUNS_NO_CRASH: 3/3| REPORT
    FIX -->|no/empty/leaked patch / apply or build fail /<br/>not 3/3 crash-free| XFIX([FAIL: fix])
    REPORT{security_report.md non-vacuous}
    REPORT -->|present and size > 0| PASS([PASS])
    REPORT -->|empty / missing| XREP([FAIL: report])
    classDef fail fill:#f9d,stroke:#922;
    classDef ok fill:#cde,stroke:#226;
    class XLEAK,XBUILD,XEXP,XFIX,XREP fail
    class PASS ok
```

## IV.2 — Mechanical gates (per role / phase)

A script *can* check each row from disk + supplied DB events. "Blocking?" = whether a
failure *should* fail the run under full enforcement — **not** that it does today (most
success rows are unwired, §IV.6; the live-wired rows are the anti-leak seal and deliverable
presence). Names are the exact `deliverables.py` / `criteria.py` constants.

| Gate | Check | Blocking? | Artifact / event |
|---|---|---|---|
| **Anti-leak seal** | `RuntimeSurfaceSealed` in DB; `∪ kind == {repro_skeleton, patch_script, secb_wrapper}`; no forbidden artifact in seeded `/testcase` | **yes** | `RuntimeSurfaceSealed`, `SealedArtifact` |
| **Builder — REQUIRED_FILES** | `base_commit_hash`, `build.sh`, `binary_paths.txt` non-vacuous; `repo_changes.diff` present (may be 0-byte) | **yes** | `REQUIRED_FILES['Builder']`; `_MAY_BE_EMPTY` |
| **Builder — build.exit** | `testcase/build.exit` ends with `=0` / contains `exit=0` | **yes** | `build.exit` sentinel; `criteria.py:444-446` |
| **Builder — real ELF** | each `binary_paths.txt` line resolves to a file whose first 4 bytes == `b"\x7fELF"` *(target; current check is a size+name hack, UNWIRED)* | **yes** *(target)* | `binary_paths.txt` |
| **Exploiter — REQUIRED_FILES** | `poc_path.txt` & `repro.sh` non-vacuous; PoC pointer resolves to a real file under run root | **yes** | `_selected_poc` `criteria.py:404` |
| **Exploiter — repro→binary handoff** | `repro.sh` has shebang and invokes the Builder-declared binary, which resolves on disk; unparseable → never a silent pass | **yes** | `_extract_repro_binary` / `_resolve_repro_binary` |
| **Exploiter — verdict** | `exploit_validation_results.txt` + `VERDICT: PASS` AND `DETERMINISM_RUNS: 3/3` | **yes** | `EXPLOIT_VALIDATION_FIELDS`; `criteria.py:597` |
| **Exploiter — determinism loop ran** | `repro_loop.exit == done` and ≥3 `repro_run_*.log`, each with an `exit=` line | warning | `validation_macros.j2:11-14` |
| **Fixer — model_patch.diff** | non-empty, valid unified diff (`diff --git` + ≥1 `@@`); presence proves in-run authorship (pre-seeded copy removed) | **yes** | `criteria.py:634-635`; removal `docker_runtime.py:569-577` |
| **Fixer — clean apply** | `fix_loop.log` shows `patch_exit=0` (+ `build_exit=0`); apply runs through the immutable 0555 `patch.sh` | **yes** | `validation_macros.j2:21-24`; `_PATCH_SCRIPT` |
| **Fixer — patch is Fixer-only** | no hunk in `model_patch.diff` is byte-identical to a `repo_changes.diff` hunk (symptom check) | warning | `build.j2:25-42`, `fix.j2:17-40` |
| **Fixer — verdict** | `patch_validation_results.txt` + `VERDICT: PASS`, `PATCH_APPLY_STATUS: clean`, `BUILD_STATUS: success`, `REPRO_RUNS_NO_CRASH: 3/3` | **yes** | `PATCH_VALIDATION_FIELDS`; `criteria.py:655-657` |
| **Fixer — root-cause block** *(decomposed only)* | `root_cause_analysis.txt` + all 5 `ROOT_CAUSE_BLOCK_KEYS` | warning | `HIERARCHICAL_ONLY['Fixer']` |
| **Reporter** | `security_report.md` present + non-vacuous (sections are an LLM concern) | **yes** | `REQUIRED_FILES['Reporter']` |
| **Lifecycle** | first event == `AgentCreated`; each WORKER reaches one terminal `WorkCompleted`/`WorkFailed`; retry chain respects ≤3 | warning | replay invariant `agent_session.py:558` |

## IV.3 — Qualitative / LLM-judge rubric (target-state, currently DEFERRED)

Most of these judges are **target-state** (DEFERRED): the three `build_*_success_llm_prompt`
builders are UNWIRED and `RunData` does not yet thread `CVEInstance` (it carries only
`run_id`, `events`, `run_dir`, `manifest` — `experiments/shared/evaluation/models.py:64-77`).
Each judge compares observed agent artifacts + DB events against the CVE **golden/reference**
fields.

> **Anti-leak nuance (important — see §II.6).** "Golden" here means *ground-truth expected
> values*, **not** *secret-from-the-agent*. `sanitizer_report` / `bug_report` /
> `bug_description` are the expected-failure **oracle that is ALREADY rendered to the agent**
> (`_CVE_DISPLAY_FIELDS`) — the judge merely re-checks observed-vs-oracle; it introduces no
> data the agent didn't see. Only the gold `patch` / `candidate_fixes` / `secb_sh` are
> host-side-secret (`_PROMPT_FORBIDDEN_FIELDS = {patch, candidate_fixes, secb_sh}`,
> `cve_instance.py:15`), and of those only the patch-correctness judge optionally consults
> the gold `patch`. *Hiding* the oracle from the agent would itself be a target-state
> anti-leak change, not current behavior.

The **first** judge below is different — a *provenance precondition* that needs no reference
data at all: it cross-checks the agent's event-sourced transcript to catch fabricated
verdicts (§IV.5).

| Judge | Inputs (observed + **golden**) | What it compares | Verdict |
|---|---|---|---|
| **★ Execution-provenance / anti-fabrication** (precondition; needs no reference data) | observed: the worker's persisted `ThoughtCaptured` transcript (tool_use/tool_result) for the Exploiter/Fixer subtree, the verdict files, `repro_run_*.log` / `*.exit` sentinels | the transcript actually contains the `secb repro`/`patch`/`build` **launch** AND a read-back of a *real* crash / exit (not an empty or `echo`-ed file), with ≥3 genuine runs corroborating `DETERMINISM_RUNS: 3/3`; the verdict-file PASS is backed by what actually ran | `{verdict, ran_secb, runs_observed, fabrication_suspected, reason, evidence_refs}` — reject a PASS the transcript does not back (the observed hallucination) |
| **★ Exploit sanitizer-match** (your headline example) | observed: `OBSERVED_SANITIZER_ERROR`, `CRASH_FUNCTION_OBSERVED`, `DETERMINISM_RUNS`, raw `repro_run_*.log`, `repro.sh`, `poc_path.txt`, DB events. **golden:** `sanitizer_report`, `bug_report`, `bug_description`, `expected_sanitizer_error` | a real sanitizer abort actually fired (not just an asserted PASS) **AND** observed error == golden error type **AND** crash fn matches the golden `bug_report` frame **AND** 3/3 | `{verdict, confidence, reason, evidence_refs}`; reason must cite the golden-vs-observed comparison |
| Exploit same-class/same-site | observed: crash fn, corruption origin, top-N frames, optional `forward_instrumentation.log`. **golden:** `bug_report`, `bug_description`, `sanitizer` | `matched_error_type` (heap-overflow ≠ use-after-free) AND `matched_crash_site` (frame aligns to golden function) | `{verdict, matched_error_type, matched_crash_site, …}` |
| Root-cause soundness | observed: 5-key `root_cause_analysis.txt`, corruption origin. **golden:** `bug_description`, `bug_report`, `sanitizer_report` | rationale explains the golden corruption mechanism (causal, not a crash-line restatement); origin upstream of the crash; principled alternative rejection | `{verdict, confidence, reason, evidence_refs}` |
| Patch correctness & minimality | observed: full `model_patch.diff`, `patch_validation_results.txt`, `PROPOSED_FIX_SITE`, DB `SourceFileEdited`. **golden:** `bug_description`, `sanitizer_report`; optional host-side `patch` as reference | post-patch sanitizer none + 3/3 + clean + success; diff edits the root-cause site; **`over_broad_suppression=false`** (must NOT disable the sanitizer / strip `-fsanitize` / widen the allocation / blanket try-catch) | `{verdict, over_broad_suppression, fixes_root_cause, …}` |
| Report evidence-quality | observed: `security_report.md` + all upstream verdict/log artifacts + DB events. **golden:** `sanitizer_report`, `bug_report`, `bug_description` | required sections present; every claim traces to a real artifact; stated error/site agree with golden (no narrating an unreproduced crash) | `{verdict, confidence, reason, evidence_refs}` |

## IV.4 — How criteria read the event store (not files)

**Invariant (holds today; target preserves it):** every mechanical function takes an
explicit `events: list[dict]` argument and **never** reads on-disk `events.jsonl`
(`criteria.py:375-377`; regression `test_criteria.py:139`).

The events the wired path consults are those that actually exist: `AgentCreated`,
`WorkCompleted`/`WorkFailed`, `VerificationPassed`/`VerificationFailed`, `ChildSpawned`
(drives `_is_hierarchical`), `SourceFileEdited` (edit provenance for `artifacts_by_bef`),
and `RuntimeSurfaceSealed` (DB-only anti-leak audit). There is **no** dedicated
per-stage "validation-result" event beyond `VerificationPassed`/`VerificationFailed`; the
Built/Exploited/Fixed verdicts are computed *from disk* + supplied events (§V.2).

## IV.5 — Execution provenance: how we know `secb` ran (and the fabrication gap)

The agent runs `secb build|repro|patch` as **detached** bash commands that redirect output
to **files**; separately, the worker adapter streams the agent's tool calls into the event
store. Two channels, very different trust levels:

```
secb build|repro|patch  ← agent runs it DETACHED:  nohup bash -c 'secb repro > run.log 2>&1' &
   │                        (_validation_macros.j2:3-30; _detached_exec.j2:9)
   ├─▶ stdout/stderr/exit ─▶ FILES in /testcase (agent-WRITABLE):  build.exit, repro_run_*.log,
   │                          fix_loop.log, exploit/patch_validation_results.txt
   │                              └─▶ read post-hoc by criteria.py  (mechanical, file-based)
   │
   └─▶ the bash TOOL CALL  ─▶ ThoughtCaptured event (output_type tool_use / tool_result),
        (worker SDK callback)   persisted to Postgres, replayed into AgentSession
                                (openhands_adapter.py:816-827; execution_service._persist_agent_events :1111-1116)
```

| Question | Answer | Evidence |
|---|---|---|
| Did the agent *issue* `secb …`? | **Yes — in events**, as a generic `ThoughtCaptured` tool_use (command text in `content`). | `ThoughtCaptured` `events.py:219-231` |
| What did `secb` *output / exit*? | **Only weakly.** Launched detached → crash/exit go to `/testcase` files; the launcher's `tool_result` shows just "launched … pid N". The real output enters events **only if the agent reads the file back**. | `_validation_macros.j2:3-30`; `CmdRunObservation → tool_result` `openhands_adapter.py:356-364` |
| Is there an authoritative `secb` *result* event? | **No.** No `SecbCommandExecuted` / dedicated exit-code event exists; the verdicts + `DETERMINISM_RUNS` live in agent-authored **files**, not events. | `events.py` (none); `criteria.py` reads files |

**The fabrication gap (the hallucination observed in practice).** Because the verdict is an
agent-writable file and the result is *not* authoritatively event-sourced, an agent can
write `VERDICT: PASS` / `DETERMINISM_RUNS: 3/3` **without ever launching `secb repro`** and
still pass every file-presence / string-match gate (it can even `echo` fake
`repro_run_*.log` files). The only hard-to-forge signal is the **event-sourced
`ThoughtCaptured` transcript** — append-only, written by the orchestrator from worker-SDK
callbacks, not by the agent's shell — where a missing `secb repro` launch or a missing
real-crash observation gives the fabrication away. Nothing cross-checks the verdict file
against that transcript today. Two fixes:

- **Near-term (LLM judge):** the **Execution-provenance / anti-fabrication** judge (§IV.3)
  reads the transcript + verdict files and rejects a PASS the transcript does not back.
  This is the sub-criterion for the self-reported-verdict / determinism hallucination.
- **By construction:** event-source the `secb` results themselves (§V.7) so the verdict is
  read from an agent-unforgeable event instead of inferred from a transcript.

## IV.6 — Mechanical vs LLM-judged vs not-yet-enforced (honesty table)

| Criterion | Currently mechanical (wired) | LLM-judged (wired) | Not-yet-enforced (gap) |
|---|---|---|---|
| Deliverable presence + non-vacuity | ✅ BEF: `artifacts_by_bef` / `success_criteria_by_bef` / `key_file_exists`; linear: `_artifacts_linear` / `_success_linear` + `key_file_exists` (`linear.py:61,84,102`) | — | — |
| Anti-leak sealing | ✅ live in `docker_runtime.py`, audited by `RuntimeSurfaceSealed` | — | golden file under a name outside `_FORBIDDEN_TESTCASE_ARTIFACTS` survives |
| Build success (real ELF) | ✅ presence of `binary_paths.txt` only | — | `built_success_mechanical` UNWIRED; ELF check is size+name hack; globs a **phantom** `build_verification*` no prompt emits |
| Exploit verdict (PASS + 3/3) | partial: verdict-file **presence** wired | — | never re-runs `secb repro`; accepts ANY sanitizer token, not the CVE's expected error |
| **`secb` actually executed (vs fabricated verdict)** | the agent's tool-call transcript IS event-sourced (`ThoughtCaptured`) | — | nothing cross-checks the verdict file against the transcript; `VERDICT`/`DETERMINISM_RUNS` are agent-written & forgeable → **anti-fabrication judge §IV.3 is the mitigation, §V.7 the by-construction fix** |
| Exploit triggers the CVE's exact error/site | — | — (target judges UNWIRED) | wrong-reason crash passes; only agent self-report compares |
| Repro→binary handoff | strict logic exists | — | **UNWIRED** (test-only); live defense is prompt self-report |
| Patch valid + clean apply + 3/3 | partial: verdict-file **presence** wired | — | `fixed_success_mechanical` **fully dead**; nothing re-applies/re-builds post-hoc |
| Patch fixes bug ≠ suppresses sanitizer | — | — (UNWIRED) | **most dangerous gap**: `secb build` runs the agent's `build.sh`, so `-fsanitize` can be stripped and a non-fix passes |
| Patch is Fixer-only | — | — | prompt-only discipline; no gate scans the diff for build-system paths |
| Root-cause soundness | block-key presence (decomposed only) | — (net-new judge UNWIRED) | crash-site-as-root-cause not caught without golden `bug_description` |
| Report evidence-backed | ✅ empty/whitespace caught | — (report judge UNWIRED) | nonempty-but-fabricated report passes |
| Built/Exploited/Fixed during the run (live retry) | — | structural/deterministic/execution/judge over **generic** output drives retry | the live pipeline does NOT re-check SEC-bench correctness; a well-formed-but-false deliverable survives |

**Three gate tiers to keep distinct.** (A) runtime/anti-leak in `docker_runtime.py` —
strong, live, mechanical; (B) in-prompt PASS blocks in `phases/*.j2` — agent **self-grade**
transcribed into verdict files, *not* independent; (C) post-hoc `criteria.py` success
judges — correct-ish logic but UNWIRED. Strongest guarantee: golden-data anti-parroting
(tier A). Weakest: sanitizer-suppression, wrong-reason-crash, flaky-repro, fabricated-
verdict — all need `CVEInstance` ground truth threaded through `RunData` **and**
independent host-side re-execution, neither of which exists today.

---

# Part V — Target state & known gaps

Consolidated forward-looking work. None of these block reading the as-built description
above; each is a concrete, isolated improvement.

## V.1 — `patch.sh` sealing (P1, deferred)

**Gap (confirmed).** Unlike `secb` (sourced from the unmounted `<run>.sealed/` dir), the
`patch.sh` **source** is `host_testcase_dir/patch.sh` — reachable and writable from the
container via the `/testcase` and `/arise-run` rw mounts; only the additional `:ro`
overlays are read-only. — `docker_runtime.py:264, 286-299, 533-543` vs sealed `secb`
`557-566`. Today's integrity is an emergent stack (`0o555` + RO overlay + per-container
re-seed), not by-construction.

**Fix.** Mirror `secb`: write `_PATCH_SCRIPT` into `_sealed_dir`, RO-mount both
`/testcase/patch.sh` and `/arise-run/testcase/patch.sh` from there, stop seeding it into
the agent-writable testcase dir (keep seeding `repro.sh` — it is legitimately agent-
editable). `SealedRuntimeSurface` already records the sha, so it stays consistent.

## V.2 — Criteria CVE-awareness (unwired)

- Thread the **host-side golden** `CVEInstance` (or a redacted projection) into `RunData`
  at the harness/composition layer — without importing `plugins/security` into the eval
  package — so judges match against `expected_sanitizer_error` / `sanitizer_report` /
  `bug_report` instead of hardcoded strings.
- **Wire** `built_/exploited_/fixed_success_mechanical` and the three
  `build_*_success_llm_prompt` builders into a consumer (`bef.py`/`linear.py` call none of
  them today; `fixed_success_mechanical` + two LLM-prompt builders have **zero** refs).
- Replace the "any `asan|valgrind|heap-buffer|stack-buffer` token" acceptance
  (`criteria.py:597`) with an exact match to the CVE oracle.
- Stop referencing the **phantom** `build_verification_summary.md` / `build_verification*`
  glob (no prompt emits it); re-point at `build.exit` / `binary_paths.txt`.
- Replace the ELF size+name-allowlist hack with real `b"\x7fELF"` magic.

## V.3 — Per-validation-result events (optional)

The detailed SEC-bench Built/Exploited/Fixed verdicts live in `runs/<run_id>/` files
(computed mechanically), not as events. If full event-source coverage of verification is
wanted, add dedicated per-stage events; otherwise the file-based verdicts plus
`VerificationPassed`/`VerificationFailed` remain the contract.

## V.4 — Live host-side re-execution (open question)

No gate ever independently re-runs `secb repro` or re-applies the patch on the host;
`DETERMINISM_RUNS: 3/3`, exploit PASS, and `patch_validation` PASS are agent-self-reported
(at most string-matched by UNWIRED code). Decide: add a host-side re-execution harness, or
rely on the CVE-aware LLM judge over the transcripts. See §IV.5 (the fabrication gap) and
§V.7 (event-sourcing `secb` results) — the same root cause.

## V.5 — Anti-leak denylist → allowlist (residual)

`_remove_forbidden_testcase_artifacts` deletes only the **hardcoded** filename set; a
golden file shipped under any other name in the agent-writable `/testcase` survives.
Consider seeding by allowlist (delete everything not explicitly permitted).

## V.6 — Documentation drift to fix

`CLAUDE.md` → "Key Architectural Decisions" should be updated: "AgentSession is the only
aggregate" → *primary* aggregate (a second `SharedStore` aggregate exists); "~31 frozen
events" → **36**. — verified `postgres_event_store.py:51-97`, `shared_context.py`.

## V.7 — Event-source the `secb` results (anti-fabrication by construction)

**Gap.** `secb build|repro|patch` runs as the agent's own **detached** bash; its exit code
and sanitizer output land in agent-writable `/testcase` files, and only a *generic* tool
transcript (`ThoughtCaptured`) is event-sourced (§IV.5). So `VERDICT: PASS` /
`DETERMINISM_RUNS: 3/3` are agent-authored and forgeable — the hallucination observed in
practice (a PASS written without `secb repro` ever running).

**Fix.** Route `secb` through an Arise-mediated execution channel that emits a dedicated
`SecbCommandExecuted` event — verb, exit code, output sha256 + bounded excerpt — captured
**outside the agent's control**, so determinism (3/3) and the Built/Exploited/Fixed verdicts
become verifiable from the DB and agent-unforgeable. This upgrades the §IV.3 anti-fabrication
judge from "infer from the transcript" to "read the authoritative event." Note the current
in-container `secb` wrapper has **no** agent-unreachable write channel (the sealed dir is
not mounted), so this needs a host-mediated `secb` path (e.g. via `secb-exec` / the
security-tools MCP server) rather than an in-container append.

---

# Appendix A — Key-file index

| Concern | File(s) |
|---|---|
| Container runtime / image / seal | `plugins/security/docker_runtime.py`, `container_runtime.py`, `image_resolver.py` |
| Image build | `deployment/build-secbench-tools.sh`, `deployment/secbench-tools.Dockerfile`, `deployment/push-study-images.sh` |
| CVE instance / anti-leak fields | `plugins/security/cve_instance.py` |
| Role catalog | `plugins/security/roles.py` |
| Artifact contract | `plugins/security/deliverables.py` |
| Drift tests | `plugins/security/tests/test_roles.py`, `test_prompt_building.py`, `test_prompt_normalization.py` |
| Prompt strategy / branch detection | `plugins/security/prompt_strategy.py` |
| Prompt templates | `prompts/system.j2`, `prompts/roles/`, `prompts/operations/`, `prompts/domains/secbench/{_secb_runtime_contract,cve,flat_pipeline,_validation_macros}.j2`, `prompts/domains/secbench/{phases,worker}/*.j2` |
| Events / aggregates | `core/domain/events/events.py`, `core/domain/aggregates/agent_session.py`, `core/domain/shared_context.py` |
| Event store / OCC | `infrastructure/adapters/postgres_event_store.py`, `infrastructure/sql/create_events_table.sql` |
| Verification / retry | `core/application/services/orchestration/verification_pipeline.py`, `core/application/execution_service.py`, `config/settings.py` |
| Evaluation / criteria | `experiments/shared/evaluation/{criteria,common,loading,models}.py`, `bef.py`, `linear.py` |
| Composition root | `bootstrap/composition.py`, `bootstrap/infrastructure.py` |

# Appendix B — Glossary

| Term | Meaning |
|---|---|
| **Patch image** | SEC-bench `:patch`-tagged image: vulnerable source + build wrapper + testcase, **no** fix |
| **Golden** | Any baked-in answer (gold patch, golden `secb`/`repro()`, candidate fixes) the agent must never see |
| **`secb`** | The command harness; Arise overlays a delegating wrapper (`build`→`compile`, `repro`→`repro.sh`, `patch`→`patch.sh`) |
| **`repo_changes.diff`** | Build/setup baseline delta (replayed before patch); **not** the fix |
| **`model_patch.diff`** | The Fixer deliverable — the candidate security fix |
| **Sealed dir** | `<run>.sealed/`, a sibling of the run root with no bind mount; holds `secb` + `secb-exec` |
| **Builder baseline** | Local `arise-builder-baseline` commit of `repo_changes.diff`, so the Fixer's `git diff HEAD` carries only Fixer edits |
| **Flat / hierarchical arm** | Single agent doing all phases vs BOSS→MANAGER→WORKER decomposition |
| **Oracle** | `sanitizer_report` + `bug_report` — the expected failure the Exploiter must match (shown to the agent on purpose) |
| **OCC** | Optimistic concurrency control via `UNIQUE(aggregate_id, sequence_number)` |
| **Wired / unwired** | Whether a `criteria.py` function is actually reached by a production consumer (`bef.py`/`linear.py`) |

---

*Last verified against source: 2026-06-14. When the code and this document disagree, the
code wins — update this file (and `CLAUDE.md`, §V.6) rather than trusting the prose.*
