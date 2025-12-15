# Event Sourcing & CQRS

> **Sources:** Greg Young (2010), Martin Fowler
> **References:**
> - [Martin Fowler - Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)
> - [Greg Young - CQRS Documents](https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf)

## Event Sourcing Principles

**Definition:** "Capture all changes to an application state as a sequence of events." (Martin Fowler)

| Principle | Description |
|-----------|-------------|
| State from Events | State is derived by replaying events, never stored directly |
| Immutable Events | Events are facts that have occurred, never modified |
| Append-Only | Event store only appends, never updates/deletes |
| All Changes = Events | Every state change MUST produce an event |

## Implementation in This Project

### State Reconstruction

State is **always** reconstructed from events:

- `core/domain/model.py:85` - `AgentSession.load_from_history()` method
- `core/domain/model.py:142` - `_apply()` singledispatchmethod (ONLY place state mutates)

### Event Store

- `core/ports/event_store_port.py:15` - Port interface
- `infrastructure/adapters/postgres_event_store.py:25` - PostgreSQL implementation
- `infrastructure/sql/create_events_table.sql:1` - Schema definition

## Optimistic Concurrency Control (OCC)

**All DB writes require `expected_version` parameter.**

OCC prevents lost updates when multiple processes modify the same aggregate:

1. Load aggregate → note current `version`
2. Apply changes → create new events
3. Append events with `expected_version=current_version`
4. If version mismatch → `ConcurrencyError` → retry

### OCC Implementation

- `core/domain/exceptions.py:8` - `ConcurrencyError` exception
- `infrastructure/adapters/postgres_event_store.py:78` - Version check on append
- `core/application/execution_service.py:112` - Retry loop on conflict

## CQRS (Command Query Responsibility Segregation)

**Definition:** "Use a different model to update information than the model you use to read information."

| Side | Purpose | Example Methods |
|------|---------|-----------------|
| Command | Mutate state, produce events | `assign_task()`, `evaluate_task()`, `execute_task()` |
| Query | Read state, return DTOs | `get_agent_result()`, `get_system_statistics()` |

### Query Side (Projections)

- `core/application/projections/` - Projection pipeline system
- `core/application/projections/pipeline.py:25` - `ProjectionPipelineBuilder`
- `core/application/projections/impl/summary.py:18` - Summary projection

## Failures as Events

> "If a failure affects aggregate state, it should be an event, not an exception." - Greg Young

LLM failures publish `WorkFailed` event because:
- Failure changes state: `ANALYZING` → `FAILED`
- Parent agents need failure notification
- Event replay must include failures
- Enables failure analytics

### Failure Event

- `core/domain/events.py:95` - `WorkFailed` event definition
- `core/domain/model.py:298` - `fail_with_reason()` method

## Domain Events Reference

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `AgentCreated` | New agent spawned | `role`, `parent_id`, `config` |
| `TaskAssigned` | Task given to agent | `task_description` |
| `StatusChanged` | State transition | `old_status`, `new_status` |
| `ComplexityEvaluated` | PENDING evaluates | `complexity`, `determined_role` |
| `SubtasksDefined` | MANAGER decomposes | `subtasks: list[Subtask]` |
| `ChildSpawned` | Parent creates child | `child_id`, `child_role`, `subtask` |
| `ThoughtCaptured` | Worker tool output | `content`, `stream` |
| `WorkCompleted` | Success | `result` |
| `WorkFailed` | Failure | `reason` |

All events defined in: `core/domain/events.py`
