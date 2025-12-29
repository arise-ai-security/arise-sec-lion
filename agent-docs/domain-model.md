# Domain Model

This document describes the core domain entities, events, and state machine that govern agent behavior.

---

## AgentSession Aggregate

The `AgentSession` class (`core/domain/model.py:35`) is the aggregate root. All agent state is derived by replaying events.

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

Defined in `core/domain/enums.py`:

| Role | Purpose | Next Action |
|------|---------|-------------|
| `BOSS` | Root agent, receives initial task | Decomposes into subtasks |
| `MANAGER` | Complex subtask handler | Decomposes further |
| `WORKER` | Simple task executor | Runs worker tool (Claude Code) |
| `PENDING` | Newly spawned, awaiting evaluation | LLM evaluates complexity |

---

## Agent Status (Lifecycle)

Defined in `core/domain/enums.py`:

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

All events in `core/domain/events.py` are immutable (frozen dataclasses).

### Lifecycle Events

| Event | Emitted When | Key Fields |
|-------|--------------|------------|
| `AgentCreated` | `AgentSession.create()` | `role`, `parent_id`, `config` |
| `TaskAssigned` | `agent.assign_task()` | `task_description` |
| `StatusChanged` | Status transitions | `old_status`, `new_status`, `reason` |
| `WorkCompleted` | Task finished successfully | `result` |
| `WorkFailed` | Task failed | `reason` |

### Decomposition Events

| Event | Emitted When | Key Fields |
|-------|--------------|------------|
| `ComplexityEvaluated` | PENDING role determined | `complexity`, `determined_role`, `reasoning` |
| `SubtasksDefined` | Task decomposed | `subtasks` (list) |
| `ChildSpawned` | Child agent created | `child_id`, `child_role`, `subtask` |
| `ChildCompleted` | Child finished | `child_id`, `result` |

### Execution Events

| Event | Emitted When | Key Fields |
|-------|--------------|------------|
| `CodeGenerationStarted` | Worker begins | `tool_name` |
| `ThoughtCaptured` | Worker outputs text | `content`, `output_type` |
| `TokensConsumed` | LLM call completed | `model`, `tokens`, `cost_usd` |

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

### Parent Context (Hierarchical)

Defined in `core/domain/context.py`:

```python
@dataclass(frozen=True)
class ParentContext:
    """Immutable context passed from parent to child."""
    ancestry: tuple[AncestorInfo, ...]  # Chain of parent tasks
    decisions: tuple[str, ...]          # Decisions made by ancestors
    artifacts: tuple[str, ...]          # Artifacts produced
    execution_limits: ExecutionLimits   # Depth/child limits
```

Children receive context via `ChildSpawned.parent_context` field.

### Shared Execution Context (Global)

For cross-agent state sharing (artifacts, decisions, budget), see [`context-passing-mechanism.md`](context-passing-mechanism.md).

```python
# One SharedExecutionContext per execution hierarchy (root_id)
context = await shared_context_port.get(root_id)
context.store_artifact(key="poc", content="...", stored_by=agent_id)
context.record_decision(key="framework", value="django", ...)
```
