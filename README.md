# Arise Sec Lion

A **recursive, self-healing multi-agent orchestration platform** that decomposes complex tasks into subtasks executed by specialized agents.

## Features

- **Recursive Task Decomposition** - Complex tasks are broken down into manageable subtasks
- **Self-Healing Execution** - Failed tasks are automatically retried with learned context
- **Multiple Worker Backends** - Claude Code PTY and OpenHands adapters
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

- **BOSS** - Root agent that receives the initial task and decomposes it
- **MANAGER** - Handles complex subtasks by further decomposition
- **WORKER** - Executes simple tasks using Claude Code or OpenHands

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- API keys for LLM providers (OpenAI, Anthropic, etc.)

## Quick Start

1. **Clone and configure:**
```bash
git clone <repository-url>
cd arise-sec-lion
cp deployment/.env.example deployment/.env
# Edit deployment/.env with your API keys
```

2. **Start the system:**
```bash
cd deployment
docker compose up --build -d
```

3. **Run a task:**
```bash
docker compose exec app python main.py run "Your task description"
```

4. **Monitor progress:**
```bash
docker compose logs -f app
```

## CLI Commands

All commands run inside the Docker container:

```bash
cd deployment

# Run a new task
docker compose exec app python main.py run "Analyze this codebase"

# List all sessions
docker compose exec app python main.py list

# View events for a session
docker compose exec app python main.py events [SESSION_ID]

# Get execution summary
docker compose exec app python main.py summary [SESSION_ID]
```

## Running Tests

```bash
cd deployment
docker compose exec app uv run pytest
docker compose exec app uv run pytest -v --tb=short
```

## Project Structure

```
arise-sec-lion/
├── core/                    # Domain core (pure business logic)
│   ├── domain/              # Aggregates, events, value objects
│   ├── ports/               # Abstract interfaces (Protocol)
│   ├── application/         # Use cases, orchestration
│   └── query/               # CQRS read models
├── infrastructure/          # Concrete implementations
│   └── adapters/            # PostgreSQL, LiteLLM, workers
├── bootstrap/               # Dependency injection
├── presentation/            # CLI, REST API, React dashboard
├── prompts/                 # Jinja2 LLM prompt templates
└── deployment/              # Docker Compose configuration
```

## Documentation

Detailed documentation is available in [`agent-docs/`](agent-docs/):

| Document | Description |
|----------|-------------|
| [architecture.md](agent-docs/architecture.md) | Layer boundaries, ports & adapters |
| [event-sourcing.md](agent-docs/event-sourcing.md) | Event store, state reconstruction |
| [domain-model.md](agent-docs/domain-model.md) | Agent lifecycle, domain events |
| [api.md](agent-docs/api.md) | REST endpoints, SSE streaming |
| [deployment.md](agent-docs/deployment.md) | Docker Compose, networking |

## Tech Stack

- **Runtime**: Python 3.12+, `uv`, `asyncpg`
- **LLM**: LiteLLM (multi-provider), Claude Code, OpenHands
- **Storage**: PostgreSQL (event store)
- **API**: FastAPI, SSE streaming
- **Frontend**: React, XYFlow

## License

[License information]
