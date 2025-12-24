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

The event store uses segregated interfaces (ISP):

| Interface | Purpose | Methods |
|-----------|---------|---------|
| `EventStoreReadPort` | Read-only queries | `get_events()`, `get_all_aggregate_ids()`, `get_all_events_grouped()` |
| `EventStoreWritePort` | Append with OCC | `append()` |
| `EventStoreConnectPort` | Connection lifecycle | `connect()`, `disconnect()`, `initialize_schema()` |
| `EventStorePort` | Composite (all above) | Full implementation interface |

**Files:**
- `core/ports/event_store_port.py` - Port interfaces (segregated + composite)
- `infrastructure/adapters/postgres_event_store.py` - PostgreSQL implementation
- `infrastructure/sql/create_events_table.sql` - Schema definition

**Usage:** Read-only clients (projections, API endpoints) depend on `EventStoreReadPort`.

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

### Core Agent Events

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

### Budget Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `BudgetAllocated` | Budget assigned | `amount`, `source` |
| `BudgetAdjusted` | Budget modified | `adjustment`, `reason`, `new_balance` |
| `BudgetRecollected` | Parent recollects | `child_id`, `remaining_budget`, `ratio_applied` |
| `ChildFailed` | Child fails task | `child_id`, `failure_reason`, `budget_at_failure` |
| `AllChildrenFailed` | All siblings fail | `subtask_description`, `child_ids`, `penalty_ratio` |

### Task Queue Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `TaskEnqueued` | Subtask added | `subtask` |
| `TaskDequeued` | Subtask removed | `subtask` |
| `SubtaskRetried` | Retry with context | `original_subtask`, `revised_subtask`, `retry_count` |

### Termination Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `AgentTerminated` | Agent terminated | `reason`, `final_budget`, `cascade` |
| `SubtreeAborted` | Subtree cancelled | `subtask_id`, `child_ids_to_terminate`, `reason` |

### Multi-Model Strategy Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `SubordinatesSpawned` | Multiple agents spawned | `subtask`, `total_budget_allocated`, `subordinate_configs` |
| `FirstSuccessRecorded` | First subordinate wins | `winning_child_id`, `sibling_ids_terminated` |
| `AllSubordinatesFailed` | All failed | `subtask`, `failed_child_ids`, `failure_reasons` |

### Verification Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `VerificationInjected` | Verification triggered | `target_subtask`, `target_child_id`, `injection_reason` |
| `VerifierSpawned` | Verifier created | `verifier_id`, `target_subtask`, `verifier_config` |
| `VerificationCompleted` | Verifier finished | `verification_passed`, `verification_report`, `issues_found` |
| `TaskReinjected` | Failed verification | `subtask`, `verification_context`, `retry_count` |
| `VerificationHeuristicEvaluated` | Heuristic checked | `complexity_score`, `random_roll`, `verification_decided` |

### Cost Tracking Events

| Event | Trigger | Key Fields |
|-------|---------|------------|
| `TokensConsumed` | LLM call completed | `model`, `prompt_tokens`, `completion_tokens`, `cost_usd` |
| `WorkerCostRecorded` | Worker tool used | `tool_name`, `cost_usd`, `duration_seconds` |
| `BudgetExceeded` | Budget limit hit | `budget_limit_usd`, `current_total_usd` |
| `LimitEnforced` | System limit enforced | `limit_type`, `limit_value`, `action_taken` |

All events defined in: `core/domain/events.py`
