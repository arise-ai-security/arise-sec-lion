# Domain Model

## AgentSession Aggregate

`core/domain/aggregates/agent_session.py` — event-sourced aggregate root. All state derived from replaying events via `AgentSession.load_from_history(events)`.

Key properties: `agent_id`, `role` (AgentRole), `status` (AgentStatus), `parent_id`, `child_ids`, `child_reports` (dict[UUID, Report]), `config` (AgentConfig), `version`, `briefing`, `hierarchy_limits`, `retry_count`, `sibling_index`, `success_criteria`.

## Roles and Status

Roles (`core/domain/values/enums.py`):

| Role | Purpose |
|------|---------|
| `BOSS` | Root agent, decomposes initial task |
| `MANAGER` | Complex subtask handler, decomposes further |
| `WORKER` | Simple task executor, runs worker tool |
| `PENDING` | Newly spawned, awaiting complexity evaluation |

Status lifecycle:

```
PENDING → ANALYZING ──┬── IN_PROGRESS → COMPLETED
                      ├── WAITING ────→ COMPLETED
                      └── FAILED
```

| Status | Meaning |
|--------|---------|
| `PENDING` | Created, no task assigned |
| `ANALYZING` | Processing (LLM call or execution) |
| `IN_PROGRESS` | Worker actively executing |
| `WAITING` | Manager/Boss waiting for children |
| `COMPLETED` | Finished successfully |
| `FAILED` | Finished with error |
| `BLOCKED` | Blocked on dependency |

## NodeMessage (Context Passing)

`core/domain/values/node_message.py` — discriminated union on `direction` field:

| Type | Direction | Purpose |
|------|-----------|---------|
| `Briefing` | ↓ down | Parent → child at spawn: `ancestry`, `parent_task`, `decisions` |
| `Report` | ↑ up | Child → parent on completion: `result`, `artifacts`, `decisions`, `execution_summary` |
| `Handoff` | ↔ lateral | Sibling context: `siblings` (PeerStatus), `shared_decisions`, computed counts |

Helper: `build_briefing(agent, parent_briefing)` constructs the ancestry chain.

## Domain Events

All in `core/domain/events/events.py` — frozen Pydantic `BaseModel` subclasses of `DomainEvent`.

Base fields: `event_id`, `aggregate_id`, `sequence_number`, `occurred_at`, `metadata`.

### Lifecycle
- `AgentCreated` — role, parent_id, config, sibling_index, briefing
- `TaskAssigned` — task_description, constraints
- `StatusChanged` — old_status, new_status, reason
- `WorkCompleted` — result
- `WorkFailed` — reason

### Decomposition
- `ComplexityEvaluated` — complexity (simple/complex), determined_role, reasoning
- `SubtasksDefined` — subtasks (list[Subtask])
- `ChildSpawned` — child_id, child_role, subtask, child_config, briefing, sibling_index
- `ChildCompleted` — child_id, result, child_report (Report)
- `ChildFailed` — child_id, reason

### Execution
- `CodeGenerationStarted` — tool_name
- `ThoughtCaptured` — content, stream, output_type
- `VerificationFailed` — stage, reason, details

### Infeasibility / Retry
- `DecisionInfeasible` — reason, constraint_details
- `RedecompositionTriggered` — reason, original_subtask_count
- `RetryScheduled` — retry_count, escalated_model, reason

### Probing
- `ProbeStarted` — probe_type
- `ProbeCompleted` — probe_type, result

### Cost / Limits
- `TokensConsumed` — model, prompt_tokens, completion_tokens, total_tokens, cost_usd, operation
- `WorkerCostRecorded` — tool_name, model, tokens, cost_usd, duration_seconds
- `LimitEnforced` — limit_type, limit_value, attempted_value, action_taken

### Observability
- `PromptSent` — prompt, prompt_type, target
- `OperationStarted` / `OperationFinished` — operation_type, duration_seconds
- `AgentExecutionStarted` / `AgentExecutionFinished` — role, depth, status, duration_seconds
- `RunStarted` / `RunCompleted` — run-level lifecycle

### Shared Context
- `SharedContextCreated` — root_id
- `ArtifactStored` — key, content_type, content, stored_by
- `DecisionRecorded` — decision_key, decision_value, rationale, decided_by

## State Machine

### PENDING Agent
```
PENDING + TaskAssigned → ANALYZING
ANALYZING + ComplexityEvaluated(simple) → role=WORKER
ANALYZING + ComplexityEvaluated(complex) → role=MANAGER
```

### BOSS/MANAGER
```
ANALYZING + SubtasksDefined + ChildSpawned* → WAITING
WAITING + all ChildCompleted + WorkCompleted → COMPLETED
```

### WORKER
```
ANALYZING + CodeGenerationStarted → IN_PROGRESS
IN_PROGRESS + WorkCompleted → COMPLETED
IN_PROGRESS + WorkFailed → FAILED
FAILED + RetryScheduled → ANALYZING (retry with escalated model)
```

## Key Methods

| Method | Events Emitted |
|--------|----------------|
| `assign_task(description)` | `TaskAssigned` |
| `apply_complexity_result(...)` | `ComplexityEvaluated` |
| `apply_subtasks_and_spawn_children(...)` | `SubtasksDefined`, `ChildSpawned`*, `StatusChanged` |
| `handle_child_update(child_id, report)` | `ChildCompleted`, maybe `WorkCompleted` |
| `handle_child_failure(child_id, reason)` | `ChildFailed`, maybe `WorkFailed` |
| `start_worker_execution(tool)` | `CodeGenerationStarted` |
| `apply_worker_event(event)` | Streams worker events |
| `mark_infeasible(reason, details)` | `DecisionInfeasible` |
| `trigger_redecomposition(reason)` | `RedecompositionTriggered` |
| `schedule_retry(model, reason)` | `RetryScheduled` |
| `fail_with_reason(reason)` | `WorkFailed` |
