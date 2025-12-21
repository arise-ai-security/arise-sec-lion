# Context Passing System - Implementation Plan

## Branch

```bash
git checkout -b feat/hierarchical-context-passing
```

## Overview

A comprehensive context passing system enabling:
- **Parent ↔ Child**: Bidirectional data flow via events
- **Direct Access**: Shared context store accessible by all nodes (keyed by root_id)
- **Concurrency Safe**: Event-sourced with OCC
- **SOLID Compliant**: Extensible, testable, loosely coupled
- **Budget as Subsystem**: Cost tracking integrated into shared context (event-sourced)

### Budget Migration

The current in-memory budget tracking in `ExecutionService` has been **removed** from the codebase. Budget will be re-implemented as part of this context passing system with proper event sourcing:

- **Before**: In-memory tracking with race condition risks
- **After**: Event-sourced via `SharedExecutionContext` with OCC guarantees

Budget flows through the system as:
1. `SharedContextCreated` initializes budget limit
2. `BudgetConsumed` events track each cost
3. `BudgetExceeded` triggers halt when limit reached
4. `ParentContext.execution_limits.remaining_budget_usd` passes budget to children
5. `ChildResult.execution_summary.cost_usd` reports consumption back to parent

---

## Architecture Decision

**Two Complementary Mechanisms**:

1. **Event-Based Context (Parent ↔ Child)**: Replace `ChildSpawned` and `ChildCompleted` events with rich context-aware versions.

2. **Shared Context Store (Direct Access)**: New `SharedExecutionContext` aggregate with its own event stream. All nodes can read/write via `root_id`.

**Consistency Model**: Event-sourced. All changes stored as immutable events, state rebuilt by replay. Uses existing OCC pattern.

**Breaking Changes Allowed**: No backward compatibility required. Can freely rename, restructure, and change function signatures.

---

## Phase 1: Value Objects

### File: `core/domain/context.py` (NEW)

```python
@dataclass(frozen=True, slots=True)
class AncestorInfo:
    """Lightweight ancestor summary."""
    agent_id: str
    role: str
    task_summary: str  # First 100 chars

@dataclass(frozen=True, slots=True)
class ParentContext:
    """Context passed from parent to child."""
    parent_task: str
    parent_role: str
    depth: int
    ancestry: tuple[AncestorInfo, ...]
    decisions: tuple[str, ...]
    constraints: dict[str, Any]
    execution_limits: dict[str, Any]

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict) -> "ParentContext": ...

@dataclass(frozen=True, slots=True)
class ChildResult:
    """Structured result from child to parent."""
    result_text: str
    artifacts: tuple[str, ...]
    decisions: tuple[str, ...]
    context_updates: dict[str, Any]
    execution_summary: dict[str, Any]
```

---

## Phase 2: Replace Parent-Child Events

### File: `core/domain/events.py`

**Replace ChildSpawned** with context-aware version:
```python
class ChildSpawned(DomainEvent):
    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]
    parent_context: ParentContext  # Rich context object
```

**Replace ChildCompleted** with structured result:
```python
class ChildCompleted(DomainEvent):
    child_id: UUID
    child_result: ChildResult  # Structured result object
```

---

## Phase 3: Shared Context Events

### File: `core/domain/events.py` (ADD)

```python
# Shared Context Events
class SharedContextCreated(DomainEvent):
    root_id: UUID
    initial_budget_usd: float
    config: dict[str, Any] = Field(default_factory=dict)

class ArtifactStored(DomainEvent):
    key: str                    # e.g., "outputs/analysis.json"
    content_type: str           # MIME type
    content: str | None = None  # Small content inline
    content_hash: str | None    # SHA256 for large content
    stored_by: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)

class DecisionRecorded(DomainEvent):
    decision_key: str           # e.g., "architecture.database"
    decision_value: str         # JSON-serialized
    rationale: str
    decided_by: UUID

class ProgressUpdated(DomainEvent):
    checkpoint_key: str
    status: str                 # started, in_progress, completed, blocked
    progress_pct: float = 0.0
    message: str = ""
    reported_by: UUID

class ConfigOverrideSet(DomainEvent):
    config_key: str
    config_value: str           # JSON-serialized
    set_by: UUID
    scope: str = "global"       # global | subtree:{agent_id}

# Budget Events (part of shared context)
class BudgetConsumed(DomainEvent):
    """Track cost consumption - event-sourced for OCC."""
    consumed_by: UUID           # Agent that consumed budget
    amount_usd: float           # Cost of this operation
    operation: str              # "llm_call", "worker_execution"
    model: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)  # {"prompt": N, "completion": M}

class BudgetExceeded(DomainEvent):
    """Budget limit reached - triggers halt."""
    limit_usd: float
    consumed_usd: float
    triggered_by: UUID
```

---

## Phase 4: Shared Context Aggregate

### File: `core/domain/shared_context.py` (NEW)

```python
class SharedExecutionContext:
    """Event-sourced aggregate for shared state.

    One instance per execution hierarchy (keyed by root_id).
    Provides: artifacts, decisions, progress, config overrides.
    """

    def __init__(self, root_id: UUID) -> None: ...

    @classmethod
    def create(cls, root_id: UUID, config: dict | None = None) -> "SharedExecutionContext": ...

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "SharedExecutionContext": ...

    # Commands (produce events)
    def store_artifact(self, key, content_type, stored_by, content=None, **metadata): ...
    def record_decision(self, decision_key, decision_value, rationale, decided_by): ...
    def update_progress(self, checkpoint_key, status, reported_by, progress_pct=0.0): ...
    def set_config_override(self, config_key, config_value, set_by, scope="global"): ...

    # Queries (read state)
    def get_artifact(self, key) -> Artifact | None: ...
    def get_decision(self, key) -> Decision | None: ...
    def get_progress(self, checkpoint_key) -> ProgressCheckpoint | None: ...
    def get_config_override(self, key) -> ConfigOverride | None: ...

    # Event sourcing infrastructure
    @property
    def events(self) -> list[DomainEvent]: ...
    def mark_changes_as_committed(self) -> None: ...
```

**State Fields**:
```python
self.root_id: UUID
self.artifacts: dict[str, Artifact] = {}
self.decisions: dict[str, Decision] = {}
self.progress: dict[str, ProgressCheckpoint] = {}
self.config_overrides: dict[str, ConfigOverride] = {}
self.version: int = 0

# Budget state (rebuilt from BudgetConsumed events)
self.initial_budget_usd: float = 0.0
self.consumed_budget_usd: float = 0.0
self.budget_exceeded: bool = False
```

**Budget Methods**:
```python
def consume_budget(self, agent_id: UUID, amount: float, operation: str, **kwargs) -> None:
    """Record budget consumption. Emits BudgetConsumed event."""

def get_remaining_budget(self) -> float:
    """Return initial_budget - consumed_budget."""

def is_budget_exceeded(self) -> bool:
    """Check if consumed >= initial budget."""
```

---

## Phase 5: Port Interfaces (SOLID)

### File: `core/ports/context_reader_port.py` (NEW)

```python
class ContextReaderPort(Protocol):
    """Read-only context access (Interface Segregation)."""

    async def get_shared_context(self, root_id: UUID) -> SharedExecutionContext | None: ...
    async def get_artifact(self, root_id: UUID, key: str) -> Artifact | None: ...
    async def get_decision(self, root_id: UUID, key: str) -> Decision | None: ...
```

### File: `core/ports/context_writer_port.py` (NEW)

```python
class ContextWriterPort(Protocol):
    """Write-only context access (Interface Segregation)."""

    async def save_shared_context(self, context: SharedExecutionContext, expected_version: int) -> None: ...
    async def store_artifact(self, root_id: UUID, key: str, content: bytes) -> str: ...
```

### File: `core/ports/shared_context_port.py` (NEW)

```python
class SharedContextPort(Protocol):
    """Combined interface for infrastructure adapters."""

    async def get_or_create(self, root_id: UUID, config: dict | None = None) -> SharedExecutionContext: ...
    async def get(self, root_id: UUID) -> SharedExecutionContext | None: ...
    async def save(self, context: SharedExecutionContext, expected_version: int) -> None: ...
```

---

## Phase 6: AgentSession Updates

### File: `core/domain/model.py`

**New State Fields** (in `_initialize_defaults`):
```python
self.parent_context: ParentContext | None = None
self.local_decisions: list[str] = []
self.local_artifacts: list[str] = []
```

**New Methods**:
```python
def get_context_for_child(self) -> dict[str, Any]:
    """Build context to pass to child via ChildSpawned."""
    ancestry = list(self.parent_context.ancestry) if self.parent_context else []
    ancestry.append(AncestorInfo.from_agent(self))
    return {
        "parent_task": self.task_description,
        "parent_role": self.role.value,
        "depth": self.execution_context.current_depth if self.execution_context else 0,
        "ancestry": [a.__dict__ for a in ancestry],
        "decisions": self.local_decisions,
        "constraints": self._get_inherited_constraints(),
        "execution_limits": self._get_execution_limits(),
    }

def record_decision(self, decision: str) -> None: ...
def record_artifact(self, artifact: str) -> None: ...
```

**Modified evaluate_task** (line ~254):
```python
# When spawning children, include parent context
parent_context = self.get_context_for_child()

child_event = ChildSpawned(
    aggregate_id=self.session_id,
    sequence_number=self._next_sequence(),
    child_id=child_id,
    child_role=child_role,
    subtask=subtask,
    child_config=subtask.config,
    parent_context=parent_context,  # NEW
)
```

---

## Phase 7: ExecutionContext Update

### File: `core/domain/execution_context.py`

**Add root_id field**:
```python
@dataclass(frozen=True, slots=True)
class ExecutionContext:
    current_depth: int
    max_depth: int
    max_children_per_node: int
    remaining_budget_usd: float
    max_retries: int
    root_id: UUID  # NEW: Reference to shared context
```

---

## Phase 8: ExecutionService Integration

### File: `core/application/execution_service.py`

**Constructor**:
```python
def __init__(
    self,
    event_store: EventStorePort,
    shared_context_port: SharedContextPort,  # NEW
    llm_port: LLMPort,
    worker_tool_port: WorkerToolPort,
    ...
) -> None:
    self.shared_context_port = shared_context_port
```

**create_boss_agent** (line ~436):
```python
async def create_boss_agent(self, task_description: str) -> UUID:
    root_id = uuid4()

    # Create shared context for this execution
    shared_context = await self.shared_context_port.get_or_create(root_id)
    await self._persist_shared_context(shared_context)

    # Create root execution context with root_id
    root_context = ExecutionContext.create_root(
        max_depth=self.system_limits.max_depth,
        max_children_per_node=self.system_limits.max_children_per_node,
        budget_usd=self.budget_config.max_total_cost_usd,
        max_retries=self.max_retries,
        root_id=root_id,  # NEW
    )
    ...
```

**_handle_child_spawning** (line ~300):
```python
# Pass parent_context from event to child config
child_config = child_event.child_config.copy()
child_config["parent_context"] = child_event.parent_context
```

**_handle_parent_notification** (line ~342):
```python
# Pass structured result back to parent
parent.handle_child_update(
    child_id=agent.session_id,
    result=agent.result or "",
    artifacts=agent.local_artifacts,
    decisions=agent.local_decisions,
    context_updates={...},
    execution_summary={...},
)
```

---

## Phase 9: Infrastructure Adapter

### File: `infrastructure/adapters/shared_context_adapter.py` (NEW)

```python
class PostgresSharedContextAdapter(SharedContextPort):
    """Shared context backed by PostgreSQL event store."""

    def __init__(self, event_store: PostgresEventStore) -> None:
        self._event_store = event_store

    async def get_or_create(self, root_id: UUID, config: dict | None = None) -> SharedExecutionContext:
        events = await self._event_store.get_events(root_id)
        if events:
            return SharedExecutionContext.load_from_history(events)
        return SharedExecutionContext.create(root_id, config)

    async def save(self, context: SharedExecutionContext, expected_version: int) -> None:
        current_version = expected_version
        for event in context.events:
            await self._event_store.append(event, expected_version=current_version)
            current_version += 1
        context.mark_changes_as_committed()
```

**Update Event Registry** (`postgres_event_store.py`):
```python
EVENT_TYPE_REGISTRY: dict[str, type[DomainEvent]] = {
    # ... existing events ...
    "SharedContextCreated": SharedContextCreated,
    "ArtifactStored": ArtifactStored,
    "DecisionRecorded": DecisionRecorded,
    "ProgressUpdated": ProgressUpdated,
    "ConfigOverrideSet": ConfigOverrideSet,
}
```

---

## Phase 10: Prompt Template Updates

### File: `prompts/tasks/complexity_evaluation.j2`

```jinja2
{% if parent_context %}
<PARENT_CONTEXT>
Parent Task: {{ parent_context.parent_task }}
Depth: {{ parent_context.depth }}
{% if parent_context.ancestry %}
Ancestry:
{% for ancestor in parent_context.ancestry %}
  - [{{ ancestor.role }}] {{ ancestor.task_summary }}
{% endfor %}
{% endif %}
{% if parent_context.decisions %}
Parent Decisions:
{% for decision in parent_context.decisions %}
  - {{ decision }}
{% endfor %}
{% endif %}
</PARENT_CONTEXT>
{% endif %}
```

---

## Implementation Order

| Phase | Files | Description |
|-------|-------|-------------|
| 1 | `core/domain/context.py` | Value objects (AncestorInfo, ParentContext, ChildResult) |
| 2 | `core/domain/events.py` | Replace ChildSpawned/ChildCompleted + add shared context events |
| 3 | `core/domain/shared_context.py` | SharedExecutionContext aggregate |
| 4 | `core/ports/shared_context_port.py` | Port interface |
| 5 | `core/domain/model.py` | AgentSession context methods |
| 6 | `core/domain/execution_context.py` | Add root_id field |
| 7 | `core/application/execution_service.py` | Integrate shared context port |
| 8 | `infrastructure/adapters/shared_context_adapter.py` | PostgreSQL adapter |
| 9 | `prompts/tasks/*.j2` | Template updates |
| 10 | `bootstrap/infrastructure.py` | Wire new adapter |
| 11 | Tests | Unit + integration tests |

---

## Critical Files Summary

| File | Action |
|------|--------|
| `core/domain/context.py` | **CREATE** - Value objects (ParentContext, ChildResult, AncestorInfo) |
| `core/domain/events.py` | **MODIFY** - Replace ChildSpawned/ChildCompleted, add shared context events |
| `core/domain/shared_context.py` | **CREATE** - SharedExecutionContext aggregate |
| `core/domain/model.py` | **MODIFY** - Add context methods to AgentSession |
| `core/domain/execution_context.py` | **MODIFY** - Add root_id field |
| `core/ports/shared_context_port.py` | **CREATE** - Port interface |
| `core/application/execution_service.py` | **MODIFY** - Integrate shared context |
| `infrastructure/adapters/shared_context_adapter.py` | **CREATE** - PostgreSQL adapter |
| `infrastructure/adapters/postgres_event_store.py` | **MODIFY** - Update event registry |
| `prompts/tasks/complexity_evaluation.j2` | **MODIFY** - Add parent context section |
| `prompts/tasks/task_decomposition.j2` | **MODIFY** - Add parent context section |
| `bootstrap/infrastructure.py` | **MODIFY** - Wire SharedContextPort adapter |

---

## Concurrency Control

- **Per-Agent**: Existing OCC via `UNIQUE(aggregate_id, sequence_number)`
- **Shared Context**: Same OCC pattern, `aggregate_id = root_id`
- **Retry Pattern**: Existing retry loop in `run_agent_step` applies to shared context operations
