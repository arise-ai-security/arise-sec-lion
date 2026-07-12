<!-- Read this when: working on aggregates, events, value objects, or domain services -->
The core domain layer contains two event-sourced aggregates (`AgentSession` primary + `SharedStore`), 42 frozen domain events, immutable value objects, and stateless domain services -- all under `core/domain/`.

---

## AgentSession Aggregate

**File:** `core/domain/aggregates/agent_session.py`

`AgentSession` is the primary aggregate (39 of the 42 event types); `SharedStore` (`core/domain/shared_context.py`) is a second event-sourced aggregate (3 event types: `SharedContextCreated`, `ArtifactStored`, `DecisionRecorded`) sharing the events table via a `uuid5`-derived `aggregate_id`. All state is derived from replaying domain events -- there are no mutable state tables. `VerificationFailed.failed_stage` is one of `structural`/`deterministic`/`execution`/`judge`.

### Construction Rules

Never call `AgentSession(agent_id)` directly. Use one of:

- `AgentSession.create(agent_id, role, config, ...)` -- creates a new session, emits `AgentCreated`.
- `AgentSession.load_from_history(events)` -- replays a list of events to reconstruct state. First event must be `AgentCreated`.

### Event Replay Mechanics

1. Domain method (e.g., `assign_task()`) constructs an event with `_next_sequence()`.
2. `_emit(event)` calls `_apply(event)` (updates internal state) then appends to `_changes`.
3. `_apply` uses `@singledispatchmethod` with one `@_apply.register` handler per event type.
4. After persistence via `EventStoreWritePort.append_batch()`, call `mark_changes_as_committed()` to clear `_changes`.
5. On load, `load_from_history()` calls `_apply(event)` for each event without appending to `_changes`.

### Key Properties

| Property | Type | Source |
|---|---|---|
| `agent_id` | `UUID` | Constructor arg |
| `role` | `AgentRole` | `AgentCreated`, updated by `ComplexityEvaluated` |
| `status` | `AgentStatus` | Lifecycle events |
| `parent_id` | `UUID \| None` | `AgentCreated` |
| `task_description` | `str` | `TaskAssigned` |
| `result` | `str \| None` | `WorkCompleted` (cleared on `RetryScheduled`) |
| `error_message` | `str \| None` | `WorkFailed`, `DecisionInfeasible`, `VerificationFailed` (cleared on `RetryScheduled`) |
| `child_ids` | `list[UUID]` | Appended by `ChildSpawned`, cleared by `RedecompositionTriggered` |
| `child_reports` | `dict[UUID, Report]` | Populated by `ChildCompleted`, cleared by `RedecompositionTriggered` |
| `failed_children` | `dict[UUID, ChildFailureRecord]` | Populated by `ChildFailed` (`child_id`, `child_task`, `reason`, `digest`); snapshotted into `failure_history` then cleared by `RedecompositionTriggered` |
| `config` | `AgentConfig` | `AgentCreated`, may change on `RetryScheduled` (model escalation) |
| `version` | `int` | Incremented by every `_apply` handler (OCC via unique constraint on `(aggregate_id, sequence_number)`) |
| `briefing` | `Briefing \| None` | `AgentCreated` (deserialized from dict) |
| `hierarchy_limits` | `HierarchyLimits \| None` | Set externally by `ExecutionService` via `set_hierarchy_limits()` |
| `local_decisions` | `list[str]` | In-memory via `record_decision()` |
| `local_artifacts` | `list[str]` | In-memory via `record_artifact()` |
| `retry_count` | `int` | `RetryScheduled` |
| `redecomposition_count` | `int` | `RedecompositionTriggered` |
| `verification_feedback` | `str \| None` | `VerificationFailed` (for retry prompts); retained across `RetryScheduled` |
| `failure_digest` | `str \| None` | `FailureDigestRecorded` (deterministic crash/procedure digest for retry + parent prompts); retained across `RetryScheduled`, mirroring `verification_feedback` |
| `recent_thoughts` | `deque[ThoughtExcerpt]` (maxlen 20) | Appended by `ThoughtCaptured` (bounded tool-output tail; source material for the failure digest) |
| `failure_history` | `list[ChildFailureRecord]` | Extended from `failed_children` on `RedecompositionTriggered` (prior attempts' failures for informed re-decomposition) |
| `last_attempt_procedural` | `bool` | Set True by `ProcedureExecutionStarted` (once-only guard: a failed procedure escalates to an *agentic* retry, never a second procedure) |
| `sibling_index` | `int` | `AgentCreated` (position among siblings, 0-indexed) |
| `success_criteria` | `str` | `AgentCreated` (from parent's `Subtask`) |
| `target_paths` | `tuple[str, ...]` | `AgentCreated` (structured child scoping) |
| `symbols` | `tuple[str, ...]` | `AgentCreated` |
| `search_hints` | `tuple[str, ...]` | `AgentCreated` |

### Domain Methods

| Method | Effect |
|---|---|
| `assign_task(description)` | Emits `TaskAssigned`, status -> ANALYZING |
| `apply_complexity_result(complexity, reasoning, determined_role)` | Emits `ComplexityEvaluated`, updates role |
| `apply_subtasks_and_spawn_children(subtasks, child_role, briefing)` | Emits `SubtasksDefined` + N x `ChildSpawned` + `StatusChanged(WAITING)`. Returns `list[(child_id, subtask)]` |
| `start_worker_execution(tool_name)` | Emits `CodeGenerationStarted`, status -> IN_PROGRESS |
| `handle_child_update(child_id, result, report)` | Emits `ChildCompleted`; auto-emits `WorkCompleted` when all children reported |
| `handle_child_failure(child_id, reason, child_task, digest, max_redecompositions)` | Emits enriched `ChildFailed` (carries `child_task` + `digest`); when all children reported: partial `WorkCompleted` if any succeeded; if ALL failed, emits `RedecompositionTriggered` while `redecomposition_count < max_redecompositions` (informed re-decomposition), else `WorkFailed` |
| `mark_infeasible(reason, min_subtasks, min_depth)` | Emits `DecisionInfeasible`, status -> FAILED |
| `trigger_redecomposition(child_id, reason)` | Emits `RedecompositionTriggered`, WAITING -> ANALYZING, clears children |
| `schedule_retry(reason, escalated_model)` | Emits `RetryScheduled`, FAILED -> ANALYZING, optionally upgrades model config |
| `mark_verification_failed(stage, feedback, stages_passed)` | Emits `VerificationFailed`, status -> FAILED |
| `fail_with_reason(reason)` | Emits `WorkFailed`, status -> FAILED |
| `mark_verification_passed(feedback)` | Emits `VerificationPassed` (observability only, status stays COMPLETED) |
| `record_failure_digest(digest, source)` | Emits `FailureDigestRecorded` (`source` ∈ `"worker_crash"`/`"procedure_failure"`); sets `failure_digest` for retry/parent prompts |
| `start_procedure(procedure_ref)` | Emits `ProcedureExecutionStarted`, status -> IN_PROGRESS, sets `last_attempt_procedural` (deterministic tier; no prompt, no LLM) |
| `finish_procedure(procedure_ref, success, summary, evidence)` | Emits `ProcedureExecutionFinished` with host-captured `evidence`; the terminal `WorkCompleted`/`WorkFailed` follows separately |
| `complete_with_result(result)` | Emits `WorkCompleted` directly from a host-computed result (procedural success path) |
| `record_decision(decision)` | Appends to `local_decisions` (no event, in-memory only) |
| `record_artifact(artifact_key)` | Appends to `local_artifacts` (no event, in-memory only) |
| `build_briefing_for_child()` | Build `Briefing` with full ancestry chain via `build_briefing()` |
| `build_report()` | Build structured `Report` to return to parent (aggregates cost from uncommitted events) |
| `set_hierarchy_limits(limits)` | Sets `hierarchy_limits` (called by `ExecutionService`, no event) |
| `is_terminal()` | True if COMPLETED or FAILED |
| `is_leaf()` | True if WORKER with no children |

### Observability Emitters

These emit events that only increment `version` (no state change):

| Method | Event |
|---|---|
| `emit_tokens_consumed(...)` | `TokensConsumed` |
| `emit_limit_enforced(...)` | `LimitEnforced` |
| `emit_prompt_sent(prompt, type, target)` | `PromptSent` |
| `emit_execution_started(role, depth)` | `AgentExecutionStarted` |
| `emit_execution_finished(role, status, duration)` | `AgentExecutionFinished` |
| `emit_operation_started(operation_type)` | `OperationStarted` |
| `emit_operation_finished(operation_type, duration)` | `OperationFinished` |
| `emit_probe_started(probe_type)` | `ProbeStarted` |
| `emit_probe_completed(probe_type, result_summary)` | `ProbeCompleted` |
| `emit_run_started(task, domain_metadata)` | `RunStarted` (BOSS only) |
| `emit_run_completed(status, duration, agents...)` | `RunCompleted` (BOSS only) |
| `apply_worker_event(tool_event)` | Re-emits any worker event with corrected aggregate_id/sequence |

### Invariants Enforced

- Cannot spawn a child with `AgentRole.BOSS` (`DomainInvariantError` in `_apply` for `ChildSpawned`).
- `handle_child_update` / `handle_child_failure` require: role is BOSS or MANAGER, status is WAITING, child_id is in `child_ids` (via `_assert_parent_can_receive_child_event`).
- `load_from_history` requires non-empty list, first event must be `AgentCreated`.

---

## Status Lifecycle

```
                                 +------------------+
                                 |     PENDING      |  <-- AgentCreated
                                 +------------------+
                                         |
                                   TaskAssigned
                                         |
                                         v
                      +----------> ANALYZING <-----------+
                      |          +----------+            |
                      |           /        \             |
                      |   (execute)      (decompose)     |
                      |         /            \           |
            RetryScheduled     v              v    RedecompositionTriggered
            (FAILED->)    IN_PROGRESS      WAITING      (<-WAITING)
                      |        |              |          |
                      |   WorkCompleted  all children    |
                      |   WorkFailed     reported:       |
                      |        |          any ok ->      |
                      |        v       WorkCompleted     |
                      +--- FAILED      all fail ->      |
                      |        |       WorkFailed        |
                      |        v              |          |
                      +--- COMPLETED      FAILED -------+
                                            |
                          DecisionInfeasible (-> FAILED)
                          VerificationFailed (-> FAILED)
```

### Status Definitions

| Status | Meaning |
|---|---|
| `PENDING` | Created, awaiting task assignment |
| `ANALYZING` | Evaluating complexity / retrying / re-decomposing |
| `IN_PROGRESS` | Worker executing via tool (Claude Code, OpenHands, ADK) |
| `WAITING` | Parent waiting for children to complete |
| `COMPLETED` | Terminal success |
| `FAILED` | Terminal failure |
| `BLOCKED` | Blocked by DAG dependency (scheduled for later) |

### Role Definitions

| Role | Meaning |
|---|---|
| `BOSS` | Root agent, one per run. Created at top level |
| `PENDING` | Newly spawned child, awaiting complexity assessment |
| `MANAGER` | Assessed as complex, decomposes into child subtasks |
| `WORKER` | Assessed as simple, executes directly via worker tool |

### Transition Table

| From | To | Trigger Event |
|---|---|---|
| PENDING | ANALYZING | `TaskAssigned` |
| ANALYZING | IN_PROGRESS | `CodeGenerationStarted` |
| ANALYZING | WAITING | `StatusChanged` (after spawning children) |
| WAITING | COMPLETED | `WorkCompleted` (all children reported, at least one succeeded) |
| WAITING | FAILED | `WorkFailed` (all children failed) |
| IN_PROGRESS | COMPLETED | `WorkCompleted` |
| Any active | FAILED | `WorkFailed`, `DecisionInfeasible`, `VerificationFailed` |
| FAILED | ANALYZING | `RetryScheduled` |
| WAITING | ANALYZING | `RedecompositionTriggered` |

---

## Domain Events

**File:** `core/domain/events/events.py`

All events extend `DomainEvent` (frozen Pydantic `BaseModel`). Base fields: `event_id` (UUID), `aggregate_id` (UUID), `sequence_number` (int), `occurred_at` (datetime), `metadata` (dict). A `model_validator(mode="before")` deep-copies all dict/list values to enforce true immutability.

### Lifecycle Events

| Event | Trigger | State Effect |
|---|---|---|
| `AgentCreated` | `AgentSession.create()` | Sets role, status=PENDING, config, parent_id, sibling_index, briefing, scoping fields, success_criteria |
| `TaskAssigned` | `assign_task()` | Sets task_description, status=ANALYZING |
| `StatusChanged` | `_emit_waiting_for_children()` | Updates status to `new_status` |
| `WorkCompleted` | Worker done / all children done | status=COMPLETED, stores result |
| `WorkFailed` | Worker fails / child fails | status=FAILED, stores error_message |
| `ChildSpawned` | `apply_subtasks_and_spawn_children()` | Appends child_id; invariant: child_role != BOSS |
| `ChildCompleted` | `handle_child_update()` | Stores Report in child_reports |
| `ChildFailed` | `handle_child_failure()` | Adds a `ChildFailureRecord` (`child_id`, `child_task`, `reason`, `digest`) to the `failed_children` dict. `child_task`/`digest` default empty, so pre-enrichment rows replay unchanged |

### Decomposition Events

| Event | Trigger | State Effect |
|---|---|---|
| `ComplexityEvaluated` | `apply_complexity_result()` | Updates role to `determined_role` |
| `SubtasksDefined` | `_emit_subtasks_defined()` | Version bump only (subtasks in event payload) |

### Execution Events

| Event | Trigger | State Effect |
|---|---|---|
| `CodeGenerationStarted` | `start_worker_execution()` | status=IN_PROGRESS |
| `ThoughtCaptured` | `apply_worker_event()` | Appends a `ThoughtExcerpt` (content capped at 500 chars) to `recent_thoughts`; version bump. Fields: `content`, `stream`, `output_type`, `tool_name` |
| `PromptSent` | `emit_prompt_sent()` | Version bump only. Fields: `prompt`, `prompt_type`, `target` |
| `ProcedureExecutionStarted` | `start_procedure()` | status=IN_PROGRESS, sets `last_attempt_procedural`. Field: `procedure_ref`. Deterministic tier -- no prompt, no LLM turns |
| `ProcedureExecutionFinished` | `finish_procedure()` | Version bump only (terminal `WorkCompleted`/`WorkFailed` follows). Fields: `procedure_ref`, `success`, `summary`, `evidence: list[dict]` (host-captured, agent-unforgeable) |

### Infeasibility and Retry Events

| Event | Trigger | State Effect |
|---|---|---|
| `DecisionInfeasible` | `mark_infeasible()` | status=FAILED. Fields: `reason`, `minimum_subtasks`, `minimum_depth` |
| `RetryScheduled` | `schedule_retry()` | status=ANALYZING, increments retry_count, optionally upgrades model. Preserves `failure_digest` + `verification_feedback` for the retry prompt |
| `FailureDigestRecorded` | `record_failure_digest()` | Sets `failure_digest`. Fields: `digest`, `source` (`"worker_crash"`/`"procedure_failure"`) |
| `RedecompositionTriggered` | `trigger_redecomposition()` | status=ANALYZING; extends `failure_history` from `failed_children`, then clears `child_ids`/`child_reports`/`failed_children`; increments redecomposition_count. Trigger: child infeasible **or** all children failed with re-plan budget left |
| `VerificationFailed` | `mark_verification_failed()` | status=FAILED. Fields: `failed_stage`, `feedback`, `stages_passed` |
| `VerificationPassed` | `mark_verification_passed()` | Version bump only (observability). Field: `feedback` |

### Probing Events

| Event | Trigger | State Effect |
|---|---|---|
| `ProbeStarted` | `emit_probe_started()` | Version bump only. Field: `probe_type` |
| `ProbeCompleted` | `emit_probe_completed()` | Version bump only. Fields: `probe_type`, `result_summary` |

### Cost Tracking Events

| Event | Trigger | State Effect |
|---|---|---|
| `TokensConsumed` | `emit_tokens_consumed()` | Version bump only. Fields: `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `operation` |
| `WorkerCostRecorded` | `apply_worker_event()` | Version bump only |
| `LimitEnforced` | `emit_limit_enforced()` | Version bump only. Fields: `limit_type`, `limit_value`, `attempted_value`, `action_taken` |

`WorkerCostRecorded` carries detailed worker metrics: `tool_name`, `model`, granular token fields (`prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`), `usage_metrics` (list of `WorkerUsageMetrics` with per-response latencies and token breakdowns), `cost_usd`, `duration_seconds`. Helper properties: `total_recorded_tokens`, `model_costs`.

### Timing/Observability Events

| Event | Trigger | State Effect |
|---|---|---|
| `AgentExecutionStarted` | `emit_execution_started()` | Version bump only. Fields: `role`, `depth` |
| `AgentExecutionFinished` | `emit_execution_finished()` | Version bump only. Fields: `role`, `status`, `duration_seconds` |
| `OperationStarted` | `emit_operation_started()` | Version bump only. Field: `operation_type` |
| `OperationFinished` | `emit_operation_finished()` | Version bump only. Fields: `operation_type`, `duration_seconds` |
| `RunStarted` | `emit_run_started()` (BOSS only) | Version bump only. Fields: `task_description`, `domain_metadata` |
| `RunCompleted` | `emit_run_completed()` (BOSS only) | Version bump only. Fields: `status`, `duration_seconds`, `total_agents`, `completed_agents`, `failed_agents` |

### Shared Context Events

Not emitted by `AgentSession` directly. Record shared-store operations for audit trail.

| Event | Purpose |
|---|---|
| `SharedContextCreated` | Shared store created for a run (keyed by `root_id`) |
| `ArtifactStored` | Artifact stored in shared context (`key`, `content_type`, `content` or `content_hash`) |
| `DecisionRecorded` | Architectural decision recorded (`decision_key`, `decision_value`, `rationale`) |

---

## Value Objects

**Directory:** `core/domain/values/`

All are frozen Pydantic models or frozen dataclasses.

### AgentConfig (`agent_config.py`)

`AgentConfig` is a type alias for `HeuristicConfig`. Fields: `strategy: Literal["heuristic"]`, `base: LLMConfig` (model, temperature, max_tokens), `tool: Literal["claude_code", "openhands", "google_adk"]`.

`VALID_WORKER_TOOLS = {"claude_code", "openhands", "google_adk"}`. Default: `"claude_code"`.

### Enums (`enums.py`)

- `AgentRole(str, Enum)`: `BOSS`, `PENDING`, `MANAGER`, `WORKER`.
- `AgentStatus(str, Enum)`: `PENDING`, `ANALYZING`, `IN_PROGRESS`, `WAITING`, `COMPLETED`, `FAILED`, `BLOCKED`.

### NodeMessage (`node_message.py`)

Discriminated union on the `direction` field for all inter-agent communication:

| Variant | Direction | Purpose | Key Fields |
|---|---|---|---|
| `Briefing` | `"down"` | Parent -> Child at spawn | `parent_task`, `parent_role`, `ancestry: tuple[Ancestor, ...]`, `decisions`, `subtask_justification` |
| `Report` | `"up"` | Child -> Parent on completion | `task`, `result`, `artifacts`, `decisions`, `execution_summary` |
| `Handoff` | `"lateral"` | Sibling <-> Sibling coordination | `siblings: tuple[PeerStatus, ...]`, `shared_decisions`, computed `total_siblings`, `completed_count`, `has_downstream_siblings` |

Supporting types: `Ancestor` (agent_id, role, task_summary), `PeerStatus` (agent_id, sibling_index, status, task/result summaries), `SharedDecision` (key, value, rationale).

Helper: `build_briefing(agent, parent_briefing)` constructs `Briefing` with full ancestry chain appended.

### Subtask (`subtask.py`)

Represents a decomposed task unit. All new fields have defaults for backward compatibility.

| Field | Type | Purpose |
|---|---|---|
| `description` | `str` | Task description (required) |
| `config` | `dict[str, Any]` | Agent config dict (required) |
| `depends_on` | `list[int]` | Sibling indices this depends on (DAG scheduling) |
| `dependency_type` | `Literal[...]` | `"finish_to_start"`, `"data"`, `"none"` |
| `estimated_complexity` | `Literal[...]` | `"simple"`, `"complex"`, `"unknown"` |
| `success_criteria` | `str` | What constitutes success |
| `failure_indicators` | `list[str]` | Signs of failure |
| `task_type` | `str` | Category: `"general"`, `"research"`, `"implementation"`, etc. |
| `justification` | `dict[str, str]` | Parent's reasoning (objective, plan, domain context) |
| `target_paths` | `tuple[str, ...]` | Files/dirs child should focus on |
| `symbols` | `tuple[str, ...]` | Function/class names relevant to subtask |
| `search_hints` | `tuple[str, ...]` | Keywords to search for |
| `execution_mode` | `Literal["auto", "agentic", "procedural"]` | Parent's dispatch marking (default `"auto"`). `"agentic"` forces an LLM worker; `"procedural"` forces a registered procedure; unknown `procedural` refs downgrade to `"auto"` at spawn (fail open) |
| `procedure_ref` | `str` | Registered procedure id, used when `execution_mode == "procedural"` |
| `procedure_params` | `dict[str, Any]` | Optional parameters passed to the procedure executor |

### HierarchyLimits (`limits.py`)

Immutable limits passed down the agent tree. Key design: `domain_context: object | None` is an opaque slot for plugin-specific data (e.g., `CVEInstance`). Only the plugin downcasts -- core remains domain-ignorant.

| Field | Default | Purpose |
|---|---|---|
| `current_depth` | -- | Current position in tree |
| `max_depth` | -- | -1 = unlimited |
| `max_children_per_node` | -- | -1 = unlimited |
| `max_retries` | -- | Max retry attempts |
| `root_id` | -- | Reference to SharedStore |
| `max_total_agents` | -1 | Global limit across hierarchy |
| `current_total_agents` | 0 | Snapshot of agents created so far |
| `domain_context` | None | Opaque plugin data |

Key methods: `for_child()` (increments depth), `can_spawn_child()`, `depth_remaining()`, `agents_remaining()`, `with_domain_context(obj)`, `with_agent_counts(current, max)`, `create_root(...)`.

### LLMResponse (`llm_response.py`)

- `LLMUsage`: `prompt_tokens`, `completion_tokens`, `total_tokens`. Supports `+` for accumulation.
- `LLMResponse`: `content`, `usage: LLMUsage`, `model`, `cost_usd`.
- `ToolCall`: `id`, `name`, `arguments: dict`.
- `LLMToolResponse`: `content | None`, `tool_calls: list[ToolCall]`, `usage`, `model`, `cost_usd`. Property: `has_tool_calls`.

### ConstraintFailure (`constraint_failure.py`)

Captures when LLM returns `status: "constraints_unsatisfiable"`. Static `matches(data)` checks for that status. `from_llm_response(data)` extracts `reason`, `minimum_subtasks`, `minimum_depth` from the response dict.

### Failure Context (`failure.py`)

Frozen `slots` dataclasses that feed the self-healing retry / informed-redecomposition loop.

- `ThoughtExcerpt`: `tool_name: str | None`, `output_type: str`, `content: str`. A bounded excerpt of one worker tool output, retained in `AgentSession.recent_thoughts` (deque maxlen 20) and consumed by `build_failure_digest`.
- `ChildFailureRecord`: `child_id: UUID`, `child_task: str`, `reason: str`, `digest: str \| None`. What a parent retains per failed child in `failed_children` / `failure_history` for informed re-decomposition.

### Procedure (`procedure.py`)

Result values for the deterministic procedure tier (procedural dispatch). Both frozen Pydantic models.

- `ProcedureEvidence`: `argv: tuple[str, ...]`, `exit_code: int`, `output_sha256: str`, `excerpt: str`. Host-captured record of one procedure command -- **agent-unforgeable** (written by the host, not the worker shell).
- `ProcedureResult`: `success: bool`, `summary: str`, `digest: str`, `evidence: tuple[ProcedureEvidence, ...]`. `summary` becomes `WorkCompleted.result` on success; `digest` carries bounded failure context for the agentic escalation on failure.

### Other Value Objects

| File | Type | Purpose |
|---|---|---|
| `recon_policy.py` | `ReconPolicy` (frozen dataclass) | Controls recon tool-calling loop: `enabled`, `max_iterations` (5), `result_char_limit` (6000), `allowed_tools` |
| `prompt_capabilities.py` | `PromptCapabilities`, `PromptToolDescriptor` (frozen dataclasses) | Runtime tool metadata for prompt rendering. `PromptToolDescriptor.from_tool_definition()` parses OpenAI format |
| `prompt_trace.py` | `SectionProvenance`, `PromptSection`, `ParsedPrompt`, `AgentNode`, `HierarchyTrace` | Prompt section provenance tracking for observability |
| `parsed_context.py` | `ParsedUpdate`, `ParsedDecision`, `ParsedArtifact` | Parsed `<context-update>` XML from worker output |
| `json_types.py` | `JsonPrimitive`, `JsonObject` | Type aliases for JSON-safe values |

---

## Domain Services

**Directory:** `core/domain/services/`

All are stateless with no I/O. They operate on values and return values.

### SubtaskParser (`subtask_parser.py`)

- `parse_subtasks_from_llm(response) -> list[Subtask]`: Strips markdown code blocks, parses JSON, validates against `AgentConfig` schema. Raises `InfeasibleError` if LLM returns `constraints_unsatisfiable`, `ValueError` if malformed.
- `parse_assessment_response(response) -> AssessmentResult`: Parses unified assess response. Returns frozen dataclass with `action` (`"execute"` | `"decompose"` | `"infeasible"`), `reasoning`, optional `subtasks`, optional `constraint_failure`.
- `_sanitize_llm_subtask(item, idx)`: Fixes known hallucination patterns -- replaces invalid tool names with `"claude_code"`, drops non-integer `depends_on` values, coerces non-string justification values to JSON strings.
- `_extract_subtask_list(data)`: Normalizes various LLM response shapes (list, `{"subtasks": [...]}`, `{"tasks": [...]}`, `{"children": [...]}`, single dict) to `list[dict]`.
- `_resolve_depends_on(items)`: Resolves string task-name references (e.g., `depends_on: ["Build-Setup"]`) to integer indices by matching bracketed description prefixes.

### TaskScheduler (`task_scheduler.py`)

DAG scheduler using Kahn's algorithm for sibling execution ordering.

- `TaskScheduler.from_dependencies({0: [], 1: [0], 2: [0], 3: [1, 2]})` -- factory from index-to-deps mapping.
- `get_ready() -> list[int]` -- returns sorted indices with all deps completed.
- `mark_completed(idx)` / `mark_failed(idx)` -- updates internal state.
- `invalidate_dependents(failed_idx) -> list[int]` -- cascading failure via BFS. Returns newly invalidated indices.
- `has_cycle() -> bool` -- cycle detection via Kahn's algorithm.
- `is_complete() -> bool` -- all tasks either completed or failed.

When no `depends_on` edges exist, all tasks are immediately ready (parallel execution).

### ConfigResolver (`config_resolver.py`)

Resolves `AgentConfig` to operation-specific `LLMConfig`:

| Operation | Temperature | Max Tokens |
|---|---|---|
| `complexity_evaluation` | 0.3 | min(base, 500) |
| `task_decomposition` | base | base |
| `task_assessment` | base | base |

### ContextUpdateParser (`context_update_parser.py`)

`parse_context_update(result) -> ParsedUpdate | None`: Extracts `<context-update>` XML blocks from worker output. Parses `<decision>` and `<output>` child elements into `ParsedDecision` and `ParsedArtifact` tuples. Returns `None` if no valid block found or XML is malformed.

---

## Domain Exceptions

**File:** `core/domain/exceptions.py`

| Exception | Base | When | Key Attributes |
|---|---|---|---|
| `DomainInvariantError` | `ValueError` | Aggregate invariant violated (spawn BOSS child, child update on non-WAITING) | -- |
| `InvalidEventHistoryError` | `ValueError` | Malformed event history (empty, wrong first event) | -- |
| `ConcurrencyError` | `Exception` | OCC conflict during `append`/`append_batch` | `aggregate_id`, `expected_version`, `actual_version` |
| `LLMError` | `Exception` | LLM API failure, rate limit, timeout | `message`, `original_error` |
| `EventStoreError` | `Exception` | DB connection, query, serialization failure | `message`, `original_error` |
| `ToolNotAvailableError` | `Exception` | Requested worker tool not configured | `tool_name`, `available_tools` |
| `InfeasibleError` | `ValueError` | LLM reports task infeasible under constraints | `failure: ConstraintFailure` |
| `CostInvariantViolation` | `Exception` | Cost calculation bug (sum mismatch, negative cost) | `invariant`, `expected`, `actual`, `context` |

---

## Ports (Protocol Interfaces)

**Directory:** `core/ports/`

Four files, all using `typing.Protocol`. Ports define what the domain needs from infrastructure, never how.

### Event Store ISP (`event_store_port.py`)

Segregated following Interface Segregation Principle:

| Port | Methods |
|---|---|
| `EventStoreConnectPort` | `connect()`, `disconnect()`, `initialize_schema()` |
| `EventStoreWritePort` | `append(event, expected_version)`, `append_batch(events, expected_version)` |
| `EventStoreReadPort` | `get_events(aggregate_id, *, limit, after_sequence)`, `get_all_aggregate_ids()`, `get_all_events_grouped(*, limit, offset)`, `get_boss_agents_grouped(*, limit, offset)`, `get_hierarchy_events_grouped(root_id)`, `get_children_events_grouped(parent_id)`, `get_boss_agent_summaries(*, limit, offset)`, `get_events_batch_incremental(agent_sequences)` |
| `EventStorePort` | Composite of all three |

OCC is enforced via `expected_version` on writes. Raises `ConcurrencyError` on mismatch. Clients should depend on the narrowest interface they need.

### Runtime Ports (`runtime_ports.py`)

| Port | Key Methods | Purpose |
|---|---|---|
| `LLMPort` | `query(prompt, config)`, `query_with_usage(prompt, config)`, `query_with_tools(messages, config, tools)` | LLM interaction with tool-calling support |
| `WorkerToolPort` | `run_session(task_context) -> AsyncIterator[DomainEvent]` | Stream events from worker tools |
| `CostCalculatorPort` | `calculate_llm_cost(model, prompt_tokens, completion_tokens)`, `calculate_worker_cost(tool, model, tokens, duration)` | USD cost calculation |
| `RealtimeCallbackPort` | `on_event(event, root_id)` | Stream events to SSE consumers |
| `SharedContextPort` | `get_or_create(root_id, config)`, `get(root_id)`, `save(context, expected_version)`, `exists(root_id)` | SharedStore persistence |
| `SiblingViewPort` | `build_view(agent_id, parent_id, root_id) -> Handoff` | Build sibling coordination snapshot |
| `SystemLimitsPort` | Properties: `max_depth`, `max_children_per_node`, `max_total_agents`, `max_concurrent_workers`, `max_concurrent_llm_calls`, `llm_jitter_max_ms`, `max_run_duration_seconds` | Execution limits |
| `Toolset` | `name`, `get_tool_definitions()`, `execute_tool(name, arguments)` | Named tool provider (OpenAI function-calling format) |
| `ReconToolPort` | Extends `Toolset`. `read_file()`, `list_directory()`, `search_codebase()`, `find_file()`, `get_file_structure()`, `get_symbols_overview()`, `read_symbol()` | Read-only reconnaissance for PENDING/MANAGER assessment |

### Procedure Executor Port (`procedure_ports.py`)

| Port | Methods | Purpose |
|---|---|---|
| `ProcedureExecutorPort` | `match(task_description, domain_context) -> str \| None`, `resolve(procedure_ref) -> bool`, `async execute(procedure_ref, task_description, domain_context, params) -> ProcedureResult` | The deterministic worker tier. `match` maps a task to a registered `procedure_ref`; `resolve` validates an LLM-supplied ref; `execute` runs it host-side. Task-level failure returns `success=False` + digest; raises only for infrastructure faults (the caller escalates agentically) |
| `NullProcedureExecutor` | Same three methods | Default binding when no plugin provides procedures: `match` -> None, `resolve` -> False, `execute` raises. With this bound, dispatch always takes the agentic path -- behavior byte-identical to pre-feature |

### Domain Plugin Port (`domain_plugin_port.py`)

| Port | Purpose |
|---|---|
| `DomainPlugin` | Optional domain-specific behavior injected at bootstrap. The security plugin (`plugins/security/`) implements this |

Key methods: `infer_context(task_text)`, `enrich_prompt(prompt, *, domain_context, briefing)`, `get_run_metadata(domain_context)`, `get_tag_mappings()`, `get_provenance_patterns()`, `prepare_run(*, root_id, run_output_path, domain_context)`, `prepare_worker_execution(*, root_id, agent_id, run_output_path, domain_context)`, `cleanup_worker_execution(...)`, `get_prompt_strategy()`, `get_procedure_executor()` (returns a `ProcedureExecutorPort \| None`; bound by bootstrap only when `settings.orchestration.procedural_dispatch` is on).

Supporting dataclasses: `PreparedRunWorkspace` (workspace override with `working_directory`), `WorkerExecutionContext` (optional `working_directory` and `task_context` dict).
