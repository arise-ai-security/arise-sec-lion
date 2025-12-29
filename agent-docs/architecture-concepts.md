# Architecture Concepts

This document explains the core architectural patterns used in Arise Sec Lion. These patterns work together to create a system that is maintainable, testable, and resilient to concurrent operations.

---

## Event Sourcing

**What it is:** Instead of storing current state, we store a sequence of events that represent everything that happened. State is derived by replaying these events.

**How we use it:**
```
Traditional: UPDATE agents SET status = 'completed' WHERE id = 123
Event Sourcing: APPEND events (AgentCreated, TaskAssigned, WorkCompleted)
```

**In this codebase:**
- `AgentSession` (aggregate) has no persistent fields—state is rebuilt from events
- `AgentSession.load_from_history(events)` replays events to reconstruct state
- Events are immutable facts stored in `core/domain/events.py`
- PostgreSQL `events` table is append-only

**Benefits:**
- Complete audit trail of everything that happened
- Can replay to any point in time
- Debug by examining event history
- Natural fit for distributed systems

**Key files:**
- `core/domain/events.py` — All domain events
- `core/domain/model.py:286` — `load_from_history()` replays events
- `infrastructure/adapters/postgres_event_store.py` — Persistence

---

## CQRS (Command Query Responsibility Segregation)

**What it is:** Separate the write model (commands that change state) from the read model (queries that return data). They can use different data structures optimized for their purpose.

```
┌─────────────┐         ┌─────────────────────────────┐
│   Command   │────────▶│  Domain Model (Write Side)  │
│  "Run task" │         │  AgentSession + Events      │
└─────────────┘         └──────────────┬──────────────┘
                                       │ events
                                       ▼
                        ┌─────────────────────────────┐
                        │      Event Store            │
                        │      (PostgreSQL)           │
                        └──────────────┬──────────────┘
                                       │ events
                                       ▼
┌─────────────┐         ┌─────────────────────────────┐
│    Query    │◀────────│  Read Model (Query Side)    │
│  "Get summary" │      │  Projections                │
└─────────────┘         └─────────────────────────────┘
```

**In this codebase:**
- **Write side:** `AgentSession` aggregate handles commands, emits events
- **Read side:** `core/query/projections/` builds read-optimized views from events
- **Projections:** Transform events into summaries, statistics, formatted output

**Example projection flow:**
```python
# Write: Command creates events
agent.assign_task("Create fizzbuzz")  # Emits TaskAssigned event

# Read: Query builds projection from events
summary = AgentSummaryService(event_store).build(agent_id)
# Returns: {role, status, subtasks, result, ...}
```

**Key files:**
- `core/application/execution_service.py` — Command side (write)
- `core/query/projections/` — Query side (read)
- `query/api/routes/` — REST endpoints use projections

---

## OCC (Optimistic Concurrency Control)

**What it is:** Instead of locking rows before updates, we assume no conflicts and check at write time. If another process modified the data, we retry.

```
Process A: Read agent (version=5)
Process B: Read agent (version=5)
Process B: Write event (expected_version=5) ✓ → version becomes 6
Process A: Write event (expected_version=5) ✗ → ConcurrencyError (actual=6)
Process A: Reload, retry with version=6
```

**In this codebase:**
- Every `append()` call includes `expected_version`
- PostgreSQL checks version matches before insert
- On conflict, `ConcurrencyError` is raised and operation retries

**Implementation:**
```python
# execution_service.py:178
async def run_agent_step(self, agent_id: UUID) -> None:
    while retry_count < self._config.max_retries:
        try:
            agent = await self._repository.load(agent_id)
            current_version = agent.version  # e.g., 5

            await self._dispatch_agent_action(agent)

            # Append with version check
            for event in agent.events:
                await self._event_store.append(event, expected_version=current_version)
                current_version += 1
            return

        except ConcurrencyError:
            retry_count += 1  # Reload and retry
```

**Key files:**
- `core/ports/event_store_port.py` — `append(event, expected_version)`
- `infrastructure/adapters/postgres_event_store.py` — Version check in SQL
- `core/domain/exceptions.py` — `ConcurrencyError`

---

## Hexagonal Architecture (Ports & Adapters)

**What it is:** The domain core is isolated from external systems (databases, APIs, UIs) through abstract interfaces called "ports". Concrete implementations called "adapters" plug into these ports.

```
                    ┌─────────────────────────────────┐
                    │         Presentation            │
                    │      (CLI, REST API, Web)       │
                    └───────────────┬─────────────────┘
                                    │
                    ┌───────────────▼─────────────────┐
                    │          Application            │
                    │    (AgentExecutionService)      │
                    └───────────────┬─────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
   ┌─────────┐                ┌─────────┐                ┌─────────┐
   │  Port   │                │  Port   │                │  Port   │
   │EventStore│               │   LLM   │                │ Worker  │
   └────┬────┘                └────┬────┘                └────┬────┘
        │                          │                          │
        ▼                          ▼                          ▼
   ┌─────────┐                ┌─────────┐                ┌─────────┐
   │ Adapter │                │ Adapter │                │ Adapter │
   │Postgres │                │ LiteLLM │                │ClaudeCode│
   └─────────┘                └─────────┘                └─────────┘
```

**Critical rule:** `core/` never imports from `infrastructure/`

```
✓ infrastructure/adapters/postgres_event_store.py imports core/ports/event_store_port.py
✗ core/domain/model.py imports infrastructure/adapters/...  (FORBIDDEN)
```

**In this codebase:**
- **Ports** (interfaces): `core/ports/` — `EventStorePort`, `LLMPort`, `WorkerToolPort`
- **Adapters** (implementations): `infrastructure/adapters/` — PostgreSQL, LiteLLM, Claude Code
- **Bootstrap** wires adapters to ports at startup

**Benefits:**
- Swap PostgreSQL for MongoDB without touching domain code
- Test domain logic with in-memory fakes
- Each adapter is independently testable

**Key files:**
- `core/ports/event_store_port.py` — Abstract interface
- `core/ports/llm_port.py` — LLM interface
- `core/ports/worker_port.py` — Worker tool interface
- `infrastructure/adapters/` — Concrete implementations
- `bootstrap/` — Wires everything together

---

## DDD (Domain-Driven Design)

**What it is:** Structure code around the business domain, using a ubiquitous language shared between developers and domain experts. Key concepts include aggregates, entities, value objects, and domain events.

**Key DDD concepts in this codebase:**

### Aggregate
A cluster of domain objects treated as a single unit for data changes. Has a root entity that controls all access.

```python
# AgentSession is our aggregate root
class AgentSession:
    """Event-sourced aggregate for agent sessions."""

    def assign_task(self, task_description: str) -> None:
        """Command method - emits TaskAssigned event."""

    def handle_child_update(self, child_id, result) -> None:
        """Command method - emits ChildCompleted event."""
```

### Domain Events
Immutable facts about something that happened. Named in past tense.

```python
# core/domain/events.py
class AgentCreated(DomainEvent): ...      # Agent was created
class TaskAssigned(DomainEvent): ...      # Task was assigned
class WorkCompleted(DomainEvent): ...     # Work was completed
class ChildSpawned(DomainEvent): ...      # Child agent was spawned
```

### Value Objects
Immutable objects defined by their attributes, not identity.

```python
# core/domain/context.py
@dataclass(frozen=True)
class ParentContext:
    """Immutable context passed from parent to child."""
    ancestry: tuple[AncestorInfo, ...]
    decisions: tuple[str, ...]
    artifacts: tuple[str, ...]
```

### Ubiquitous Language
Terms used consistently in code and conversation:

| Term | Meaning |
|------|---------|
| **BOSS** | Root agent that receives the initial task |
| **MANAGER** | Agent that decomposes complex tasks |
| **WORKER** | Agent that executes simple tasks |
| **PENDING** | Agent awaiting complexity evaluation |
| **Subtask** | Decomposed piece of a larger task |

**Key files:**
- `core/domain/model.py` — `AgentSession` aggregate
- `core/domain/events.py` — Domain events
- `core/domain/context.py` — Value objects
- `core/domain/enums.py` — `AgentRole`, `AgentStatus`

---

## How They Work Together

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              User: "Run task"                                 │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CLI (Presentation) calls AgentExecutionService (Application)                │
│  Hexagonal: UI layer talks to application through defined interface          │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  AgentExecutionService uses Ports (EventStorePort, LLMPort)                  │
│  Hexagonal: Application depends on abstractions, not implementations         │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  AgentSession (DDD Aggregate) processes command                              │
│  DDD: Business logic lives in the aggregate                                  │
│                                                                              │
│  agent.assign_task("Create fizzbuzz")                                        │
│    → Emits TaskAssigned event                                                │
│  Event Sourcing: State change = new event                                    │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  event_store.append(event, expected_version=5)                               │
│  OCC: Check version before write, retry on conflict                         │
│  Event Sourcing: Append-only storage                                         │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  Query: ProjectionPipeline builds summary from events                        │
│  CQRS: Read model is separate, built from events                             │
│  Event Sourcing: Can rebuild any view by replaying events                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Shared Execution Context

**What it is:** A centralized, event-sourced store for cross-agent state sharing within a single execution run. One SharedExecutionContext exists per hierarchy (keyed by root_id).

**Problem it solves:** In a recursive multi-agent system, agents need to share:
- Artifacts (files, code, analysis results)
- Decisions (architectural choices, tool selections)
- Progress checkpoints
- Budget tracking

**In this codebase:**

```
SharedExecutionContext (Facade)
├── ArtifactStore      - Shared outputs between agents
├── DecisionLog        - Architectural/design decisions
├── ProgressTracker    - Progress checkpoints
└── BudgetAccount      - Cost tracking with enforcement
```

**Usage pattern:**
```python
# Get or create context for this run
context = await shared_context_port.get_or_create(root_id)

# Record a decision
context.record_decision(
    decision_key="target_framework",
    decision_value="django",
    rationale="Based on requirements.txt analysis",
    decided_by=agent_id,
)

# Store an artifact
context.store_artifact(
    key="exploit_poc",
    content_type="text/python",
    content="import requests...",
    stored_by=agent_id,
)

# Save with OCC
await shared_context_port.save(context, expected_version=context.version)
```

**Key files:**
- `core/domain/shared_context.py` — Domain model
- `core/ports/shared_context_port.py` — Port interface
- `infrastructure/adapters/shared_context_adapter.py` — PostgreSQL adapter

See [`context-passing-mechanism.md`](context-passing-mechanism.md) for detailed documentation.

---

## Quick Reference

| Pattern | Problem It Solves | Where in Code |
|---------|-------------------|---------------|
| Event Sourcing | "What happened?" audit trail, time travel | `core/domain/events.py`, `model.py` |
| CQRS | Read vs write optimization | `core/query/projections/` |
| OCC | Concurrent writes without locks | `event_store.append(expected_version)` |
| Hexagonal | Swap infrastructure without domain changes | `core/ports/`, `infrastructure/adapters/` |
| DDD | Business logic clarity, ubiquitous language | `core/domain/` |
| Shared Context | Cross-agent state sharing | `core/domain/shared_context.py` |
