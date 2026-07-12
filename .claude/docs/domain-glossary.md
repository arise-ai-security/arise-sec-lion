<!-- Read this when: encountering unfamiliar domain terms or concepts -->
Domain glossary for the arise-sec-lion multi-agent orchestration platform.

---

## 1. Agent Hierarchy (Roles)

Defined in `core/domain/values/enums.py` as `AgentRole(str, Enum)`.

| Role | Value | Definition |
|------|-------|------------|
| **BOSS** | `"boss"` | Root agent of an execution hierarchy. Created once per run. Decomposes the top-level task into subtasks and spawns children. Only BOSS emits `RunStarted`/`RunCompleted` events. |
| **PENDING** | `"pending"` | Temporary role assigned to every newly spawned child. The `assess_task` operation evaluates it and promotes it to WORKER or MANAGER (or marks it INFEASIBLE). |
| **MANAGER** | `"manager"` | Non-leaf agent that decomposes its task into subtasks and spawns children. Transitions to WAITING after spawning, then aggregates child results. |
| **WORKER** | `"worker"` | Leaf agent that executes work via an external tool (Claude Code, OpenHands). Produces a result directly, verified by the `VerificationPipeline`. |

Role transitions: PENDING -> WORKER or PENDING -> MANAGER (decided by `assess_task`). BOSS acts like MANAGER but is created at run start, not by assessment. A BOSS cannot be spawned as a child (enforced by `ChildSpawned` handler in `AgentSession`).

---

## 2. Agent Statuses

Defined in `core/domain/values/enums.py` as `AgentStatus(str, Enum)`. Distinct from roles (above).

| Status | Value | Meaning |
|--------|-------|---------|
| **PENDING** | `"pending"` | Initial status after `AgentCreated`. Awaiting task assignment. |
| **ANALYZING** | `"analyzing"` | Task assigned (`TaskAssigned`). Assessment or decomposition in progress. Also re-entered on retry or re-decomposition. |
| **IN_PROGRESS** | `"in_progress"` | WORKER has started tool execution (`CodeGenerationStarted`). |
| **WAITING** | `"waiting"` | BOSS/MANAGER spawned children and is waiting for all to complete. |
| **COMPLETED** | `"completed"` | Work finished successfully (`WorkCompleted`). Terminal state. |
| **FAILED** | `"failed"` | Work failed (`WorkFailed`), verification failed, or declared infeasible. Terminal state unless retry scheduled. |
| **BLOCKED** | `"blocked"` | Agent cannot proceed (dependency not met, resource unavailable). |

Terminal check: `AgentSession.is_terminal()` returns True for COMPLETED or FAILED.

---

## 3. Orchestrator Operations

Three direct methods on `AgentOrchestrator` (`core/application/agent_orchestrator.py`). No pipeline, strategy, or chain-of-responsibility abstraction wraps them -- this is intentional.

| Operation | Method | Input Role | What It Does |
|-----------|--------|------------|--------------|
| **assess_task** | `assess_task()` | PENDING | Single LLM call that decides: execute directly (promote to WORKER) or decompose (promote to MANAGER and spawn children). Can also return INFEASIBLE. This is the fork point of the agent tree. |
| **evaluate_task** | `evaluate_task()` | BOSS / MANAGER | LLM decomposes the task into a `Subtask` list, spawns child agents for each. Parent transitions to WAITING. |
| **execute_task** | `execute_task()` | WORKER | Dispatches work to a worker tool (Claude Code, OpenHands). Streams `DomainEvent`s back. Result verified by `VerificationPipeline`. |

Role dispatch logic lives in `core/application/services/lifecycle/role_dispatch.py`.

---

## 4. Context Passing (Inter-Agent Messages)

Defined in `core/domain/values/node_message.py`. Three variants of `NodeMessage` (discriminated union on `direction` field):

### Briefing (direction: `"down"`)
Parent-to-child context passed at spawn time. Contains `parent_task`, `parent_role`, `ancestry` (chain of `Ancestor` entries from root to parent), `decisions` made so far, and optional `subtask_justification` (parent's reasoning for this specific subtask). Built by `AgentSession.build_briefing_for_child()`.

### Report (direction: `"up"`)
Child-to-parent result on completion. Contains `task`, `result`, `artifacts` (file keys produced), `decisions` recorded, and `execution_summary` (e.g. cost). Built by `AgentSession.build_report()`.

### Handoff (direction: `"lateral"`)
Sibling-to-sibling coordination snapshot. Contains `parent_task`, `current_sibling_index`, `siblings` (list of `PeerStatus` snapshots), and `shared_decisions`. Built by `SiblingViewPort.build_view()` (`core/ports/runtime_ports.py`). Provides computed fields: `total_siblings`, `completed_count`, `has_downstream_siblings`.

Related types (all in `core/domain/values/node_message.py`): `Ancestor` (lightweight entry in lineage chain), `PeerStatus` (sibling status snapshot), `SharedDecision` (cross-sibling decision).

---

## 5. Event Sourcing

### DomainEvent
Base class for all immutable domain events (`core/domain/events/events.py`). Frozen Pydantic model with `event_id`, `aggregate_id`, `sequence_number`, `occurred_at`, `metadata`. All dict/list fields are deep-copied on construction via a `model_validator`. 42 concrete event types, all registered in `EVENT_TYPE_REGISTRY` (`infrastructure/adapters/postgres_event_store.py`).

### AgentSession (primary aggregate)
The primary aggregate (`core/domain/aggregates/agent_session.py`), mutated by 39 of the 42 event types; `SharedStore` is a second event-sourced aggregate (see its entry below). All agent state is derived by replaying events via `singledispatchmethod` handlers. Factory: `AgentSession.create()` or `AgentSession.load_from_history()`. Uncommitted events accessed via `.events`, cleared by `mark_changes_as_committed()`. No mutable state tables exist.

### OCC (Optimistic Concurrency Control)
Concurrency strategy enforced by `EventStoreWritePort.append()` / `append_batch()` (`core/ports/event_store_port.py`). Each append specifies `expected_version`; if the actual version differs (another process wrote first), `ConcurrencyError` is raised (`core/domain/exceptions.py`). Unique constraint on `(aggregate_id, sequence_number)`.

### append_batch
Atomic multi-event write on `EventStoreWritePort` (`core/ports/event_store_port.py`). Persists all events in a single transaction. Significantly faster than individual appends for workers that produce many events (thoughts, tool uses).

### Projection
Read-side transformation of domain events into structured output (`core/query/projections/base/projection.py`). Abstract base class with `project(events) -> Any`. Concrete implementations in `core/query/projections/impl/`: `SummaryProjection`, `CostProjection`, `AgentListProjection`, `IncrementalSummaryProjection`.

---

## 6. Architecture Concepts

### Port
A Python `Protocol` class in `core/ports/` defining an interface that the domain layer depends on. Core never imports implementations. Key ports: `LLMPort`, `WorkerToolPort`, `EventStorePort` (composite of Connect/Write/Read), `SharedContextPort`, `CostCalculatorPort`, `ReconToolPort`, `SiblingViewPort`, `SystemLimitsPort`, `Toolset`. See `core/ports/runtime_ports.py` and `core/ports/event_store_port.py`.

### Adapter
A concrete implementation of a Port protocol, located in `infrastructure/adapters/`. Injected at bootstrap. Core code references only the Protocol; the adapter provides the concrete behavior.

### DomainPlugin
Bridge protocol for optional domain-specific behavior (`core/ports/domain_plugin_port.py`). Methods: `infer_context()`, `enrich_prompt()`, `get_run_metadata()`, `get_tag_mappings()`, `get_provenance_patterns()`, `prepare_run()`, `prepare_worker_execution()`, `cleanup_worker_execution()`, `get_prompt_strategy()`. The security plugin (`SecurityDomainPlugin`) is the only current implementation.

### PromptStrategy
Protocol for extending the default prompt chain (`core/application/services/prompt/prompt_strategy.py`). Four extension points: `extend_assessment_prompt()`, `extend_boss_prompt()`, `extend_manager_prompt()`, `extend_worker_prompt()`. Each receives a `TemplateChain` and `PromptContext`, returns an extended chain or None (use default). Related: `PromptContext` (immutable context dataclass), `SubtaskScope` (structured scoping from parent decomposition with `target_paths`, `symbols`, `search_hints`).

### TemplateChain
Fluent builder for chaining Jinja2 template renders (`core/application/services/prompt/prompt_builder.py`). Methods: `.render()`, `.render_if()`, `.render_optional()`, `.text()`, `.text_if()`, `.build()`. Used by `PromptBuilder` to compose the 4-tier prompt architecture.

### PromptBuilder
Domain service that builds hierarchical prompts from Jinja2 templates (`core/application/services/prompt/prompt_builder.py`). Implements the 4-tier architecture: Tier 1 `system.j2` (global constraints), Tier 2 `roles/*.j2` (per-role persona), Tier 3 `operations/*.j2` (per-operation format), Tier 4 `domains/*.j2` (optional domain context). Uses `PromptStrategy` for domain extensions.

### domain_context (opaque slot)
The `HierarchyLimits.domain_context: object | None` field (`core/domain/values/limits.py`) that carries plugin-specific data through the agent tree. Only the plugin downcasts it (e.g., to `CVEInstance`). Core remains fully domain-ignorant. Set via `HierarchyLimits.with_domain_context()`, checked via `has_domain_context()`.

### Procedural dispatch
A flag-gated deterministic execution tier (`settings.orchestration.procedural_dispatch`, default off) that runs registry-matched worker tasks host-side with **zero LLM turns** -- no prompt, no worker session, no cost events. Core sees only `ProcedureExecutorPort` (`core/ports/procedure_ports.py`); a domain plugin supplies the registry and execution. `execute_task` runs a dispatch ladder (`_resolve_procedure_ref`): explicit `agentic` bypasses; explicit `procedural` with a resolvable ref runs it; an unknown ref downgrades to `auto` with a warning (fail open); `auto` consults `match()`. Success -> `WorkCompleted` through the normal verification pipeline; failure -> a `procedure_failure` digest + `WorkFailed`, then exactly one guaranteed *agentic* retry (a procedure never dispatches twice). Default off binds `NullProcedureExecutor`, so behavior is byte-identical to pre-feature.

### Execution mode
The `Subtask.execution_mode` marking (`"auto"` | `"agentic"` | `"procedural"`, default `"auto"`) a parent attaches during decomposition, carried to the child via `AgentCreated`. Drives the procedural dispatch ladder; paired with `procedure_ref` and `procedure_params`.

### ProcedureEvidence
Host-captured, agent-unforgeable record of one procedure command (`argv`, `exit_code`, `output_sha256`, `excerpt`) in `core/domain/values/procedure.py`. Serialized per command onto `ProcedureExecutionFinished`, making determinism and validator verdicts verifiable from the event store rather than from agent-written verdict files (the SYSTEM_REFERENCE §V.7 by-construction fix).

---

## 7. Scheduling and Shared State

### TaskScheduler (DAG scheduling, Kahn's algorithm)
Schedules sibling agents in topological order based on `depends_on` edges (`core/domain/services/task_scheduler.py`). Each task is identified by `sibling_index`. `get_ready()` returns indices whose dependencies are all satisfied (completed set). When no edges exist, all agents are immediately ready (parallel execution). Uses Kahn's algorithm: tasks with zero unsatisfied dependencies are ready.

### Subtask
Frozen Pydantic value object for task decomposition (`core/domain/values/subtask.py`). Fields: `description`, `config`, `depends_on` (sibling indices), `dependency_type`, `estimated_complexity`, `success_criteria`, `failure_indicators`, `task_type`, `justification`, `target_paths`, `symbols`, `search_hints`.

### SharedStore
Event-sourced facade composing `ArtifactStore` and `DecisionLog` (`core/domain/shared_context.py`). One instance per execution hierarchy, keyed by `root_id`. Aggregate ID derived deterministically from `root_id` via UUID5 to avoid collision with `AgentSession`. Factory: `SharedStore.create()` or `SharedStore.load_from_history()`.

### ArtifactStore
Projection within `SharedStore` for shared outputs between agents (`core/domain/shared_context.py`). Stores `Artifact` value objects (key, content_type, content or content_hash, stored_by, metadata). Events: `ArtifactStored`.

### DecisionLog
Projection within `SharedStore` for architectural/design decisions (`core/domain/shared_context.py`). Stores `Decision` value objects (key, value, rationale, decided_by). Events: `DecisionRecorded`. Decisions should be consistent across the hierarchy.

### ContextCondenser
Manages context budget for the recon tool-calling loop (`core/application/services/orchestration/context_condenser.py`). Three layers: (1) per-result hard cap -- truncate each tool output before it enters history, (2) sliding-window summarization -- compress older tool exchanges via LLM into a digest, (3) token budget gate -- estimate total tokens and force-condense when near limit. Prevents context window overflow during PENDING/MANAGER reconnaissance.

---

## 8. Resilience

### RetryPolicy
Decides whether failed workers should be retried and persists retry state (`core/application/services/orchestration/retry_policy.py`). Only retries WORKER agents. This is the *generic* second retry path (model escalation, budget = length of the model-escalation chain); it runs after the verification/digest-specific path (`_maybe_retry_worker`, budget `verification_max_retries`) declines, and shares `agent.retry_count` (not an extra budget). Owns the model failure counter and circuit-breaker logic.

### Model escalation
When a worker fails, `RetryPolicy` may promote it to a more capable model from the configured `model_escalation_chain`. The `RetryScheduled` event records the new model. Agent transitions FAILED -> ANALYZING for re-execution with the escalated model.

### Circuit breaker
Per-model failure counter in `RetryPolicy._model_failures` (`core/application/services/orchestration/retry_policy.py`). When a model's failure count reaches `circuit_breaker_threshold`, that model is skipped in the escalation chain. Prevents repeated failures with a broken model.

### Re-decomposition
A parent re-decomposes via `AgentSession.trigger_redecomposition()` when a child reports INFEASIBLE **or** when ALL children fail and re-plan budget remains (`max_redecompositions`, default 2). Emits `RedecompositionTriggered`; first snapshots `failed_children` into `failure_history`, then clears child state (`child_ids`, `child_reports`, `failed_children`) and transitions parent WAITING -> ANALYZING. Tracked by `AgentSession.redecomposition_count`. The retained `failure_history` renders as a `<previous_attempt_failures>` block in the volatile tail of the BOSS/MANAGER decomposition prompt (`prompts/operations/decomposition_variable.j2`), so re-planning is *informed* by why the prior attempt failed while the cached prompt prefix stays byte-identical.

### InfeasibleError
Raised when the LLM reports a task cannot be completed under current constraints (`core/domain/exceptions.py`). Wraps a `ConstraintFailure` value object. Triggers parent re-decomposition or propagates failure up the tree.

### ConstraintFailure
Frozen Pydantic value object representing an LLM response of `constraints_unsatisfiable` (`core/domain/values/constraint_failure.py`). Contains `reason`, optional `minimum_subtasks`, and `minimum_depth`. Detected via `ConstraintFailure.matches(data)` (checks for `status == "constraints_unsatisfiable"` in response dict).

### VerificationPipeline
Runs deterministic and judge-based verification on completed worker output (`core/application/services/orchestration/verification_pipeline.py`). Stages: `structural` (non-empty output), `deterministic` (format checks), `execution` (runtime checks), and optional `judge` (LLM-based quality gate, skippable via `skip_judge`). On failure, emits `VerificationFailed` with `failed_stage` and `feedback`; on success, emits `VerificationPassed`.

### Failure digest
A deterministic, LLM-free summary of a failed worker attempt, built by `build_failure_digest` (`core/application/services/orchestration/failure_digest.py`) from the aggregate's `error_message`, its `recent_thoughts` tool-output tail, and the retry count -- sections `FAILURE` / `LAST TOOL CALLS` / `ATTEMPT`, capped at 4000 chars via the shared `head_tail` (`truncation.py`). Recorded as `FailureDigestRecorded` (`source` = `worker_crash` or `procedure_failure`), retained across `RetryScheduled`, and injected into the retry prompt (`## Previous Attempt Failure (Retry)`) and the parent's `ChildFailureRecord`. Turns an opaque crash into reproducible, context-rich guidance for the next attempt.

### Self-healing failure loop
The end-to-end resilience path layered on top of `RetryPolicy`: worker crash -> bounded traceback (`describe_error`, ~1.5 KB, `infrastructure/adapters/worker/shared/errors.py`) attached to the failure reason -> deterministic `failure_digest` -> context-rich worker retry (budget `verification_max_retries`) -> if all sibling workers still fail, an *informed* parent re-decomposition seeded with `failure_history`. Every step is event-sourced and no LLM is used to build the digest.

---

## 9. Topology Limits

All defined in `core/domain/values/limits.py` as the frozen `HierarchyLimits` Pydantic model.

| Field | Type | Meaning |
|-------|------|---------|
| **max_depth** | `int` | Maximum tree depth (-1 = unlimited). At max depth, PENDING agents are forced to WORKER role. |
| **max_children_per_node** | `int` | Maximum children a single BOSS/MANAGER can spawn (-1 = unlimited). |
| **max_total_agents** | `int` | Global cap on agents across the entire hierarchy (-1 = unlimited). |
| **max_retries** | `int` | Retry budget per agent. |
| **current_depth** | `int` | This agent's depth in the tree (root = 0). |
| **current_total_agents** | `int` | Snapshot of total agents created so far. |
| **root_id** | `UUID` | Reference to the root agent / SharedStore. |
| **domain_context** | `object \| None` | Opaque slot for plugin-specific data (see section 6). |

Key methods: `for_child()` (increment depth), `can_spawn_child()`, `depth_remaining()`, `agents_remaining()`, `with_domain_context()`, `with_agent_counts()`.

### max_run_duration_seconds
Defined on `SystemLimitsPort` (`core/ports/runtime_ports.py`). Global wall-clock timeout for the entire execution run. Enforced by the system loop in `ExecutionService` (`core/application/execution_service.py`).

### max_redecompositions
Tracked by `AgentSession.redecomposition_count` (default 2). Limits how many times a parent can re-decompose after child infeasibility **or** after all its children fail (informed re-decomposition).

### HierarchyLimitsRegistry
In-memory registry that tracks and propagates limits from parent to child (`core/application/services/lifecycle/hierarchy_limits_registry.py`). Methods: `create_root()`, `propagate_to_child()` (increments depth), `get()`, `set()`, `has_limits()`, `get_root_id()`. One registry per execution run, reset between runs.

---

## 10. Security Domain (plugins/security/)

All code in this category lives strictly under `plugins/security/`. No non-security code belongs here.

### CVEInstance
Frozen Pydantic model representing a CVE from the SEC-bench dataset (`plugins/security/cve_instance.py`). Fields: `instance_id`, `repo`, `project_name`, `lang`, `work_dir`, `sanitizer`, `bug_description`, `base_commit`, `build_sh`, `secb_sh`, `dockerfile`, `patch`, `sanitizer_report`, `bug_report`. Computed fields: `cve_id`, `docker_image`, `expected_sanitizer_error`, `has_gold_patch`, `has_dockerfile`. Loaded from JSON via `from_json_file()`. Carried through the hierarchy as `domain_context`.

### SEC-bench
The security vulnerability benchmark dataset. Each entry is a `CVEInstance` with a known vulnerability, build scripts, and expected sanitizer behavior. The system attempts to reproduce and fix vulnerabilities in containerized environments across three phases (builder, exploiter, fixer).

### SecBenchPromptStrategy
`PromptStrategy` implementation for SEC-bench runs (`plugins/security/prompt_strategy.py`). Extends default prompt chains with CVE-specific context (vulnerability description, sanitizer info, tool prescriptions). Detects which benchmark branch (builder/exploiter/fixer/reporter) an agent belongs to via ancestry inspection in `detect_benchmark_branch()`.

### DooD (Docker-out-of-Docker)
Pattern where the orchestrator container shares the host's Docker socket to launch SEC-bench worker containers (`plugins/security/docker_runtime.py`). Volume bind paths must be expressed as host paths (via `HOST_PROJECT_ROOT` env var) because the Docker daemon runs on the host, not inside the orchestrator container. Implemented in `DockerSecBenchRuntime`.

### SecurityTool
Frozen Pydantic model describing a security analysis tool available in SEC-bench containers (`plugins/security/security_tool.py`). Fields: `name`, `description`, `commands`, `applicable_phases`. Registry: `SECURITY_TOOL_REGISTRY` dict (includes valgrind, klee, etc.). Used by prompt enrichment to prescribe tools per phase via `get_tools_for_phase()`.

### SecurityDomainPlugin
Concrete `DomainPlugin` implementation (`plugins/security/plugin.py`). Infers `CVEInstance` from task text via `CVEInstanceInferenceService`, prepares container workspaces via `SecurityContainerRuntime`, manages Docker lifecycle, returns `SecBenchPromptStrategy` from `get_prompt_strategy()`, and (when procedural dispatch is enabled) returns `SecBenchProcedureExecutor` from `get_procedure_executor()`.

### SecBenchProcedureExecutor
The security plugin's `ProcedureExecutorPort` implementation (`plugins/security/procedures.py`) for the deterministic validator tier. Maps `[Exploit-Validator]` -> `secb_exploit_validation` and `[Patch-Validator]` -> `secb_patch_validation`, drives the run's existing container synchronously (`secb repro` x3 / `secb patch` / `secb build`), writes the same on-disk verdict files as the agentic path (`exploit_validation_results.txt` / `patch_validation_results.txt`), and computes the verdict by comparing crash signatures against the CVE oracle. Bound only when `settings.orchestration.procedural_dispatch` is on.

### Crash signature
A structured reduction of a sanitizer crash to `(sanitizer_class, access_type, top_frame)` (`plugins/security/crash_signature.py`: `compute_crash_signature()`, `signatures_match()`). Lets the procedural validator decide exploit PASS/FAIL by matching the observed crash against the CVE oracle across 3/3 deterministic runs, host-side and agent-unforgeable. Drift-tested against `experiments/shared/evaluation/criteria.py::_crash_signature`.
