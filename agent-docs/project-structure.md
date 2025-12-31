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

Pure business logic, no I/O. Organized into subdirectories:

```
core/domain/
├── aggregates/          # Aggregate roots
│   └── agent_session.py # AgentSession aggregate root
├── events/              # Domain events
│   └── events.py        # All domain events (Pydantic models)
├── services/            # Domain services
│   ├── config_resolver.py      # Resolves AgentConfig heuristically
│   ├── context_update_parser.py # Parses worker context updates
│   └── subtask_parser.py       # SubtaskParser domain service
├── values/              # Value objects (Pydantic models)
│   ├── agent_config.py         # AgentConfig
│   ├── context/                # Context types by data flow direction
│   │   ├── limits.py           # HierarchyLimits (depth/child limits)
│   │   ├── parent_to_child.py  # SpawnPayload, AncestorSummary
│   │   ├── child_to_parent.py  # TaskOutcome
│   │   └── sibling_to_sibling.py # SiblingView, SiblingStatus, SharedDecision
│   ├── enums.py                # AgentRole, AgentStatus
│   ├── llm_response.py         # LLM response types
│   ├── parsed_context.py       # Parsed context updates
│   └── subtask.py              # Subtask value object
└── shared_context.py    # SharedExecutionContext for cross-agent state
```

### Ports (`core/ports/`)

Abstract interfaces (Protocol classes).

| File | Interface |
|------|-----------|
| `event_store_port.py` | `EventStorePort` - event persistence |
| `llm_port.py` | `LLMPort` - LLM queries |
| `worker_port.py` | `WorkerToolPort` - worker execution |
| `shared_context_port.py` | `SharedContextPort` - cross-agent state |
| `sibling_context_port.py` | `SiblingViewPort` - sibling worker context |
| `cost_calculator_port.py` | `CostCalculatorPort` - token pricing |

### Application (`core/application/`)

Use cases and orchestration.

| File | Purpose |
|------|---------|
| `execution_service.py` | `AgentExecutionService` - main orchestrator |
| `agent_orchestrator.py` | `AgentOrchestrator` - delegates to pipelines |
| `pipelines.py` | `PipelineFactory` - creates configured pipelines |
| `services/agent_repository.py` | Load/save agents |
| `services/query_service.py` | Read operations (CQRS) |
| `services/child_factory.py` | Create child agents |
| `services/context_registry.py` | `HierarchyLimitsRegistry` - manage hierarchy limits |
| `services/sibling_context_builder.py` | Build sibling worker context (SiblingView) |
| `services/parent_notifier.py` | Notify parent on child completion |
| `services/prompt_builder.py` | Builds LLM prompts from Jinja2 templates |
| `services/prompt_strategy.py` | Prompt strategy selection |

#### Pipeline Architecture (`core/application/pipeline/`)

Composable step-based execution using Chain of Responsibility pattern.

| File | Purpose |
|------|---------|
| `context.py` | `PipelineState` (ephemeral transport), `StepResult` |
| `protocol.py` | `PipelineStep` protocol |
| `executor.py` | `Pipeline` executor (short-circuits on failure) |
| `steps/validation.py` | Agent state validation steps |
| `steps/prompt.py` | Prompt building steps |
| `steps/llm.py` | `QueryLLM` step |
| `steps/parsing.py` | LLM response parsing steps |
| `steps/observability.py` | Event emission steps |
| `steps/limits.py` | Limit enforcement steps |
| `steps/domain.py` | Domain method invocation steps |
| `steps/worker.py` | Worker execution steps |

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

Jinja2 templates for LLM prompts using template inheritance.

| Directory | Purpose |
|-----------|---------|
| `base/` | Base templates for inheritance (`coordinator.j2`) |
| `core/roles/` | Role-specific prompts (boss, manager, worker, pending) |
| `core/strategies/` | Decomposition and complexity strategies |
| `core/output/` | Output format specifications |
| `core/context/` | Context injection templates |
| `secbench/` | SecBench benchmark prompts |
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
