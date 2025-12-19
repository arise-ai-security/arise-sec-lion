# Milestone 5: Shared State via Projections

**Status**: Not Started
**Prerequisites**: Milestones 1-4 complete

**Goal**: Enable agents to query system-wide state without violating event sourcing.

---

## Implementation Steps

1. Create `SystemStateProjection`
2. Create `ProjectionPort`
3. Implement `ContextBuilder`
4. Integrate with execution service

---

## 5.1 System State Projection

**File**: `core/query/projections/impl/system_state.py` (new)

```python
class SystemStateProjection(Projection):
    """Aggregate events into queryable system state."""

    def project(self, events: Iterable[DomainEvent]) -> SystemState:
        state = SystemState()
        for event in events:
            if isinstance(event, AgentCreated):
                state.agents[str(event.aggregate_id)] = AgentInfo(...)
            elif isinstance(event, WorkCompleted):
                state.completed_tasks.append(...)
            elif isinstance(event, ChildSpawned):
                state.files_created.extend(...)
        return state

@dataclass
class SystemState:
    agents: dict[str, AgentInfo]
    completed_tasks: list[TaskSummary]
    files_created: list[str]
    decisions_made: list[str]
    total_cost_usd: float
```

---

## 5.2 Projection Port

**File**: `core/ports/projection_port.py` (new)

```python
class ProjectionPort(Protocol):
    """Query projections for read-only state."""

    async def get_system_state(self) -> SystemState
    async def get_sibling_status(self, agent_id: UUID) -> list[SiblingInfo]
    async def get_cost_summary(self) -> CostSummary
```

---

## 5.3 Context Builder Service

**File**: `core/application/context_builder.py` (new)

```python
class ContextBuilder:
    """Build context for agents using projections."""

    def __init__(self, projection_port: ProjectionPort):
        self._projection = projection_port

    async def build_worker_context(self, agent: AgentSession) -> dict[str, Any]:
        """Build context for worker including sibling info."""
        state = await self._projection.get_system_state()
        siblings = await self._projection.get_sibling_status(agent.session_id)
        return {
            "files_created_by_siblings": state.files_created,
            "sibling_status": [(s.task, s.status) for s in siblings],
            "system_cost_so_far": state.total_cost_usd,
        }
```

---

## 5.4 Execution Service Integration

**File**: `core/application/execution_service.py`

Add `projection_port` parameter and use `ContextBuilder` to enrich context before dispatching workers.

```python
class AgentExecutionService:
    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        system_limits: SystemLimitsConfig,
        projection_port: ProjectionPort | None = None,  # NEW
        ...
    ):
        self._context_builder = ContextBuilder(projection_port) if projection_port else None
```

---

## Data Models

```python
@dataclass
class AgentInfo:
    agent_id: str
    role: str
    status: str
    task_description: str
    parent_id: str | None

@dataclass
class TaskSummary:
    agent_id: str
    task: str
    result: str
    completed_at: datetime

@dataclass
class SiblingInfo:
    agent_id: str
    task: str
    status: str
    result: str | None

@dataclass
class CostSummary:
    total_cost_usd: float
    llm_cost_usd: float
    worker_cost_usd: float
    cost_by_model: dict[str, float]
    cost_by_agent: dict[str, float]
```

---

## Testing Strategy

Key test scenarios:
- Parent queries sibling status via projection
- Worker receives context with files created by siblings
- Cost summary accurately aggregates all events
- System state reflects current agent hierarchy

## Files to Modify/Create

| File | Action |
|------|--------|
| `core/query/projections/impl/system_state.py` | Create new |
| `core/ports/projection_port.py` | Create new |
| `core/application/context_builder.py` | Create new |
| `core/application/execution_service.py` | Add projection_port, use ContextBuilder |
| `bootstrap/application.py` | Wire projection_port |
