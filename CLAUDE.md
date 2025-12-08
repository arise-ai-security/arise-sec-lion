# CLAUDE.md - Arise Multi-Agent System

## What Is This Project?

A **recursive, self-healing multi-agent orchestration platform** that decomposes complex tasks into subtasks executed by specialized agents.

| Aspect | Description |
|--------|-------------|
| **Architecture** | Hexagonal (Ports & Adapters) + Event Sourcing |
| **Agent Hierarchy** | BOSS → MANAGER → WORKER (recursive) |
| **Execution** | "Black Box" tools (Claude Code, OpenHands) with thinking capture |
| **Tech Stack** | Python 3.12+, `uv`, `asyncpg`, `litellm`, `pydantic` |

## Project Structure Map

```
arise-sec-lion/
├── core/                    # Domain core (NO infrastructure imports!)
│   ├── domain/              # Pure business logic
│   │   ├── model.py         # AgentSession aggregate (THE central entity)
│   │   ├── events.py        # All domain events
│   │   └── ...
│   ├── ports/               # Abstract interfaces (Protocol)
│   │   ├── event_store_port.py
│   │   ├── llm_port.py
│   │   └── worker_port.py
│   └── application/         # Use cases, orchestration
│       ├── execution_service.py  # "The Brain" - runs everything
│       └── projections/     # Query/read side (CQRS)
│
├── infrastructure/          # Concrete implementations
│   ├── adapters/            # Implements ports
│   │   ├── postgres_event_store.py
│   │   ├── litellm_adapter.py
│   │   ├── claude_pty_adapter.py
│   │   └── openhands_adapter.py
│   └── sql/                 # Database schemas
│
├── bootstrap/               # Dependency injection (composition root)
├── config/                  # YAML settings + pydantic-settings
├── presentation/            # CLI (Click commands)
├── prompts/                 # Jinja2 templates for LLM prompts
├── deployment/              # Docker, docker-compose
└── agent-docs/              # Detailed documentation (see below)
```

## Critical Architectural Rule

**`core/` must NEVER import from `infrastructure/`**

Dependencies flow inward:
```
Infrastructure → Ports ← Domain Core
      ↑                      ↑
   Bootstrap ───────────────┘
```

Verify: `grep -r "from infrastructure" core/` should return nothing.

## Off-Limits Directory

**Claude shall NOT read docs in the `user-docs/` directory.** This directory contains user-specific content that should not be accessed.

## Forbidden Operations

**In any case, Claude shall NEVER:**
- **Commit** - Do not run `git commit` or create commits
- **Lint** - Do not run `ruff check`, `ruff format`, or any linting/formatting commands

The user will handle these operations manually.

## Development Environment

**This project uses Docker Compose for development.** All commands should run inside containers.

### Running the System

```bash
cd deployment

# First time or after code changes: build and start
docker compose up --build -d

# Run a task (container must be running)
docker compose exec app python main.py run "Your task description"

# View logs
docker compose logs -f app

# View results
docker compose exec app python main.py events
docker compose exec app python main.py summary
docker compose exec app python main.py list

# Stop services
docker compose down
```

### Running Tests

```bash
docker compose exec app uv run pytest
docker compose exec app uv run pytest -v --tb=short
```

### Important

- **DO NOT** suggest bare `uv run` commands - always use `docker compose exec app`
- The app container has the source mounted for hot reload
- API keys are configured via `deployment/.env`

## Agent Roles & Flow

```
BOSS (root)
  │ decomposes task
  ▼
PENDING ──────────────────────────────┐
  │ evaluates complexity              │
  ├─── SIMPLE ──► WORKER              │
  │                 └─► executes      │
  └─── COMPLEX ──► MANAGER            │
                     └─► spawns ──────┘ (recursive)
```

## Key Domain Concepts

| Concept | Location | Purpose |
|---------|----------|---------|
| `AgentSession` | `core/domain/model.py:45` | Aggregate root, all agent state |
| `DomainEvent` | `core/domain/events.py` | Immutable facts (event sourcing) |
| `EventStorePort` | `core/ports/event_store_port.py` | Persistence interface |
| `AgentExecutionService` | `core/application/execution_service.py:25` | Orchestration ("The Brain") |

## Event Sourcing Essentials

1. **State = replay of events** - Never store state directly
2. **OCC required** - All writes use `expected_version` parameter
3. **Failures are events** - `WorkFailed` event, not exceptions
4. **Append-only** - Never update/delete events

## Coding Standards

| Standard | Requirement |
|----------|-------------|
| Type hints | Mandatory on all functions |
| Async | All ports/adapters use `async/await` |
| Docstrings | Required on public classes/methods |
| Tests | Red-Green-Refactor (TDD), Given-When-Then |

## Detailed Documentation

**Before starting work, decide which docs are relevant and read them:**

| Document | When to Read |
|----------|--------------|
| [`agent-docs/architecture.md`](agent-docs/architecture.md) | Modifying layer boundaries, adding ports/adapters |
| [`agent-docs/event-sourcing.md`](agent-docs/event-sourcing.md) | Working with events, state, persistence |
| [`agent-docs/design-principles.md`](agent-docs/design-principles.md) | Understanding code patterns, reviewing code |
| [`agent-docs/domain-model.md`](agent-docs/domain-model.md) | Modifying agent behavior, lifecycle, domain logic |
| [`agent-docs/development.md`](agent-docs/development.md) | Building, testing, Docker, coding conventions |

**Instruction:** Read the relevant doc(s) before implementing changes. If unsure which docs apply, ask.

## Pre-Commit Checklist

```bash
uv run ruff check .                      # ✓ No lint errors
uv run ruff format . --check             # ✓ Formatting correct
uv run pytest                            # ✓ All tests pass
grep -r "from infrastructure" core/      # ✓ No dependency violations
```

## Implementation Status

- [x] Event Store (PostgreSQL + OCC)
- [x] Domain Model (AgentSession, Events)
- [x] LiteLLM Adapter (Multi-provider LLM)
- [x] Claude Code PTY Adapter (Thinking capture)
- [x] OpenHands Adapter (Alternative worker)
- [x] CLI with Click (run, events, summary, list)
- [x] Projection Pipeline (Query side)
