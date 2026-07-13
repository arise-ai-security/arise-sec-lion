<!-- Read this when: working on the security plugin or SEC-bench pipeline -->
The security plugin (`plugins/security/`) implements SEC-bench vulnerability analysis as the sole domain plugin. It supplies the generic core with a `DomainPlugin`, `PromptStrategy`, `DecompositionValidator`, and -- when procedural dispatch is enabled -- a `ProcedureExecutorPort` implementation for deterministic security procedures.

## Boundary Rules

`plugins/security/` is **strictly** for cybersecurity code. Topology, orchestration, scheduling, and any non-security logic must never be placed here.

The plugin never imports from `infrastructure/`. It defines its own `SecurityContainerRuntime` protocol in `container_runtime.py`, and `DockerSecBenchRuntime` (the concrete adapter) lives in `docker_runtime.py` within the plugin itself.

`bootstrap/composition.py` is the **only** file outside `plugins/` that imports from `plugins/security/`. It imports `SecurityDomainPlugin` and `DockerSecBenchRuntime`. If you find yourself importing from `plugins/security/` anywhere else, you are violating the architectural boundary.

## File Map

| File | Purpose |
|---|---|
| `plugin.py` | `SecurityDomainPlugin` -- implements `DomainPlugin` protocol |
| `prompt_strategy.py` | `SecBenchPromptStrategy` -- implements `PromptStrategy` protocol; `detect_benchmark_branch()` and `_detect_branch_from_task()` |
| `roles.py` | The 16-role SEC-bench catalog, phase ownership, deliverables, hard/soft dependencies, and required/optional status |
| `decomposition_validator.py` | Adaptive B4 fixed phase skeleton, compact/escalated role routes, and role-fused treatment policy |
| `cve_instance.py` | `CVEInstance` frozen Pydantic model (17 stored fields, 6 computed) |
| `cve_inference.py` | `CVEInstanceInferenceService` -- regex extraction from task text; `CVEInferenceError` |
| `container_runtime.py` | `SecurityContainerRuntime` protocol + `SecBenchWorkspace` / `SecBenchContainerSession` frozen dataclasses |
| `docker_runtime.py` | `DockerSecBenchRuntime` -- concrete Docker-backed implementation of `SecurityContainerRuntime` |
| `image_resolver.py` | `resolve_secbench_image()` -- Docker image name remapping to tool-enriched variants |
| `security_tool.py` | `SECURITY_TOOL_REGISTRY` with Valgrind and KLEE definitions; `get_tools_for_phase()` |
| `procedures.py` | `SecBenchProcedureExecutor` -- implements `ProcedureExecutorPort` for four deterministic roles; `ProcedureSession` protocol |
| `crash_signature.py` | `compute_crash_signature()` / `signatures_match()` / `CrashSignature` -- sanitizer-class/access-type/top-frame reduction for host-computed verdicts |
| `patch_plan.py` | Structured `PatchPlan`, validation, approval, and literal diff rendering for the mechanical patch-applier |
| `deliverables.py` | `ARTIFACT_PATHS` / `PHASE_COMMANDS` -- the on-disk artifact + verdict-file contract shared by the agentic and procedural paths |
| `__init__.py` | Public exports (re-exports key symbols) |

## Connection to Core

### DomainPlugin protocol

Defined in `core/ports/domain_plugin_port.py`. `SecurityDomainPlugin` in `plugin.py` implements it. Core treats the plugin as opaque; only bootstrap knows the concrete type.

```
cat core/ports/domain_plugin_port.py
```

Methods implemented by `SecurityDomainPlugin`:

| Method | Signature | What it does |
|---|---|---|
| `infer_context` | `(task_text: str, **kwargs) -> object \| None` | Loads `CVEInstance` from `--cve-file` JSON or infers via regex from task text |
| `enrich_prompt` | `(prompt: str, *, domain_context, briefing, chain_factory) -> str` | Appends security tool instructions (Valgrind/KLEE) based on detected phase |
| `get_run_metadata` | `(domain_context: object) -> JsonObject` | Returns `{"instance_id": ...}` for event store persistence |
| `get_tag_mappings` | `() -> dict[str, SectionProvenance]` | Maps 15 prompt template tags to `SectionProvenance.SYSTEM` |
| `get_provenance_patterns` | `() -> list[tuple[str, SectionProvenance]]` | Returns regex pattern for security-related tag detection |
| `prepare_run` | `(*, root_id, run_output_path, domain_context) -> PreparedRunWorkspace \| None` | Creates host workspace via container runtime, caches in `_workspaces[root_id]` |
| `prepare_worker_execution` | `(*, root_id, agent_id, run_output_path, domain_context) -> WorkerExecutionContext \| None` | Starts a Docker container session, caches in `_sessions[root_id]` |
| `cleanup_worker_execution` | `(*, root_id, agent_id, domain_context) -> None` | Intentional no-op; the run-shared container persists until process cleanup |
| `get_prompt_strategy` | `() -> PromptStrategy \| None` | Returns `SecBenchPromptStrategy()` |
| `get_decomposition_validator` | `() -> DecompositionValidator \| None` | Returns the adaptive/full/role-fused SEC-bench decomposition policy |
| `get_procedure_executor` | `() -> ProcedureExecutorPort \| None` | Returns the four-role deterministic procedure executor |

### PromptStrategy protocol

Defined in `core/application/services/prompt/prompt_strategy.py`. `SecBenchPromptStrategy` in `prompt_strategy.py` implements it.

```
cat core/application/services/prompt/prompt_strategy.py
```

| Method | Role | Template chain |
|---|---|---|
| `extend_assessment_prompt` | Assessor | `cve.j2` + `assess.j2` |
| `extend_boss_prompt` | Boss | `cve.j2` + `boss.j2` |
| `extend_manager_prompt` | Manager | `cve.j2` + `manager.j2` + `manager/{phase}.j2` (direct boss children only) |
| `extend_worker_prompt` | Worker | Common `cve.j2` + `worker.j2`, then either `worker/role_contract.j2` + one allowlisted `worker/roles/{phase}/{role}.j2`, or a phase-level `worker/{phase}.j2` for fused/whole-phase work |

All four methods return `None` when `domain_context` is not a `CVEInstance`, causing core to use default prompts.

## Bootstrap Wiring

`bootstrap/composition.py` is the **sole** file that imports from `plugins/security/`.

```
cat bootstrap/composition.py
```

The wiring path in `_build_security_components(settings)`:

1. Checks `settings.security.enabled`. Returns empty `DomainComponents()` if disabled.
2. Creates `DockerSecBenchRuntime()` directly.
3. Creates `SecurityDomainPlugin(...)` with tools, shared-context ordering,
   `adaptive_execution`, and `treatment_version` from settings.
4. Calls `plugin.set_container_runtime(runtime)` to inject the runtime.
5. Calls `plugin.get_prompt_strategy()` to get the paired `SecBenchPromptStrategy`.
6. Exposes prompt, decomposition, and procedure seams through generic ports; bootstrap
   remains the only cross-boundary importer.

Activation requires one of:
- `--domain security` CLI flag
- `--cve-file <path>` (auto-activates the security domain)

## Worker Classification

The role taxonomy uses two independent axes. Do not put `mechanical`, `coding`, and
`reasoning` in one enum: the first describes **how work executes**, while the second
describes **what kind of work it is**.

| Axis | Values | Meaning |
|---|---|---|
| Execution mechanism | `LLM`, `host procedure` | Whether a model conversation runs or trusted Arise Python executes a fixed procedure |
| Task character | `coding/execution`, `reasoning`, `hybrid`, `synthesis` | The cognitive shape of the task; `thinking` is not a separate category from reasoning |

The implemented pre-experiment model policy is role-based. Every role classified as
reasoning, hybrid, or synthesis uses `gpt-5.3-codex`; pure coding/execution roles use
`gpt-5.4-mini`; host procedures use no model. Generic `worker.model_overrides` routes
the LLM-backed roles and persists the selected model in worker cost events.

| Phase | Catalog role | Current mechanism | Task character | Target model |
|---|---|---|---|---|
| Builder | `Build-Setup` | LLM | coding/execution | `gpt-5.4-mini` |
| Builder | `Build-Executor` | LLM | coding/execution | `gpt-5.4-mini` |
| Builder | `Build-Verifier` | host procedure | mechanical verification | none |
| Exploiter | `PoC-Researcher` | LLM | reasoning | `gpt-5.3-codex` |
| Exploiter | `Data-Flow-Analyst` | LLM | reasoning | `gpt-5.3-codex` |
| Exploiter | `PoC-Tester` | LLM | reasoning/execution | `gpt-5.3-codex` |
| Exploiter | `Forward-Instrumentator` | LLM | hybrid reasoning/coding | `gpt-5.3-codex` |
| Exploiter | `Repro-Creator` | LLM | coding/execution | `gpt-5.4-mini` |
| Exploiter | `Exploit-Validator` | host procedure | mechanical verification | none |
| Fixer | `Root-Cause-Analyst` | LLM | reasoning | `gpt-5.3-codex` |
| Fixer | `Candidate-Reviewer` | LLM | reasoning | `gpt-5.3-codex` |
| Fixer | `Regression-Tester` | LLM | reasoning/execution | `gpt-5.3-codex` |
| Fixer | `Patch-Applier` | host procedure | mechanical transformation | none |
| Fixer | `Patch-Validator` | host procedure | mechanical verification | none |
| Fixer | `Fix-Aggregator` (optional) | LLM | synthesis | `gpt-5.3-codex` |
| Reporter | `Reporter` | LLM | synthesis/reasoning | `gpt-5.3-codex` |

In the role-fused B3 treatment, a phase worker receives one model. Apply the same
functional policy: Builder uses `gpt-5.4-mini`; Exploiter, Fixer, and Reporter use
`gpt-5.3-codex` because each fused task contains reasoning. Record this as part of the
treatment: B3 role fusion changes model-token allocation as well as topology.

## Adaptive and Role-Fused Policies

`SecBenchDecompositionValidator.fixed_decomposition()` owns the current deterministic
adaptive policy when `adaptive_execution` is enabled:

```text
BOSS
  -> Builder -> Build-Setup, Build-Executor, Build-Verifier
  -> Exploiter -> Repro-Creator, Exploit-Validator
  -> Fixer -> Root-Cause-Analyst, Patch-Applier, Patch-Validator
  -> Reporter -> Reporter
```

That compact route is nine leaf workers, four of which are host procedures. It is
selected by trusted host code, not by an LLM manager. A failed phase re-decomposition
adds only specialists whose trigger matches the failure; an unrecognized failure uses
the phase's full escalated set. Hard dependencies are closed transitively, so an adaptive
route cannot select a consumer without its producer. `b4-adaptive-rolefused-v1` creates
four fused LLM phase workers plus the same four host procedures, with no role children.
The assess and worker prompts describe these two treatment shapes explicitly.

## Deterministic Procedure Tier (procedural dispatch)

When `settings.orchestration.procedural_dispatch` is on, `SecBenchProcedureExecutor` (`procedures.py`) is bound as the run's `ProcedureExecutorPort` via `SecurityDomainPlugin.get_procedure_executor()`. It runs four fixed roles host-side with zero LLM turns, so validation and literal patch application become host-computed and event-sourced instead of agent-authored. Default off => `NullProcedureExecutor` => the agentic path.

**Static registry (leading `[Role]` bracket -> `procedure_ref`):**

| `[Role]` bracket | `procedure_ref` | `secb` commands (host-driven, synchronous) |
|---|---|---|
| `[Build-Verifier]` | `secb_build_validation` | `secb build`, then resolve and inspect the declared binary |
| `[Exploit-Validator]` | `secb_exploit_validation` | `secb repro` x3 |
| `[Patch-Applier]` | `secb_patch_apply` | validate the approved `patch_plan.json`; render `model_patch.diff` literally |
| `[Patch-Validator]` | `secb_patch_validation` | `secb patch` -> `secb build` -> `secb repro` x3 (post-patch, expect no crash) |

- `match()` parses the task's leading `[Role]` bracket (via `role_from_task`) and looks it up in `_REGISTRY`; `resolve()` checks membership in `_PROCEDURE_REFS`.
- `execute()` resolves the run's live container through the injected `session_resolver(root_id)` (the orchestrator injects `root_id` into `procedure_params` from `hierarchy_limits`), downcasts `domain_context` to `CVEInstance`, and drives the container `ProcedureSession` synchronously. Task-level failure (FAIL verdict, preflight miss, timeout) returns `success=False` + a bounded digest and never raises; only a missing session / missing `root_id` / unknown ref raises `ProcedureInfrastructureError`.
- **Verdict by crash signature.** Each `secb repro` output is reduced to a `CrashSignature` (sanitizer class / access type / top frame) by `crash_signature.py`; `_consensus` picks the majority signature + determinism count; `signatures_match` compares it against the CVE oracle (`compute_crash_signature(cve.sanitizer_report)`). Exploit PASS iff the observed signature matches the oracle across 3/3 deterministic runs. `crash_signature.py` is drift-tested against `experiments/shared/evaluation/criteria.py::_crash_signature` (same 3-tuple shape).
- **Same on-disk contract.** Writes the identical artifacts the agentic path uses -- `exploit_validation_results.txt` / `patch_validation_results.txt`, `repro_run_*.log`, exit sentinels -- reusing `ARTIFACT_PATHS` / `PHASE_COMMANDS` from `deliverables.py`. Additionally each command becomes a `ProcedureEvidence` (`argv`, `exit_code`, `output_sha256`, `excerpt`) on the `ProcedureExecutionFinished` event -- agent-unforgeable, unlike the agent-written verdict files.

### Mechanical criteria

| Gate | PASS | FAIL |
|---|---|---|
| Build | `secb build` exits 0 before timeout and the declared binary pointer resolves | timeout, nonzero exit, missing/invalid binary pointer |
| Exploit preflight | `repro.sh`, PoC pointer, and binary pointer resolve; the reproducer is not the seeded stub | any missing, unresolved, or stub artifact |
| Exploit replay | three independent `secb repro` runs produce the frozen oracle crash signature | fewer than 3/3 matches, timeout, signal/exit inconsistency, sanitizer/access/top-frame mismatch |
| Patch plan | JSON schema, allowed operations, paths, context anchors, and frozen scope all validate | malformed plan, path escape/protected path, missing/ambiguous anchor, unsupported operation |
| Patch apply | host renders the approved plan into a unified diff without discretionary edits | any mismatch between approved plan and rendered patch |
| Patch validation | patch applies, build succeeds, and all three post-patch replays are crash-free | patch/build timeout or nonzero exit, signal, assertion, sanitizer, or any replay crash |
| Safety/provenance | protected artifacts unchanged; all six containers are distinct; pre/post replay trios target the requested CVE and same frozen base | target/base identity drift, protected-path modification, reused container, missing host evidence |

Mechanical gates never accept an agent-authored `VERDICT: PASS`. A failed procedure
records a bounded `failure_digest` and gets exactly one agentic retry; the retry does
not erase the failed host evidence.

## CVEInstance Model

`CVEInstance` (`plugins/security/cve_instance.py`) is a frozen Pydantic model carrying all SEC-bench metadata through the agent tree as the opaque `domain_context` slot in `HierarchyLimits`.

### Stored Fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `instance_id` | `str` | required | Format: `{project_name}.{cve_id}` |
| `repo` | `str` | required | GitHub repo path (e.g., `knik0/faad2`) |
| `project_name` | `str` | required | Project name extracted from repo |
| `lang` | `str` | required | Programming language |
| `work_dir` | `str` | required | Container working directory (e.g., `/src`) |
| `sanitizer` | `str` | required | Sanitizer type: `address`, `memory`, `undefined` |
| `bug_description` | `str` | required | CVE description text |
| `base_commit` | `str` | required | 40-char git commit hash |
| `build_sh` | `str` | `""` | Build script content |
| `secb_sh` | `str` | `""` | SEC-bench test script content |
| `dockerfile` | `str` | `""` | Dockerfile content |
| `patch` | `str` | `""` | Gold patch (ground truth fix) |
| `exit_code` | `int` | `0` | Expected exit code |
| `sanitizer_report` | `str` | `""` | Full sanitizer output |
| `bug_report` | `str` | `""` | Original report retained for provenance/evaluation/curation; stripped from solving prompts because it may disclose the fix |
| `candidate_fixes` | `str` | `""` | Candidate fix suggestions |
| `docker_image_override` | `str` | `""` | Optional explicit SEC-bench image name |

### Computed Fields

| Field | Type | Derivation |
|---|---|---|
| `cve_id` | `str` | Second part of `instance_id` split on `.` |
| `docker_image` | `str` | `docker_image_override` when set; otherwise `hwiwonlee/secb.eval.x86_64.{project_name}.{cve_id}:patch` |
| `expected_sanitizer_error` | `str` | Parsed from `sanitizer_report` (ASan error types, MSan, UBSan patterns) |
| `has_dockerfile` | `bool` | `bool(self.dockerfile.strip())` |
| `has_build_script` | `bool` | `bool(self.build_sh.strip())` |
| `has_cve_data` | `bool` | `bool(self.bug_description.strip())` |

### Two Loading Paths

**JSON file (preferred):** `CVEInstance.from_json_file(path)` reads and validates a JSON file. Triggered by `--cve-file` CLI flag.

**Task text inference:** `CVEInstanceInferenceService.infer_instance(task_text)` uses regex to extract fields from free-form text. Requires at minimum a GitHub repo URL and a 40-char commit hash. Raises `CVEInferenceError` listing missing fields when `fail_fast=True` (default). Returns `None` on missing required fields when `fail_fast=False`.

## Domain Context Flow (Opaque Slot Pattern)

The `CVEInstance` object travels as `object | None` through `HierarchyLimits.domain_context`. Core never downcasts it. Only plugin code calls `_as_cve_instance(domain_context)` to recover the typed `CVEInstance`.

Flow:

1. `SecurityDomainPlugin.infer_context()` creates a `CVEInstance` from JSON file or task text inference.
2. Core stores it as `HierarchyLimits.domain_context: object | None` (opaque to core).
3. On each prompt build, core passes `domain_context` to `PromptStrategy.extend_*_prompt()` via `PromptContext`.
4. `SecBenchPromptStrategy` calls `_as_cve_instance(context.domain_context)` to downcast. Returns `None` (skip) if it's not a `CVEInstance`.
5. On worker execution, core passes `domain_context` to `prepare_run` / `prepare_worker_execution` / `cleanup_worker_execution`.
6. `SecurityDomainPlugin` downcasts with `_as_cve_instance()` in each method, returning `None` early if not applicable.

The `_as_cve_instance` helper is defined independently in both `plugin.py` and `prompt_strategy.py` (module-level function, not shared).

## SEC-bench Phases

Four phases correspond to branches of the BOSS agent's decomposition tree. Each has its own worker template; three have manager templates.

| Phase | Purpose | Keywords for detection | Templates |
|---|---|---|---|
| `builder` | Compile the project in the container | `builder`, `environment`, `setup`, `docker pull` | `manager/builder.j2`, phase-level `worker/builder.j2`, per-role `worker/roles/builder/*.j2` |
| `exploiter` | Trigger the vulnerability with a PoC | `exploiter`, `poc`, `exploit`, `proof of concept` | `manager/exploiter.j2`, phase-level `worker/exploiter.j2`, per-role `worker/roles/exploiter/*.j2` |
| `fixer` | Patch the vulnerability | `fixer`, `patch`, `fix` | `manager/fixer.j2`, phase-level `worker/fixer.j2`, per-role `worker/roles/fixer/*.j2` |
| `reporter` | Write a security report | `reporter`, `report`, `security report` | phase-level `worker/reporter.j2`, per-role `worker/roles/reporter/reporter.j2` (no manager template) |

### Phase Detection

Two functions in `prompt_strategy.py` handle detection:

**`detect_benchmark_branch(briefing)`** -- Scans `briefing.ancestry` in **reverse** (most specific ancestor first) for phase keywords. Used as a fallback when task-text detection fails. Also used by `enrich_prompt` in `plugin.py` for security tool injection.

**`_detect_branch_from_task(task_description)`** -- Checks for explicit bracket prefixes first (`[Builder]`, `[Exploiter]`, `[Fixer]`, `[Reporter]`) as authoritative matches, then falls back to loose keyword matching. This is the primary detection method.

Worker and manager prompt extension methods try `_detect_branch_from_task` first, then fall back to `detect_benchmark_branch`.

Worker role selection is stricter than phase detection. `role_from_task()` resolves the
leading bracket against the catalog, then `_ROLE_TEMPLATE_BY_NAME` maps the canonical
role to one explicit template path. Never derive a Jinja path from task text. Canonical
phase brackets select `_PHASE_TEMPLATE_BY_BRANCH`; any other unknown bracket raises
`ValueError` rather than receiving a whole-phase contract.

Manager phase-specific templates are only injected for direct boss children (`len(briefing.ancestry) == 1`). This prevents sub-managers from receiving redundant phase-specific instructions. Worker phase templates are injected regardless of depth.

## Container Runtime

### Protocol

`SecurityContainerRuntime` in `container_runtime.py` defines the two-step lifecycle:

```python
async def prepare_workspace(cve, run_output_path, image, root_id) -> SecBenchWorkspace
async def start_session(cve, workspace, agent_id) -> SecBenchContainerSession
```

There is no stop step: the run's shared container is reaped at process exit by the
PID-labeled cleanup registry.

### Concrete Implementation

`DockerSecBenchRuntime` in `docker_runtime.py` implements the protocol. It uses Docker CLI commands via `asyncio.create_subprocess_exec`. Supports Docker-out-of-Docker (DooD) via `HOST_PROJECT_ROOT` env var for path remapping.

Key operations in `start_session`:
- Runs container with `tail -f /dev/null` (keeps it alive).
- Mounts host source and testcase directories as bind volumes.
- Installs `secb_sh` script if present.
- Marks `work_dir` as git safe directory.
- Makes `build.sh` executable.
- Writes a `secb-exec` helper script to the workspace.

### Value Objects

**`SecBenchWorkspace`** (frozen dataclass) -- Host-side workspace:

| Field | Type | Description |
|---|---|---|
| `root_id` | `UUID` | Run identifier |
| `image` | `str` | Resolved Docker image name |
| `host_root` | `Path` | Root path on host filesystem |
| `host_source_dir` | `Path` | Host path mirroring `/src` |
| `host_testcase_dir` | `Path` | Host path for `/testcase` |
| `host_work_dir` | `Path` | Host path mirroring `cve.work_dir` |
| `container_source_dir` | `str` | `/src` |
| `container_testcase_dir` | `str` | `/testcase` |
| `container_working_directory` | `str` | Working directory inside container |
| `helper_script` | `Path` | Path to generated `secb-exec` script |

**`SecBenchContainerSession`** (frozen dataclass) -- Active container bound to a workspace. Fields: `workspace`, `container_id`, `container_name`, `image`. Provides `to_task_context() -> dict[str, str]` that serializes all paths and IDs for worker adapters.

### Lifecycle in the Plugin

1. **`prepare_run`** -- Called once per run. Resolves image via `resolve_secbench_image`, calls `container_runtime.prepare_workspace`. Caches in `_workspaces[root_id]`. Idempotent (returns cached workspace on repeat calls).
2. **`prepare_worker_execution`** -- Called before each worker executes. Reuses the run's existing session when one is cached in `_sessions[root_id]` (one shared container per run); otherwise calls `container_runtime.start_session` and caches it. A per-root lock serializes the check-and-start so concurrent workers cannot race to a second container.
3. **`cleanup_worker_execution`** -- Intentional no-op: the run's one shared container stays alive across ALL workers (stopping per-worker would destroy container-local state the next worker needs) and is reaped at process exit by the PID-labeled cleanup.

## Image Resolver

`resolve_secbench_image()` in `image_resolver.py` remaps base SEC-bench images to tool-enriched variants when `security_tools_enabled` is `True`.

```
Input:  hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838:patch
Output: secb-tools:gpac.cve-2023-2838-patch
```

Extracts parts after `secb.eval.x86_64.` (index 3+) from the dot-separated image name. When `security_tools_enabled` is `False`, returns the base image unchanged. The `image_prefix` parameter defaults to `"secb-tools"`.

## Security Tool Registry

`SECURITY_TOOL_REGISTRY` in `security_tool.py` maps tool name strings to `SecurityTool` frozen Pydantic models.

| Tool | Key | Applicable Phases | Purpose |
|---|---|---|---|
| Valgrind | `"valgrind"` | `exploiter`, `fixer` | Dynamic memory analysis (leaks, buffer overflows, use-after-free) |
| KLEE | `"klee"` | `exploiter` | Symbolic execution for automatic test input generation |

`get_tools_for_phase(phase, enabled_tools) -> list[SecurityTool]` returns tools that are both enabled in config and applicable to the given phase.

Each `SecurityTool` has: `name` (display), `description`, `commands` (example CLI invocations), `applicable_phases`.

### Tool Injection into Prompts

The plugin's `enrich_prompt` method in `plugin.py`:
1. Checks that `domain_context` is a `CVEInstance`, tools are enabled, and `chain_factory` is provided.
2. Detects phase via `detect_benchmark_branch(briefing)`.
3. Calls `get_tools_for_phase(phase, self._enabled_tools)`.
4. Renders `domains/secbench/tools.j2` with the tool list (serialized via `model_dump()`).
5. Appends the rendered text to the existing prompt string.

### Adding a New Tool

1. Add a `SecurityTool` entry to `SECURITY_TOOL_REGISTRY` in `security_tool.py`.
2. Set `applicable_phases` to the relevant phases.
3. Add the tool name to `settings.security.tools` default list.
4. The tool is automatically rendered via `tools.j2` and injected into prompts.

## Prompt Templates

All SEC-bench Jinja2 templates live under `prompts/domains/secbench/`:

```
prompts/domains/secbench/
    cve.j2                    -- CVE metadata display (shared across roles)
    assess.j2                 -- Assessment-specific CVE analysis
    boss.j2                   -- BOSS decomposition guidance
    manager.j2                -- Base manager instructions
    worker.j2                 -- Base worker instructions
    tools.j2                  -- Security tool docs (appended via enrich_prompt)
    manager/
        builder.j2            -- Builder phase manager instructions
        exploiter.j2          -- Exploiter phase manager instructions
        fixer.j2              -- Fixer phase manager instructions
    worker/
        role_contract.j2      -- Shared ownership/dependency boundary for catalog roles
        builder.j2            -- Builder fused/whole-phase instructions
        exploiter.j2          -- Exploiter fused/whole-phase instructions
        fixer.j2              -- Fixer fused/whole-phase instructions
        reporter.j2           -- Reporter fused/whole-phase instructions
        roles/
            builder/
                build_setup.j2
                build_executor.j2
                build_verifier.j2
            exploiter/
                poc_researcher.j2
                data_flow_analyst.j2
                poc_tester.j2
                forward_instrumentator.j2
                repro_creator.j2
                exploit_validator.j2
            fixer/
                root_cause_analyst.j2
                candidate_reviewer.j2
                regression_tester.j2
                patch_applier.j2
                patch_validator.j2
                fix_aggregator.j2
            reporter/
                reporter.j2
```

The 16 files under `worker/roles/` are the only catalog-role bodies. A B4 role leaf
renders the common runtime/CVE/worker prefix, `role_contract.j2`, and exactly one of
those files. It does not render `worker/{builder,exploiter,fixer,reporter}.j2` or a full
phase partial. The four phase-level worker templates remain separate for role-fused B3
and whole-phase fallback. This separation prevents a role from inheriting sibling
deliverables while preserving an intentional composite contract for fused workers.

The solver-facing boundary is `CVEInstance.to_template_context()`, not the raw model.
It removes `bug_report`, `patch`, `candidate_fixes`, and `secb_sh` before
`_with_cve_display()` selects fields for `cve.j2`. The effective CVE display is
`instance_id`, `cve_id`, `repo`, `project_name`, `lang`, `sanitizer`, `base_commit`,
`work_dir`, `bug_description`, and `sanitizer_report` (empty values are omitted).

This follows the official SEC-bench evaluation convention: solving agents receive the
concise vulnerability description and sanitizer crash report, while the unrestricted
original report and gold patch stay host-side. The distinction is visible in the
[paper's patch-task formulation](https://arxiv.org/pdf/2506.11791) and the
[official patch prompt](https://github.com/SEC-bench/smolagents/blob/a945dba9d6f2594cd94eb00d77f6b41a92fea88b/src/smolagents/prompts/patch.j2#L1-L10).
Do not remove `bug_report` from `CVEInstance`: host-side curation and evaluation may
still need the original bytes. Do not pass it through a prompt-safe dictionary or render
it for workers. An upstream report may contain the official fix commit or exact changed
code.

The authoritative mechanical crash oracle is `sanitizer_report`. The current evaluator
still retains `bug_report`, and two semantic paths expose it to judges:
`experiments/shared/evaluation/criteria/judge_prompts.py::build_cve_reproduced_prompt()`
and `experiments/shared/scripts/evaluate_run.py::_semantic_evidence()`. That is a known
leakage risk when the field contains gold fix content; it is not part of the solver prompt
contract and must not be described as safe merely because the consumer is a judge.

### Tag Provenance

`get_tag_mappings()` registers 15 tags as `SectionProvenance.SYSTEM`: `cve_context`, `security_context`, `issue_description`, `repository_info`, `cve_instance`, `bug_report`, `sanitizer`, `work_dir`, `base_commit_hash`, `commit_hash`, `commit_hash1`, `commit_hash2`, `commit_url`, `changed_file_path`, `security_tools`. This is provenance classification for parsing; the presence of a `bug_report` tag mapping does not authorize rendering `CVEInstance.bug_report` to a solver.

`get_provenance_patterns()` returns the regex `cve|security|workspace|instance|commit|file|bug|sanitizer|exploit` to catch remaining security-related tags.

## CVE Inference Details

`CVEInstanceInferenceService` in `cve_inference.py` extracts fields using these regex patterns:

| Field | Pattern | Notes |
|---|---|---|
| `cve_id` | `CVE-\d{4}-\d{4,}` | Case-insensitive |
| `base_commit` | `\b[a-f0-9]{40}\b` | Exact 40-char hex |
| `repo` | `github\.com[:/]owner/repo` | Strips `.git` suffix |
| `sanitizer` | Keyword match (ASan/MSan/UBSan patterns) | Defaults to `address` |
| `lang` | File extension or language name patterns | C, C++, Python, Rust, Go, Java, JavaScript |

`ExtractionResult.has_required_fields()` requires both `repo` and a 40-char `base_commit`. The inferred `CVEInstance` gets default `work_dir="/src"` and empty `bug_description`. The `instance_id` is synthesized as `{project_name}.{cve_id}`.

## What Belongs Where

### In `plugins/security/`

- CVE metadata models and loading logic
- SEC-bench phase detection
- Security-specific prompt strategy and template rendering
- Container runtime protocol, value objects, and Docker implementation
- Security tool definitions and registry
- Benchmark result models

### NOT in `plugins/security/`

- Orchestration logic (task scheduling, DAG execution, agent lifecycle) -- `core/`
- Topology management (depth limits, child counts, agent caps) -- `core/`
- Generic prompt building and template chain mechanics -- `core/application/services/prompt/`
- CLI argument parsing or command dispatch -- `bootstrap/` or `presentation/`
- Configuration loading (Pydantic Settings, YAML parsing) -- `config/`

## Tests

Tests live in `plugins/security/tests/` using Given-When-Then style:

| Test file | What it covers |
|---|---|
| `test_container_lifecycle.py` | Workspace preparation, session start/stop, cleanup error handling |
| `test_cve_inference.py` | Regex extraction, missing field errors, edge cases |
| `test_docker_runtime.py` | DockerSecBenchRuntime subprocess behavior |
| `test_image_resolver.py` | Image name remapping, tools-disabled passthrough |
| `test_prompt_building.py` | Phase detection, template rendering per role, `None` returns |
| `test_role_prompt_catalog.py` | All 16 catalog roles map to explicit templates; role ownership, unknown-label rejection, and role/phase isolation |

Fixtures are in `plugins/security/tests/fixtures/`.
