# Arise Sec Lion

A **recursive, self-healing multi-agent orchestration platform** that decomposes complex tasks into subtasks executed by specialized agents.

## Features

- **Recursive Task Decomposition** - Complex tasks broken down into manageable subtasks
- **Self-Healing Execution** - Failed tasks automatically retried with learned context
- **Multiple Worker Backends** - Claude Code, OpenHands, Google ADK
- **Event Sourcing** - Complete audit trail with state reconstruction
- **Real-time Monitoring** - SSE streaming and React dashboard

## Architecture

| Layer | Pattern | Purpose |
|-------|---------|---------|
| **Domain** | Hexagonal (Ports & Adapters) | Pure business logic, no infrastructure dependencies |
| **Persistence** | Event Sourcing | Append-only events, state via replay |
| **Query** | CQRS | Separate read models for efficient queries |

### Agent Hierarchy

```
BOSS (root)
  │ decomposes task into subtasks
  ▼
PENDING ──────────────────────────────┐
  │ evaluates complexity              │
  ├─── SIMPLE ──► WORKER              │
  │                 └─► executes      │
  └─── COMPLEX ──► MANAGER            │
                     └─► spawns ──────┘ (recursive)
```

## Quick Start

```bash
git clone <repository-url>
cd arise-sec-lion/deployment
cp .env.example .env          # add your API keys
docker compose --profile local up -d --build

# Generic task (no security plugin)
docker compose --profile local exec app python main.py run "Refactor the auth module"

# Security task (SEC-bench plugin)
docker compose --profile local exec app python main.py run "Patch CVE-2023-1234" \
  --cve-file deployment/gpac_cve_instance.json
```

See the [User Manual](USER_MANUAL.md) for full CLI reference, plugin/recon configuration, worker tools, and dashboard setup.

---

## Project Structure

```
arise-sec-lion/
├── core/                    # Domain core (pure business logic, no infra imports)
│   ├── domain/              # Aggregates, events, value objects
│   ├── ports/               # Abstract interfaces (Protocol)
│   ├── application/         # Use cases, orchestration
│   └── query/               # CQRS read models
├── infrastructure/          # Concrete adapter implementations
│   └── adapters/            # PostgreSQL, LiteLLM, workers
├── plugins/                 # Optional domain plugins
│   └── security/            # SEC-bench security domain (fully removable)
├── bootstrap/               # Composition root, dependency wiring
├── presentation/            # CLI, formatters, renderers
├── query/api/               # FastAPI + SSE + React SPA dashboard
├── config/                  # Pydantic Settings + YAML configuration
├── prompts/                 # Jinja2 LLM prompt templates (4-tier)
└── deployment/              # Docker Compose, Dockerfile
```

## Documentation

Detailed documentation in [`agent-docs/`](agent-docs/):

| Document | Description |
|----------|-------------|
| [architecture-concepts.md](agent-docs/architecture-concepts.md) | CQRS, Event Sourcing, Hexagonal, DDD |
| [architecture-separation.md](agent-docs/architecture-separation.md) | Generic vs security domain boundary, plugin protocol |
| [domain-model.md](agent-docs/domain-model.md) | Agent lifecycle, domain events |
| [execution-flow.md](agent-docs/execution-flow.md) | How tasks flow through the system |
| [project-structure.md](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [configuration.md](agent-docs/configuration.md) | Config files, secrets, environments |
| [api-reference.md](agent-docs/api-reference.md) | REST endpoints, SSE streaming |
| [development.md](agent-docs/development.md) | Testing, coding standards |
| [USER_MANUAL.md](USER_MANUAL.md) | CLI reference, plugins, recon, dashboard |

## Tech Stack

- **Runtime**: Python 3.12+, `uv`, `asyncpg`
- **LLM**: LiteLLM (multi-provider), Claude Code, OpenHands, Google ADK
- **Storage**: PostgreSQL (event store)
- **API**: FastAPI, SSE streaming

## License

[License information]
