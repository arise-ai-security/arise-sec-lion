# Architecture - Hexagonal (Ports & Adapters)

> **Source:** Alistair Cockburn (2005)
> **Reference:** [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture)

## Purpose

"Create your application to work without either a UI or a database so you can run automated regression-tests against it, work when the database becomes unavailable, and link applications together without any source code dependencies."

## Core Components

| Component | Description | Location |
|-----------|-------------|----------|
| **Ports** | Abstract interfaces using `typing.Protocol` | `core/ports/` |
| **Adapters** | Concrete implementations | `infrastructure/adapters/` |
| **Domain Core** | Pure business logic, NO infrastructure deps | `core/domain/` |

## Layer Structure

```
Outer Layer (Infrastructure)
    ↓ implements
Port Interfaces (core/ports/)
    ↑ depends on
Inner Layer (Domain Core)
```

## Strict Dependency Rule

**`core/` must NEVER import from `infrastructure/`**

- `core/domain/`: Pure business logic (Pydantic models). NO SQL, NO HTTP.
- `core/ports/`: Abstract interfaces only.
- `infrastructure/`: Concrete implementations that import and implement ports.

## Port Interfaces

| Port | Purpose | Implementation |
|------|---------|----------------|
| `EventStorePort` | Persist/load domain events (composite) | `PostgresEventStore` |
| `EventStoreReadPort` | Read-only event queries | (part of composite) |
| `EventStoreWritePort` | Append events with OCC | (part of composite) |
| `EventStoreConnectPort` | Connection lifecycle | (part of composite) |
| `LLMPort` | LLM reasoning operations | `LiteLLMAdapter` |
| `WorkerToolPort` | Execute worker tasks | `ClaudeCodePTYAdapter`, `OpenHandsAdapter` |
| `SharedContextPort` | Shared execution context | `SharedContextAdapter` |
| `CostCalculatorPort` | LLM cost estimation | `CostCalculator` |

### EventStorePort Segregation (ISP)

The event store follows Interface Segregation Principle with three focused interfaces:

```
EventStoreConnectPort     EventStoreWritePort     EventStoreReadPort
  - connect()               - append()              - get_events()
  - disconnect()                                    - get_all_aggregate_ids()
  - initialize_schema()                             - get_all_events_grouped()
        │                        │                        │
        └────────────────────────┴────────────────────────┘
                                 │
                          EventStorePort (composite)
```

**Usage:** Read-only clients (projections, queries, API endpoints) depend on `EventStoreReadPort`.
Full implementations use the composite `EventStorePort`.

### Key Port Locations

- `core/ports/event_store_port.py` - `EventStorePort` and segregated interfaces
- `core/ports/llm_port.py` - `LLMPort` Protocol
- `core/ports/worker_port.py` - `WorkerToolPort` Protocol
- `core/ports/shared_context_port.py` - `SharedContextPort` Protocol
- `core/ports/cost_calculator_port.py` - `CostCalculatorPort` Protocol

### Key Adapter Locations

- `infrastructure/adapters/postgres_event_store.py` - PostgreSQL + asyncpg + OCC
- `infrastructure/adapters/litellm_adapter.py` - Multi-provider LLM
- `infrastructure/adapters/claude_pty_adapter.py` - Claude Code PTY wrapper
- `infrastructure/adapters/openhands_adapter.py` - OpenHands SDK wrapper
- `infrastructure/adapters/heuristic_reward_adapter.py` - Budget recollection heuristics
- `infrastructure/adapters/cost_calculator.py` - LLM pricing calculator

## Composition Root (Bootstrap)

The `bootstrap/` module is the **only place** that knows about both core and infrastructure:

- `bootstrap/bootstrap.py:21` - Main composition function
- `bootstrap/infrastructure.py:32` - Infrastructure wiring
- `bootstrap/application.py:18` - Application layer wiring

## Dependency Flow Diagram

```
Bootstrap (outermost - knows everything)
    │
    ├── Presentation (CLI)
    │       │
    │       ↓
    ├── Application (Services)
    │       │
    │       ↓
    ├── Infrastructure (Adapters)
    │       │
    │       ↓ implements
    └── Domain Core + Ports (innermost)
```

## Verification

Run this to verify no dependency violations:
```bash
# Should return NO results
grep -r "from infrastructure" core/
```
