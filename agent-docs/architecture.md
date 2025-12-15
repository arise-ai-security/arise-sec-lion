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
| `EventStorePort` | Persist/load domain events | `PostgresEventStore` |
| `LLMPort` | LLM reasoning operations | `LiteLLMAdapter` |
| `WorkerToolPort` | Execute worker tasks | `ClaudeCodePTYAdapter`, `OpenHandsAdapter` |

### Key Port Locations

- `core/ports/event_store_port.py:15` - `EventStorePort` Protocol
- `core/ports/llm_port.py:12` - `LLMPort` Protocol
- `core/ports/worker_port.py:18` - `WorkerToolPort` Protocol

### Key Adapter Locations

- `infrastructure/adapters/postgres_event_store.py:25` - PostgreSQL + asyncpg + OCC
- `infrastructure/adapters/litellm_adapter.py:18` - Multi-provider LLM
- `infrastructure/adapters/claude_pty_adapter.py:22` - Claude Code PTY wrapper
- `infrastructure/adapters/openhands_adapter.py:30` - OpenHands SDK wrapper

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
