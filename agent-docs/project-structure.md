# Project Structure

This document describes the directory layout and layer responsibilities.

---

## Directory Overview

```
arise-sec-lion/
├── core/                    # Domain core (NO infrastructure imports!)
├── infrastructure/          # Concrete implementations (adapters)
├── bootstrap/               # Dependency injection (composition root)
├── config/                  # YAML settings + Pydantic models
├── presentation/            # CLI interface
├── query/                   # Query side (API + Web dashboard)
├── prompts/                 # Jinja2 LLM prompt templates
├── deployment/              # Docker, docker-compose
└── agent-docs/              # Documentation for Claude
```

---

## Core Layer (`core/`)

**Rule:** Must NEVER import from `infrastructure/`

### Domain (`core/domain/`)

Pure business logic, no I/O.

| File | Purpose |
|------|---------|
| `model.py:35` | `AgentSession` aggregate root |
| `events.py` | All domain events (frozen dataclasses) |
| `enums.py` | `AgentRole`, `AgentStatus` |
| `context.py` | `ParentContext`, `ChildResult` value objects |
| `shared_context.py` | `SharedExecutionContext` for cross-agent state |
| `services.py` | `SubtaskParser` domain service |
| `prompt_builder.py` | Builds LLM prompts from templates |
| `agent_config.py` | `AgentConfig` Pydantic model |
| `execution_context.py` | Depth/child limits |

### Ports (`core/ports/`)

Abstract interfaces (Protocol classes).

| File | Interface |
|------|-----------|
| `event_store_port.py` | `EventStorePort` - event persistence |
| `llm_port.py` | `LLMPort` - LLM queries |
| `worker_port.py` | `WorkerToolPort` - worker execution |
| `shared_context_port.py` | `SharedContextPort` - cross-agent state |
| `sibling_context_port.py` | `SiblingContextPort` - sibling worker context |
| `cost_calculator_port.py` | `CostCalculatorPort` - token pricing |

### Application (`core/application/`)

Use cases and orchestration.

| File | Purpose |
|------|---------|
| `execution_service.py:56` | `AgentExecutionService` - main orchestrator |
| `agent_orchestrator.py:32` | `AgentOrchestrator` - LLM/worker calls |
| `services/agent_repository.py` | Load/save agents |
| `services/query_service.py` | Read operations (CQRS) |
| `services/child_factory.py` | Create child agents |
| `services/context_registry.py` | Manage execution contexts |
| `services/sibling_context_builder.py` | Build sibling worker context |
| `services/parent_notifier.py` | Notify parent on child completion |

### Query (`core/query/`)

CQRS read side.

| File | Purpose |
|------|---------|
| `projections/models.py` | Read model dataclasses |
| `projections/impl/` | Projection implementations |

---

## Infrastructure Layer (`infrastructure/`)

Concrete implementations of ports.

### Adapters (`infrastructure/adapters/`)

| File | Implements |
|------|------------|
| `postgres_event_store.py` | `EventStorePort` |
| `litellm_adapter.py` | `LLMPort` |
| `claude_pty_adapter.py` | `WorkerToolPort` (Claude Code) |
| `openhands_adapter.py` | `WorkerToolPort` (OpenHands) |
| `worker_base.py` | Shared worker validation |
| `shared_context_adapter.py` | `SharedContextPort` |
| `cost_calculator.py` | `CostCalculatorPort` |

### SQL (`infrastructure/sql/`)

| File | Purpose |
|------|---------|
| `create_events_table.sql` | Event store schema |

---

## Bootstrap Layer (`bootstrap/`)

Composition root - wires everything together.

| File | Purpose |
|------|---------|
| `bootstrap.py` | Main entry, creates CLI |
| `infrastructure.py` | Wires adapters to ports |
| `application.py` | Wires application services |
| `presentation.py` | Wires CLI |

---

## Presentation Layer (`presentation/`)

User interfaces.

| File | Purpose |
|------|---------|
| `cli.py` | Click commands (run, events, summary, list) |
| `context.py` | Event store context manager |
| `formatters.py` | Output formatting |
| `rendering.py` | Console output |

---

## Query Layer (`query/`)

REST API and web dashboard.

### API (`query/api/`)

| File | Purpose |
|------|---------|
| `app.py` | FastAPI application factory |
| `bootstrap.py` | API-specific wiring |
| `routes/agents.py` | Agent endpoints |
| `routes/events.py` | Event endpoints + SSE |
| `routes/prompts.py` | Prompt management |
| `routes/config.py` | System config |
| `schemas.py` | Pydantic response schemas |

### Web (`query/web/`)

React dashboard.

| Path | Purpose |
|------|---------|
| `src/api/client.ts` | API client functions |
| `src/types/api.ts` | TypeScript type definitions |
| `src/components/` | React components |
| `src/pages/` | Page components |

---

## Config Layer (`config/`)

| File | Purpose |
|------|---------|
| `settings.py` | Pydantic settings models |
| `config.yaml` | Base defaults |
| `config.development.yaml` | Dev overrides |
| `config.production.yaml` | Prod overrides |

---

## Prompts (`prompts/`)

Jinja2 templates for LLM prompts.

| Directory | Purpose |
|-----------|---------|
| `system/` | System prompts |
| `strategies/` | Decomposition strategies |
| `worker/` | Worker instructions |
| `tasks/` | Task-specific prompts |
| `security/` | Security task prompts |

---

## Deployment (`deployment/`)

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Service definitions |
| `Dockerfile` | Multi-stage build |
| `.env.example` | Template for secrets |
| `.env` | Secrets (not committed) |
| `.env.dev` | Dev profile secrets |

---

## Dependency Flow

```
presentation/  ──┐
query/api/     ──┼──► bootstrap/ ──► core/application/
                 │                        │
                 │                        ▼
                 │                   core/domain/
                 │                        │
                 │                        ▼
                 │                   core/ports/ (interfaces)
                 │                        ▲
                 │                        │
                 └──► infrastructure/adapters/ (implements)
```

**Critical:** Arrows point inward. `core/` never imports from outer layers.
