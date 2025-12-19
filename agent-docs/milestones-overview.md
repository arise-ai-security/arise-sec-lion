# Multi-Agent Enhancement Plan: Milestones Overview

## Status Summary

| Milestone | Status | Description |
|-----------|--------|-------------|
| 1. Cost Tracking | ✅ Complete | Token usage, cost calculation, budget enforcement |
| 2. System Limits | ✅ Complete | max_depth, max_children, concurrency limits (-1 = unlimited) |
| 3. Failure & Retry | 🔲 Not Started | Retry with adjusted params, parent notification |
| 4. Context Passing | 🔲 Not Started | Bidirectional context flow in hierarchy |
| 5. Shared State | 🔲 Not Started | CQRS projections for system-wide queries |
| 6. Hyperparameter Tuning | 🔲 Not Started | Parent-controlled retry strategies |

---

## Completed Work

### Milestone 1: Cost Tracking ✅
- `TokensConsumed`, `WorkerCostRecorded`, `BudgetExceeded` events
- `LLMResponse` and `LLMUsage` value objects
- `query_with_usage()` method in `LLMPort`
- `LiteLLMAdapter` extracts usage and calculates cost
- Budget tracking in `AgentExecutionService`

### Milestone 2: System Limits ✅
- `SystemLimitsConfig` with `is_*_limited()` helpers
- `-1` as "unlimited" sentinel value
- `ExecutionContext` immutable value object
- `LimitEnforced` event for audit trail
- Worker concurrency semaphore
- Depth and children limits enforced in `evaluate_task()`

---

## Remaining Work

### Milestone 3: Failure & Retry
See: [milestone-3-failure-retry.md](milestone-3-failure-retry.md)

Key additions:
- `ChildFailed`, `RetryScheduled`, `RetryStarted` events
- `RetryPolicy` model with exponential backoff
- `FailureCategorizer` for pattern matching
- Parent notification on child failure

### Milestone 4: Context Passing
See: [milestone-4-context-passing.md](milestone-4-context-passing.md)

Key additions:
- Extend `ChildSpawned` with `parent_context`
- Extend `ChildCompleted` with `artifacts`, `context_updates`
- `get_context_for_child()` method
- Parent context in prompts

### Milestone 5: Shared State
See: [milestone-5-shared-state.md](milestone-5-shared-state.md)

Key additions:
- `SystemStateProjection` for queryable state
- `ProjectionPort` protocol
- `ContextBuilder` service
- Sibling awareness for workers

### Milestone 6: Hyperparameter Tuning
See: [milestone-6-hyperparameter-tuning.md](milestone-6-hyperparameter-tuning.md)

Key additions:
- `AdjustmentStrategy` protocol
- `DefaultAdjustmentStrategy` implementation
- `decide_child_retry()` method
- Configurable fallback models and timeout multipliers

---

## Key Design Decisions

1. **-1 as unlimited**: Following Unix conventions, -1 disables a limit
2. **Event sourcing**: All state changes via domain events
3. **Immutable contexts**: `ExecutionContext` uses `@dataclass(frozen=True)`
4. **CQRS**: Projections for read-only queries, events for writes
5. **Fail fast**: No default values for critical config (require explicit config)
6. **Retry with backoff**: Exponential delay, category-specific adjustments

---

## Testing Guidelines

Each milestone should include:
1. Unit tests for new domain events
2. Unit tests for new value objects/models
3. Integration tests for service changes
4. Update mocks for `query_with_usage()` (returns `LLMResponse`)
5. Account for `TokensConsumed` events in event counts
