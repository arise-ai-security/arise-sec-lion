# Architectural Patterns Overview

This document explains the core architectural patterns used in Arise Sec Lion. Understanding these patterns is essential for contributing to the codebase.

---

## Table of Contents

1. [Hexagonal Architecture](#1-hexagonal-architecture-ports--adapters)
2. [Event Sourcing](#2-event-sourcing)
3. [CQRS](#3-cqrs-command-query-responsibility-segregation)
4. [Domain-Driven Design](#4-domain-driven-design-concepts)
5. [Optimistic Concurrency Control](#5-optimistic-concurrency-control-occ)
6. [How They Work Together](#6-how-they-work-together-in-arise-sec-lion)

---

## 1. Hexagonal Architecture (Ports & Adapters)

### The Problem It Solves

Traditional layered architecture creates tight coupling:

```
❌ Traditional Layers (tightly coupled)

Controller → Service → Repository → Database
                ↓
           External API

Problem: Service layer directly depends on database and external APIs.
         Changing database = rewriting service layer.
         Testing = needs real database.
```

### The Solution

Hexagonal Architecture (also called "Ports & Adapters") isolates business logic from external systems:

```
✅ Hexagonal Architecture

                    ┌─────────────────────┐
   Adapters         │                     │         Adapters
   (Infrastructure) │    Domain Core      │    (Infrastructure)
                    │   (Business Logic)  │
  ┌──────────┐      │                     │      ┌──────────┐
  │ Postgres │◄────►│ ◄── Ports ──►       │◄────►│  LiteLLM │
  │ Adapter  │      │  (Interfaces)       │      │  Adapter │
  └──────────┘      │                     │      └──────────┘
                    └─────────────────────┘
```

### Key Concepts

| Concept | What It Is | Example in Our Code |
|---------|------------|---------------------|
| **Port** | Abstract interface (Protocol) | `EventStorePort`, `LLMPort` |
| **Adapter** | Concrete implementation | `PostgresEventStore`, `LiteLLMAdapter` |
| **Domain Core** | Pure business logic | `AgentSession`, `DomainEvent` |

### In Our Codebase

```
core/                          # Domain Core (NO infrastructure imports!)
├── domain/                    # Pure business logic
│   ├── model.py              # AgentSession aggregate
│   └── events.py             # Domain events
├── ports/                     # Abstract interfaces (Ports)
│   ├── event_store_port.py   # Protocol for persistence
│   ├── llm_port.py           # Protocol for LLM calls
│   └── worker_port.py        # Protocol for task execution
└── application/               # Orchestration (uses ports, not adapters)

infrastructure/                # Adapters (implements ports)
├── adapters/
│   ├── postgres_event_store.py   # Implements EventStorePort
│   ├── litellm_adapter.py        # Implements LLMPort
│   └── claude_pty_adapter.py     # Implements WorkerToolPort
```

### The Golden Rule

```python
# ✅ CORRECT: Domain uses Port (abstraction)
# core/application/execution_service.py
class AgentExecutionService:
    def __init__(self, event_store: EventStorePort, ...):  # Port, not concrete class
        self.event_store = event_store

# ❌ WRONG: Domain imports infrastructure
# This would violate hexagonal architecture
from infrastructure.adapters.postgres_event_store import PostgresEventStore  # NEVER in core/
```

### Benefits

1. **Testability**: Inject mock adapters for unit tests
2. **Flexibility**: Swap PostgreSQL for MongoDB without changing domain
3. **Isolation**: Domain logic has no external dependencies

---

## 2. Event Sourcing

### The Problem It Solves

Traditional CRUD stores only current state:

```
❌ Traditional CRUD

┌─────────────────────────────────────┐
│ agents table                        │
├─────────────────────────────────────┤
│ id: 123                             │
│ status: "completed"                 │
│ result: "Task done"                 │
│ updated_at: 2024-01-15 10:30:00     │
└─────────────────────────────────────┘

Problem: How did we get here? What was the previous status?
         When did it change? Who changed it? WHY?
```

### The Solution

Event Sourcing stores every change as an immutable event:

```
✅ Event Sourcing

┌──────────────────────────────────────────────────────────────┐
│ events table                                                 │
├──────────────────────────────────────────────────────────────┤
│ seq=1  AgentCreated    {role: "boss"}           10:00:00     │
│ seq=2  TaskAssigned    {task: "Build API"}      10:00:01     │
│ seq=3  StatusChanged   {old: "pending", new: "analyzing"}    │
│ seq=4  SubtasksDefined {subtasks: [...]}        10:00:05     │
│ seq=5  ChildSpawned    {child_id: "456"}        10:00:05     │
│ seq=6  StatusChanged   {old: "analyzing", new: "waiting"}    │
│ seq=7  ChildCompleted  {child_id: "456", result: "..."}      │
│ seq=8  WorkCompleted   {result: "Task done"}    10:30:00     │
└──────────────────────────────────────────────────────────────┘

Current state = replay all events
Complete history = always available
```

### Key Concepts

| Concept | What It Is | Example |
|---------|------------|---------|
| **Event** | Immutable fact that happened | `AgentCreated`, `WorkCompleted` |
| **Aggregate** | Entity whose state is derived from events | `AgentSession` |
| **Event Store** | Append-only log of events | PostgreSQL `events` table |
| **Replay** | Reconstruct state by applying events | `load_from_history()` |

### In Our Codebase

**Events** (`core/domain/events.py`):
```python
class DomainEvent(BaseModel):
    model_config = {"frozen": True}  # Immutable!

    event_id: UUID
    aggregate_id: UUID       # Which agent this belongs to
    sequence_number: int     # Order within aggregate
    occurred_at: datetime
```

**State Reconstruction** (`core/domain/model.py`):
```python
class AgentSession:
    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "AgentSession":
        """Reconstruct state by replaying events."""
        instance = cls(events[0].aggregate_id)
        for event in events:
            instance._apply(event)  # Each event mutates state
        return instance

    @singledispatchmethod
    def _apply(self, event: DomainEvent) -> None:
        raise TypeError(f"No handler for {type(event)}")

    @_apply.register
    def _(self, event: AgentCreated) -> None:
        self.role = AgentRole(event.role)
        self.status = AgentStatus.PENDING

    @_apply.register
    def _(self, event: WorkCompleted) -> None:
        self.status = AgentStatus.COMPLETED
        self.result = event.result
```

**Append-Only Persistence**:
```python
# We NEVER update or delete events
await event_store.append(event, expected_version=current_version)

# To get current state, we replay:
events = await event_store.get_events(agent_id)
agent = AgentSession.load_from_history(events)
```

### Benefits

1. **Complete Audit Trail**: Every change is recorded forever
2. **Time Travel**: Reconstruct state at any point in history
3. **Debugging**: See exactly what happened and when
4. **Analytics**: Analyze patterns in historical data

---

## 3. CQRS (Command Query Responsibility Segregation)

### The Problem It Solves

Single model for both writes and reads creates conflicts:

```
❌ Single Model

┌────────────────┐
│  Agent Model   │◄──── Write: "complete this task"
│                │◄──── Read: "show me all agents with costs"
│                │◄──── Read: "show agent hierarchy as tree"
│                │◄──── Read: "show cost breakdown by model"
└────────────────┘

Problem: Model optimized for writes is awkward for complex queries.
         Adding query features bloats the write model.
```

### The Solution

Separate models for commands (writes) and queries (reads):

```
✅ CQRS

         Commands                              Queries
            │                                     │
            ▼                                     ▼
    ┌───────────────┐                    ┌───────────────┐
    │ Write Model   │                    │ Read Models   │
    │ (AgentSession)│────Events────►     │ (Projections) │
    │               │                    │               │
    │ - State       │                    │ - TreeView    │
    │ - Behavior    │                    │ - CostSummary │
    │ - Validation  │                    │ - Statistics  │
    └───────────────┘                    └───────────────┘
```

### Key Concepts

| Concept | What It Is | Example |
|---------|------------|---------|
| **Command** | Intent to change state | `assign_task()`, `execute_task()` |
| **Query** | Request for information | "Get agent tree", "Get cost summary" |
| **Write Model** | Optimized for state changes | `AgentSession` aggregate |
| **Read Model** | Optimized for queries | `HierarchyProjection`, `SummaryProjection` |
| **Projection** | Transforms events into read model | Builds tree from events |

### In Our Codebase

**Write Side** (`core/domain/model.py`):
```python
class AgentSession:
    """Write model - handles commands, emits events."""

    async def evaluate_task(self, llm_port, prompt_builder):
        # Business logic
        subtasks = parse_subtasks(llm_response)

        # Emit events (commands produce events)
        event = SubtasksDefined(subtasks=subtasks)
        self._apply(event)
        self._changes.append(event)
```

**Read Side** (`core/query/projections/`):
```python
class HierarchyProjection(Projection):
    """Read model - builds tree structure for visualization."""

    def project(self, events: Iterable[DomainEvent]) -> AgentTree:
        nodes = {}
        for event in events:
            if isinstance(event, AgentCreated):
                nodes[event.aggregate_id] = TreeNode(...)
            elif isinstance(event, ChildSpawned):
                nodes[event.aggregate_id].children.append(event.child_id)
        return AgentTree(nodes)
```

### Benefits

1. **Optimized Models**: Each side optimized for its purpose
2. **Scalability**: Read side can be scaled independently
3. **Flexibility**: Multiple read models from same events
4. **Simplicity**: Write model doesn't need query concerns

---

## 4. Domain-Driven Design Concepts

### Aggregate

An **Aggregate** is a cluster of objects treated as a single unit for data changes.

```python
class AgentSession:  # This is our Aggregate Root
    """All state changes go through this class."""

    session_id: UUID           # Aggregate ID
    role: AgentRole
    status: AgentStatus
    child_ids: list[UUID]      # References to other aggregates
    _changes: list[DomainEvent]  # Pending changes
```

**Rules**:
- All modifications go through the aggregate root
- Aggregates are loaded/saved as a whole
- References to other aggregates use IDs only (not object references)

### Value Object

**Value Objects** are immutable and compared by value, not identity.

```python
@dataclass(frozen=True)  # Immutable!
class Subtask:
    """Value object - no identity, compared by content."""
    description: str
    config: dict[str, Any]

# Two subtasks with same content are equal
subtask1 = Subtask(description="Build API", config={})
subtask2 = Subtask(description="Build API", config={})
assert subtask1 == subtask2  # True!
```

### Domain Event

**Domain Events** capture something that happened in the domain.

```python
class WorkCompleted(DomainEvent):
    """Something happened: work was completed."""
    result: str

# Events are:
# - Past tense (WorkCompleted, not CompleteWork)
# - Immutable (frozen=True)
# - Self-contained (all needed data included)
```

### Ubiquitous Language

We use consistent terminology throughout:

| Term | Meaning |
|------|---------|
| Agent | An autonomous unit that performs tasks |
| Session | The lifetime of an agent's work |
| BOSS | Root agent that orchestrates |
| MANAGER | Decomposes tasks into subtasks |
| WORKER | Executes tasks directly |
| Subtask | A unit of work spawned by BOSS/MANAGER |

---

## 5. Optimistic Concurrency Control (OCC)

### The Problem It Solves

Concurrent writes can corrupt data:

```
❌ Without OCC

Thread A: Read agent (version 1)
Thread B: Read agent (version 1)
Thread A: Update agent → version 2
Thread B: Update agent → version 2  ← OVERWRITES Thread A's changes!
```

### The Solution

OCC detects conflicts and rejects stale writes:

```
✅ With OCC

Thread A: Read agent (version 1)
Thread B: Read agent (version 1)
Thread A: Write with expected_version=1 → Success, now version 2
Thread B: Write with expected_version=1 → REJECTED! (actual is 2)
Thread B: Retry - Read agent (version 2)
Thread B: Write with expected_version=2 → Success, now version 3
```

### In Our Codebase

**Event Store Interface**:
```python
class EventStorePort(Protocol):
    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Raises ConcurrencyError if version mismatch."""
```

**Usage in ExecutionService**:
```python
async def run_agent_step(self, agent_id: UUID) -> None:
    retry_count = 0

    while retry_count < self.max_retries:
        try:
            # Load current state
            events = await self.event_store.get_events(agent_id)
            agent = AgentSession.load_from_history(events)
            current_version = agent.version

            # Do work
            await self._dispatch_agent_action(agent)

            # Persist with version check
            for event in agent.events:
                await self.event_store.append(event, expected_version=current_version)
                current_version += 1

            return  # Success!

        except ConcurrencyError:
            retry_count += 1
            # Loop will reload and retry
```

### Benefits

1. **No Locks**: Better performance than pessimistic locking
2. **Conflict Detection**: Prevents silent data corruption
3. **Automatic Retry**: System handles conflicts gracefully

---

## 6. How They Work Together in Arise Sec Lion

### Complete Request Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           REQUEST FLOW                                  │
└─────────────────────────────────────────────────────────────────────────┘

1. CLI: "run task"
         │
         ▼
2. ExecutionService (Application Layer)
         │
         │  Uses ports, not concrete implementations
         ▼
3. AgentSession.create() ──► AgentCreated event
         │
         ▼
4. event_store.append(event, expected_version=0)
         │                              │
         │                    ┌─────────┴─────────┐
         │                    │ OCC Check         │
         │                    │ version match?    │
         │                    └─────────┬─────────┘
         │                              │
         ▼                              ▼
5. PostgresEventStore           Events Table
   (Infrastructure)             ┌────────────────┐
         │                      │ seq=1 Created  │
         │                      │ seq=2 Assigned │
         │                      │ seq=3 ...      │
         │                      └────────────────┘
         │
         ▼
6. Query (Read Side)
   HierarchyProjection.project(events) ──► Tree View
   SummaryProjection.project(events) ──► Cost Summary
```

### Pattern Interactions

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PATTERN RELATIONSHIPS                            │
└─────────────────────────────────────────────────────────────────────────┘

                    Hexagonal Architecture
                    ─────────────────────
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
       ┌────────┐    ┌──────────┐    ┌────────┐
       │ Ports  │    │ Domain   │    │Adapters│
       │        │    │ Core     │    │        │
       └────────┘    └──────────┘    └────────┘
                           │
                           │ Uses
                           ▼
                    Event Sourcing
                    ─────────────
                           │
            ┌──────────────┴──────────────┐
            │                             │
            ▼                             ▼
     ┌─────────────┐              ┌─────────────┐
     │   Events    │─────────────►│ Projections │
     │ (Write Side)│              │ (Read Side) │
     └─────────────┘              └─────────────┘
            │                             │
            │                             │
            └───────────┬─────────────────┘
                        │
                        ▼
                      CQRS
                    ────────

     All protected by OCC during writes
```

### Why These Patterns?

| Pattern | Why We Use It |
|---------|---------------|
| Hexagonal | Test domain logic without database; swap LLM providers easily |
| Event Sourcing | Complete audit trail of agent decisions; debug failures by replaying |
| CQRS | Tree visualization doesn't affect write performance; cost analytics separate from execution |
| OCC | Multiple agents can process concurrently without locks |

---

## Summary Cheat Sheet

| Pattern | One-Liner | Key Benefit |
|---------|-----------|-------------|
| **Hexagonal** | Business logic doesn't know about databases | Testability, flexibility |
| **Event Sourcing** | Store changes, not state | Complete history, debugging |
| **CQRS** | Separate read and write models | Performance, clarity |
| **OCC** | Check version before writing | Concurrency without locks |
| **Aggregate** | Consistency boundary for changes | Data integrity |
| **Value Object** | Immutable, compared by value | Simplicity, safety |
| **Domain Event** | Fact that happened | Decoupling, audit |
