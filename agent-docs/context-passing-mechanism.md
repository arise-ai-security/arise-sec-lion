# Context Passing Mechanism

This document describes how agents share state and context across the execution hierarchy using the SharedExecutionContext system.

---

## Overview

In a recursive multi-agent system, agents need to share information:
- **Artifacts**: Files, code snippets, analysis results produced by workers
- **Decisions**: Architectural choices, tool selections, design decisions
- **Progress**: Checkpoint status for long-running operations
- **Budget**: Cost tracking and enforcement across the hierarchy

The **SharedExecutionContext** provides an event-sourced, centralized store for cross-agent state sharing within a single execution run.

```
                    ┌─────────────────────────────────────┐
                    │       SharedExecutionContext        │
                    │         (one per root_id)           │
                    ├─────────────────────────────────────┤
                    │  ArtifactStore    │  DecisionLog    │
                    │  ProgressTracker  │  BudgetAccount  │
                    └─────────────────────────────────────┘
                                    ▲
                    ┌───────────────┼───────────────┐
                    │               │               │
                 BOSS           MANAGER          WORKER
                    │               │               │
                    └───────────────┴───────────────┘
                         All agents share access
```

---

## Key Concepts

### One Context Per Execution Hierarchy

Each execution run (starting from a BOSS agent) has exactly one SharedExecutionContext, identified by the `root_id`:

```python
# Derived aggregate ID prevents collision with AgentSession
aggregate_id = uuid5(SHARED_CONTEXT_NAMESPACE, str(root_id))
```

### Event-Sourced State

All state changes are captured as domain events, enabling:
- Complete audit trail of shared decisions and artifacts
- OCC (Optimistic Concurrency Control) for concurrent access
- Replay capability for debugging and recovery

---

## Component Aggregates

SharedExecutionContext is a **facade** composing four specialized aggregates:

### 1. ArtifactStore

Stores shared outputs between agents (files, code, analysis results).

```python
# Store an artifact
context.store_artifact(
    key="vulnerability_report",
    content_type="text/markdown",
    stored_by=worker_agent_id,
    content="## Findings\n- SQL Injection in login.php...",
)

# Retrieve artifact
artifact = context.get_artifact("vulnerability_report")
# Returns: Artifact(key, content_type, content, content_hash, stored_by, metadata)
```

**Events**: `ArtifactStored`

### 2. DecisionLog

Records architectural and design decisions with rationale.

```python
# Record a decision
context.record_decision(
    decision_key="target_framework",
    decision_value="django",
    rationale="Target app uses Django 3.2 based on requirements.txt",
    decided_by=manager_agent_id,
)

# Query decision
decision = context.get_decision("target_framework")
# Returns: Decision(key, value, rationale, decided_by)
```

**Events**: `DecisionRecorded`

### 3. ProgressTracker

Tracks progress checkpoints across the hierarchy.

```python
# Update progress
context.update_progress(
    checkpoint_key="exploit_development",
    status="in_progress",
    progress_pct=60.0,
    message="PoC working, testing payload variations",
    reported_by=worker_agent_id,
)

# Check progress
checkpoint = context.get_progress("exploit_development")
# Returns: ProgressCheckpoint(key, status, progress_pct, message, reported_by)
```

**Events**: `ProgressUpdated`

### 4. BudgetAccount

Tracks cost consumption and enforces budget limits.

```python
# Consume budget (called automatically by LLM adapter)
context.consume_budget(
    agent_id=agent_id,
    amount=0.0023,
    operation="llm_call",
    model="gpt-4",
    tokens={"input": 1500, "output": 200},
)

# Check budget
remaining = context.get_remaining_budget()  # -1 if unlimited
exceeded = context.is_budget_exceeded()
summary = context.get_budget_summary()
# Returns: {initial_budget_usd, consumed_budget_usd, remaining_budget_usd, budget_exceeded}
```

**Events**: `BudgetConsumed`, `BudgetExceeded`

---

## Domain Events

All SharedContext events (defined in `core/domain/events.py`):

| Event | Purpose | Key Fields |
|-------|---------|------------|
| `SharedContextCreated` | Context initialization | `root_id`, `initial_budget_usd`, `config` |
| `ArtifactStored` | Artifact added/updated | `key`, `content_type`, `content`, `stored_by` |
| `DecisionRecorded` | Decision logged | `decision_key`, `decision_value`, `rationale`, `decided_by` |
| `ProgressUpdated` | Progress checkpoint update | `checkpoint_key`, `status`, `progress_pct`, `reported_by` |
| `BudgetConsumed` | Cost recorded | `consumed_by`, `amount_usd`, `operation`, `model`, `tokens` |
| `BudgetExceeded` | Budget limit hit | `limit_usd`, `consumed_usd`, `triggered_by` |
| `ConfigOverrideSet` | Runtime config change | `config_key`, `config_value`, `set_by`, `scope` |

---

## Port Interface

The `SharedContextPort` (`core/ports/shared_context_port.py`) defines the interface:

```python
class SharedContextPort(Protocol):
    async def get_or_create(
        self, root_id: UUID, initial_budget_usd: float, config: dict
    ) -> SharedExecutionContext:
        """Get existing or create new context."""

    async def get(self, root_id: UUID) -> SharedExecutionContext | None:
        """Get context by root ID."""

    async def save(self, context: SharedExecutionContext, expected_version: int) -> None:
        """Save with OCC (raises ConcurrencyError on conflict)."""

    async def exists(self, root_id: UUID) -> bool:
        """Check if context exists."""
```

### Interface Segregation

For read-only access, use `ContextReaderPort`:

```python
class ContextReaderPort(Protocol):
    async def get_shared_context(self, root_id: UUID) -> SharedExecutionContext | None
    async def get_artifact(self, root_id: UUID, key: str) -> Artifact | None
    async def get_decision(self, root_id: UUID, key: str) -> Decision | None
```

---

## Usage Flow

### 1. Context Creation (Boss Agent)

When a BOSS agent is created, SharedExecutionContext is initialized:

```python
# execution_service.py
async def create_boss_agent(self, task_description: str) -> UUID:
    root_id = uuid4()

    # Create shared context for this run
    shared_context = await self._shared_context_port.get_or_create(
        root_id=root_id,
        config={},
    )
    await self._shared_context_port.save(shared_context, expected_version=0)

    # Create boss agent...
```

### 2. Worker Updates Context

When workers complete, they can record decisions and artifacts:

```python
# execution_service.py
async def _process_worker_context_updates(self, agent: AgentSession) -> None:
    # Parse structured output from worker result
    parsed = parse_context_update(agent.result)

    root_id = self._context_registry.get_root_id(agent.agent_id)
    context = await self._shared_context_port.get(root_id)
    current_version = context.version

    # Record decisions
    for decision in parsed.decisions:
        context.record_decision(
            decision_key=decision.key,
            decision_value=decision.value,
            rationale=decision.rationale,
            decided_by=agent.agent_id,
        )

    # Store artifacts
    for output in parsed.outputs:
        context.store_artifact(
            key=output.key,
            content_type="text/plain",
            stored_by=agent.agent_id,
            content=output.description,
        )

    # Persist with OCC
    if context.events:
        await self._shared_context_port.save(context, expected_version=current_version)
        context.mark_changes_as_committed()
```

### 3. Sibling Context for Sequential Workers

Workers receive context from completed siblings via `SiblingContextPort`:

```python
# execution_service.py
async def _dispatch_agent_action(self, agent: AgentSession) -> None:
    if agent.role == AgentRole.WORKER:
        # Build context from completed siblings
        sibling_context = await self._sibling_context_port.build_context(
            agent_id=agent.agent_id,
            parent_id=agent.parent_id,
            root_id=root_id,
        )

        await self._orchestrator.execute_task(
            agent,
            sibling_context=sibling_context,  # Passed to worker prompt
            ...
        )
```

---

## Concurrency Control

### Optimistic Concurrency Control (OCC)

SharedContext uses the same OCC pattern as AgentSession:

1. Load context (get current version)
2. Make changes (record events)
3. Save with expected_version
4. On conflict: `ConcurrencyError` raised, caller retries

```python
# OCC pattern
context = await shared_context_port.get(root_id)
current_version = context.version  # e.g., 5

context.record_decision(...)  # Adds event to uncommitted list

try:
    await shared_context_port.save(context, expected_version=current_version)
except ConcurrencyError:
    # Another process updated first - reload and retry
    pass
```

### Database Constraint

OCC is enforced by PostgreSQL unique constraint:

```sql
CONSTRAINT unique_aggregate_sequence UNIQUE (aggregate_id, sequence_number)
```

### When Locking is Needed

OCC works well for low-contention scenarios. For high-contention operations (e.g., task deduplication where multiple managers may try to register the same task), consider:

- **PostgreSQL Advisory Locks**: Database-level distributed locks
- **Retry with backoff**: Exponential backoff on ConcurrencyError

---

## Key Files

| File | Purpose |
|------|---------|
| `core/domain/shared_context.py` | Domain model (SharedExecutionContext, aggregates) |
| `core/domain/events.py` | SharedContext domain events |
| `core/ports/shared_context_port.py` | Port interface |
| `infrastructure/adapters/shared_context_adapter.py` | PostgreSQL adapter |
| `core/application/execution_service.py:445` | Worker context update processing |
| `core/application/services/sibling_context_builder.py` | Builds sibling context |

---

## Value Objects

Immutable data objects used by SharedContext:

```python
@dataclass(frozen=True)
class Artifact:
    key: str
    content_type: str
    content: str | None
    content_hash: str | None
    stored_by: UUID
    metadata: dict[str, Any]

@dataclass(frozen=True)
class Decision:
    key: str
    value: str
    rationale: str
    decided_by: UUID

@dataclass(frozen=True)
class ProgressCheckpoint:
    key: str
    status: str
    progress_pct: float
    message: str
    reported_by: UUID
```

---

## Quick Reference

| Operation | Method | Event |
|-----------|--------|-------|
| Store artifact | `context.store_artifact(...)` | `ArtifactStored` |
| Record decision | `context.record_decision(...)` | `DecisionRecorded` |
| Update progress | `context.update_progress(...)` | `ProgressUpdated` |
| Consume budget | `context.consume_budget(...)` | `BudgetConsumed` |
| Set config | `context.set_config_override(...)` | `ConfigOverrideSet` |
