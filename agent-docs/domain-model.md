# Domain Model

This document describes the core domain entities, events, and state machine that govern agent behavior.

---

## AgentSession Aggregate

The `AgentSession` class (`core/domain/aggregates/agent_session.py`) is the aggregate root. All agent state is derived by replaying events.

```python
# Never store state directly - reconstruct from events
agent = AgentSession.load_from_history(events)
```

### Key Properties

| Property | Type | Description |
|----------|------|-------------|
| `agent_id` | `UUID` | Unique identifier |
| `role` | `AgentRole` | BOSS, MANAGER, WORKER, or PENDING |
| `status` | `AgentStatus` | Current lifecycle state |
| `parent_id` | `UUID \| None` | Parent agent (None for BOSS) |
| `child_ids` | `list[UUID]` | Spawned children |
| `task_description` | `str` | Assigned task |
| `result` | `str \| None` | Final result (when completed) |
| `version` | `int` | OCC version for concurrency control |

---

## Agent Roles

Defined in `core/domain/values/enums.py`:

| Role | Purpose | Next Action |
|------|---------|-------------|
| `BOSS` | Root agent, receives initial task | Decomposes into subtasks |
| `MANAGER` | Complex subtask handler | Decomposes further |
| `WORKER` | Simple task executor | Runs worker tool (Claude Code) |
| `PENDING` | Newly spawned, awaiting evaluation | LLM evaluates complexity |

---

## Agent Status (Lifecycle)

Defined in `core/domain/values/enums.py`:

```
PENDING ──► ANALYZING ──┬──► IN_PROGRESS ──► COMPLETED
                        │                         │
                        ├──► WAITING ─────────────┤
                        │    (for children)       │
                        │                         │
                        └──► FAILED ◄─────────────┘
```

| Status | Meaning |
|--------|---------|
| `PENDING` | Created, no task assigned |
| `ANALYZING` | Has task, processing (LLM call or worker execution) |
| `IN_PROGRESS` | Worker actively executing |
| `WAITING` | Manager/Boss waiting for children to complete |
| `COMPLETED` | Finished successfully |
| `FAILED` | Finished with error |

---

## Domain Events

All events in `core/domain/events/events.py` are immutable Pydantic models with `frozen=True`.

### Base Event Structure

Every event inherits from `DomainEvent` with these common fields:

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | `UUID` | Unique identifier for this event |
| `aggregate_id` | `UUID` | Agent ID this event belongs to |
| `sequence_number` | `int` | Order within the aggregate's event stream |
| `occurred_at` | `datetime` | UTC timestamp when event occurred |
| `metadata` | `dict` | Optional metadata (tracing, correlation IDs) |

---

### Lifecycle Events

Events that track agent creation, task assignment, and terminal states.

#### `AgentCreated`
Emitted when a new agent session is created via `AgentSession.create()`.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `str` | Initial role: "boss", "manager", "worker", or "pending" |
| `parent_id` | `UUID \| None` | Parent agent ID (None for BOSS) |
| `config` | `dict` | Agent configuration (model, tool, strategy) |
| `sibling_index` | `int` | Position among siblings (0 = first/leftmost) |
| `spawn_payload` | `dict \| None` | Parent context passed to child agents |

#### `TaskAssigned`
Emitted when a task is assigned to an agent via `agent.assign_task()`.

| Field | Type | Description |
|-------|------|-------------|
| `task_description` | `str` | The task to be performed |
| `constraints` | `dict` | Task constraints (limits, requirements) |

#### `StatusChanged`
Emitted on any status transition (PENDING→ANALYZING, ANALYZING→WAITING, etc.).

| Field | Type | Description |
|-------|------|-------------|
| `old_status` | `str` | Previous status |
| `new_status` | `str` | New status |
| `reason` | `str` | Human-readable reason for transition |

#### `WorkCompleted`
Emitted when an agent successfully completes its task.

| Field | Type | Description |
|-------|------|-------------|
| `result` | `str` | Final result/output of the work |

#### `WorkFailed`
Emitted when an agent fails to complete its task.

| Field | Type | Description |
|-------|------|-------------|
| `reason` | `str` | Error message or failure reason |

---

### Decomposition Events

Events related to task complexity evaluation and hierarchical decomposition.

#### `ComplexityEvaluated`
Emitted after LLM evaluates task complexity to determine agent role.

| Field | Type | Description |
|-------|------|-------------|
| `complexity` | `str` | "simple" or "complex" |
| `determined_role` | `str` | Resulting role: "worker" or "manager" |
| `reasoning` | `str` | LLM's reasoning for the determination |

#### `SubtasksDefined`
Emitted when a BOSS/MANAGER decomposes a task into subtasks.

| Field | Type | Description |
|-------|------|-------------|
| `subtasks` | `list[Subtask]` | List of subtask definitions |

#### `ChildSpawned`
Emitted when a parent agent spawns a child agent for a subtask.

| Field | Type | Description |
|-------|------|-------------|
| `child_id` | `UUID` | ID of the spawned child agent |
| `child_role` | `str` | Role assigned to child ("pending", "worker") |
| `subtask` | `Subtask` | The subtask assigned to the child |
| `child_config` | `dict` | Configuration for the child agent |
| `parent_context` | `dict` | Serialized SpawnPayload with ancestry, decisions |
| `sibling_index` | `int` | Child's position among siblings (0-indexed) |

#### `ChildCompleted`
Emitted when a child agent completes and notifies its parent.

| Field | Type | Description |
|-------|------|-------------|
| `child_id` | `UUID` | ID of the completed child |
| `result` | `str` | Simple result text |
| `child_result` | `dict` | Rich TaskOutcome structure (decisions, artifacts) |

#### `ChildFailed`
Emitted when a child agent fails and notifies its parent.

| Field | Type | Description |
|-------|------|-------------|
| `child_id` | `UUID` | ID of the failed child |
| `reason` | `str` | Error message from the child |

---

### Execution Events

Events tracking worker tool execution and output capture.

#### `CodeGenerationStarted`
Emitted when a WORKER begins execution with a tool (Claude Code, OpenHands, etc.).

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` | Name of the worker tool being used |

#### `ThoughtCaptured`
Emitted to capture worker tool output streams (stdout, thinking, progress).

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` | The captured output text |
| `stream` | `str` | Source stream: "tool", "stdout", "stderr" |
| `output_type` | `str` | Type: "thinking", "progress", "output", "debug" |

#### `PromptSent`
Emitted when a prompt is sent to an LLM or worker tool (for observability).

| Field | Type | Description |
|-------|------|-------------|
| `prompt` | `str` | The full prompt text |
| `prompt_type` | `str` | "complexity_evaluation", "task_decomposition", "worker_execution" |
| `target` | `str` | "llm" or tool name like "claude_code", "openhands" |

---

### Cost Tracking Events

Events for tracking token usage and costs across the system.

#### `TokensConsumed`
Emitted after each LLM call to track token usage and costs.

| Field | Type | Description |
|-------|------|-------------|
| `model` | `str` | Model used (e.g., "gpt-4o", "claude-3-5-sonnet") |
| `prompt_tokens` | `int` | Input tokens consumed |
| `completion_tokens` | `int` | Output tokens generated |
| `total_tokens` | `int` | Total tokens (prompt + completion) |
| `cost_usd` | `float` | Calculated cost in USD |
| `operation` | `str` | "complexity_evaluation", "task_decomposition", "worker_execution" |

#### `WorkerCostRecorded`
Emitted when a worker tool execution incurs cost.

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` | "claude_code", "openhands", "adk" |
| `model` | `str \| None` | Underlying model if known |
| `tokens` | `int \| None` | Total tokens if available |
| `cost_usd` | `float` | Cost in USD (0.0 if unknown) |
| `duration_seconds` | `float` | Execution duration |

#### `LimitEnforced`
Emitted when a system limit constrains agent behavior.

| Field | Type | Description |
|-------|------|-------------|
| `limit_type` | `str` | "depth", "children", "agents" |
| `limit_value` | `int \| float` | The configured limit |
| `attempted_value` | `int \| float` | What was attempted |
| `action_taken` | `str` | "forced_worker_role", "rejected_children" |

---

### Timing/Observability Events

Events for measuring operation and agent execution duration.

#### `OperationStarted`
Emitted when an LLM operation begins (for duration tracking).

| Field | Type | Description |
|-------|------|-------------|
| `operation_type` | `str` | "complexity_evaluation", "task_decomposition", "worker_execution" |

#### `OperationFinished`
Emitted when an LLM operation completes (paired with OperationStarted).

| Field | Type | Description |
|-------|------|-------------|
| `operation_type` | `str` | "complexity_evaluation", "task_decomposition", "worker_execution" |
| `duration_seconds` | `float` | Time elapsed since OperationStarted |

#### `AgentExecutionStarted`
Emitted when an agent begins actual work (after task assignment).

| Field | Type | Description |
|-------|------|-------------|
| `role` | `str` | "boss", "manager", "worker" |
| `depth` | `int` | Depth in the agent hierarchy |

#### `AgentExecutionFinished`
Emitted when an agent completes or fails execution.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `str` | "boss", "manager", "worker" |
| `status` | `str` | "completed" or "failed" |
| `duration_seconds` | `float` | Total execution time |

---

### Shared Context Events

Events for cross-agent state sharing (artifacts, decisions, configuration).

#### `SharedContextCreated`
Emitted once per execution hierarchy when shared context is initialized.

| Field | Type | Description |
|-------|------|-------------|
| `root_id` | `UUID` | Root agent ID (context key) |
| `initial_budget_usd` | `float` | Starting budget limit |
| `config` | `dict` | Initial shared configuration |

#### `ArtifactStored`
Emitted when an agent stores a shareable artifact.

| Field | Type | Description |
|-------|------|-------------|
| `key` | `str` | Artifact path (e.g., "outputs/poc.py") |
| `content_type` | `str` | MIME type |
| `content` | `str \| None` | Inline content (small artifacts) |
| `content_hash` | `str \| None` | SHA256 hash (large artifacts) |
| `stored_by` | `UUID` | Agent that stored the artifact |

#### `DecisionRecorded`
Emitted when an agent records an architectural/design decision.

| Field | Type | Description |
|-------|------|-------------|
| `decision_key` | `str` | Decision identifier (e.g., "architecture.database") |
| `decision_value` | `str` | JSON-serialized decision value |
| `rationale` | `str` | Reasoning for the decision |
| `decided_by` | `UUID` | Agent that made the decision |

#### `ProgressUpdated`
Emitted when an agent updates a progress checkpoint.

| Field | Type | Description |
|-------|------|-------------|
| `checkpoint_key` | `str` | Checkpoint identifier |
| `status` | `str` | "started", "in_progress", "completed", "blocked" |
| `progress_pct` | `float` | Completion percentage (0.0-100.0) |
| `message` | `str` | Progress message |
| `reported_by` | `UUID` | Agent reporting progress |

#### `ConfigOverrideSet`
Emitted when runtime configuration is modified.

| Field | Type | Description |
|-------|------|-------------|
| `config_key` | `str` | Configuration key |
| `config_value` | `str` | JSON-serialized value |
| `set_by` | `UUID` | Agent that set the override |
| `scope` | `str` | "global" or "subtree:{agent_id}" |

---

### Budget Events

Events for tracking budget consumption across the execution hierarchy.

#### `BudgetConsumed`
Emitted for each cost-incurring operation.

| Field | Type | Description |
|-------|------|-------------|
| `consumed_by` | `UUID` | Agent that consumed budget |
| `amount_usd` | `float` | Cost of this operation |
| `operation` | `str` | "llm_call", "worker_execution" |
| `model` | `str \| None` | Model used if applicable |
| `tokens` | `dict` | {"prompt": N, "completion": M} |

#### `BudgetExceeded`
Emitted when budget limit is reached or exceeded.

| Field | Type | Description |
|-------|------|-------------|
| `limit_usd` | `float` | The budget limit |
| `consumed_usd` | `float` | Total consumed amount |
| `triggered_by` | `UUID` | Agent that triggered the limit |

---

### SEC-bench Benchmark Events

Events specific to SEC-bench CVE reproduction benchmarks.

#### `BenchmarkStarted`
Emitted by BOSS when a CVE benchmark run begins.

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | `str` | Benchmark instance (e.g., "gpac.cve-2023-2838") |
| `cve_id` | `str` | CVE identifier (e.g., "cve-2023-2838") |
| `docker_image` | `str` | Pre-built Docker image name |
| `sanitizer` | `str` | "address", "memory", or "undefined" |

#### `BenchmarkStageCompleted`
Emitted by workers when a benchmark stage completes.

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | `str` | Benchmark instance |
| `stage` | `str` | "builder", "exploiter", or "fixer" |
| `success` | `bool` | Whether stage succeeded |
| `details` | `str` | Success message or error description |
| `sanitizer_output` | `str \| None` | Sanitizer output (exploiter/fixer) |
| `duration_seconds` | `float` | Stage execution time |
| `worker_agent_id` | `UUID` | Worker that completed the stage |

#### `BenchmarkCompleted`
Emitted by BOSS when all benchmark stages finish.

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | `str` | Benchmark instance |
| `overall_success` | `bool` | Whether benchmark passed |
| `builder_success` | `bool \| None` | Builder stage result |
| `exploiter_success` | `bool \| None` | Exploiter stage result |
| `fixer_success` | `bool \| None` | Fixer stage result |
| `total_duration_seconds` | `float` | Total benchmark time |
| `total_cost_usd` | `float` | Total cost incurred |

---

## State Machine Transitions

### PENDING Agent
```
PENDING + TaskAssigned → ANALYZING
ANALYZING + ComplexityEvaluated(simple) → role=WORKER, still ANALYZING
ANALYZING + ComplexityEvaluated(complex) → role=MANAGER, still ANALYZING
```

### BOSS/MANAGER Agent
```
ANALYZING + SubtasksDefined + ChildSpawned* + StatusChanged → WAITING
WAITING + ChildCompleted (all children) + WorkCompleted → COMPLETED
```

### WORKER Agent
```
ANALYZING + CodeGenerationStarted → IN_PROGRESS
IN_PROGRESS + ThoughtCaptured* + WorkCompleted → COMPLETED
IN_PROGRESS + WorkFailed → FAILED
```

---

## Key Methods

### Command Methods (emit events)

| Method | Purpose | Events Emitted |
|--------|---------|----------------|
| `assign_task(description)` | Assign task to agent | `TaskAssigned` |
| `apply_complexity_result(...)` | Set role after evaluation | `ComplexityEvaluated` |
| `apply_subtasks_and_spawn_children(...)` | Decompose task | `SubtasksDefined`, `ChildSpawned`*, `StatusChanged` |
| `handle_child_update(child_id, result)` | Record child completion | `ChildCompleted`, maybe `WorkCompleted` |
| `fail_with_reason(reason)` | Mark as failed | `WorkFailed` |

### Query Methods (read state)

| Method | Purpose |
|--------|---------|
| `is_terminal()` | True if COMPLETED or FAILED |
| `is_leaf()` | True if WORKER with no children |
| `load_from_history(events)` | Reconstruct from events |

---

## Context Passing

Context types are organized by data flow direction in `core/domain/values/context/`:

### SpawnPayload (Parent → Child)

Defined in `core/domain/values/context/parent_to_child.py`:

```python
class SpawnPayload(BaseModel):
    """Immutable context passed from parent to child."""
    ancestry: tuple[AncestorSummary, ...]  # Chain of parent tasks
    decisions: tuple[str, ...]              # Decisions made by ancestors
    artifacts: tuple[str, ...]              # Artifacts produced
```

Children receive context via `ChildSpawned.spawn_payload` field.

### HierarchyLimits

Defined in `core/domain/values/context/limits.py`:

```python
class HierarchyLimits(BaseModel):
    """Depth and child limits for the agent hierarchy."""
    current_depth: int
    max_depth: int
    max_children_per_node: int
    max_total_agents: int
```

### TaskOutcome (Child → Parent)

Defined in `core/domain/values/context/child_to_parent.py`:

```python
class TaskOutcome(BaseModel):
    """Result returned from child to parent."""
    child_id: UUID
    result: str
    decisions: tuple[str, ...]
    artifacts: tuple[str, ...]
```

### SiblingView (Sibling → Sibling)

Defined in `core/domain/values/context/sibling_to_sibling.py`:

```python
class SiblingView(BaseModel):
    """Context from completed sibling workers."""
    siblings: tuple[SiblingStatus, ...]
    decisions: tuple[SharedDecision, ...]
```

### Shared Execution Context (Global)

For cross-agent state sharing (artifacts, decisions, budget), see [`context-passing-mechanism.md`](context-passing-mechanism.md).

```python
# One SharedExecutionContext per execution hierarchy (root_id)
context = await shared_context_port.get(root_id)
context.store_artifact(key="poc", content="...", stored_by=agent_id)
context.record_decision(key="framework", value="django", ...)
```
