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

1. **Clone and configure:**
```bash
git clone <repository-url>
cd arise-sec-lion/deployment
cp .env.example .env
# Edit .env with your API keys
```

2. **Start and run:**
```bash
docker compose --profile local up -d --build
docker compose --profile local exec app python main.py run "Your task"
```

---

## Execution by Profile

All commands run inside Docker containers. Choose a profile based on your setup:

| Profile | Database | Use Case |
|---------|----------|----------|
| `local` | Local PostgreSQL container | Development with local DB |
| `dev` | External DB (e.g., Supabase) | Development with cloud DB |
| `prod` | External DB | Production deployment |
| `test` | Ephemeral PostgreSQL (tmpfs) | Running tests |

### Local Profile (Local Database)

```bash
cd deployment

# Start services (app + api + local postgres)
docker compose --profile local up -d --build

# Run a task
docker compose --profile local exec app python main.py run "Analyze security vulnerabilities"

# View logs
docker compose --profile local logs -f app
```

### Dev Profile (External Database)

```bash
cd deployment

# Configure .env.dev with external DB credentials
# Start services (app + api, no local DB)
docker compose --profile dev up -d --build

# Run a task
docker compose --profile dev exec app-dev python main.py run "Your task"
```

### Test Profile

```bash
cd deployment

# Start ephemeral test database
docker compose --profile test up -d

# Run tests
docker compose --profile dev exec app-dev uv run pytest
```

---

## Configuration

Configuration uses YAML files with environment-specific overrides. Secrets come from environment variables.

### Config Files

```
config/
├── config.yaml              # Base configuration (all settings)
├── config.development.yaml  # Development overrides (ARISE_ENV=development)
├── config.production.yaml   # Production overrides (ARISE_ENV=production)
```

Set `ARISE_ENV` to select the environment. Configs are merged: `base <- environment-specific`.

### Key Configuration Options

#### LLM Models

```yaml
# config/config.yaml
llm:
  model_boss: gpt-4o          # Model for BOSS/MANAGER decomposition
```

#### Worker Configuration

```yaml
worker:
  tool_type: claude_code      # Options: claude_code, openhands, google_adk
  tool_model: claude-opus-4-5-20251101  # Model used by worker
  tool_timeout: 300           # Worker execution timeout (seconds)
```

#### Orchestration Limits

```yaml
orchestration:
  max_retries: 3              # Retry failed operations
  retry_delay: 0.1            # Delay between retries (seconds)
  poll_interval: 0.5          # Agent polling interval (seconds)
  llm_timeout: 30.0           # LLM call timeout (seconds)
  worker_timeout: 300.0       # Worker execution timeout (seconds)
  default_task_complexity_threshold: 5

  limits:
    max_depth: 5              # Maximum agent hierarchy depth
    max_children_per_node: 10 # Maximum subtasks per agent
    max_total_agents: 100     # Maximum total agents per run
    max_concurrent_workers: 5 # Concurrent worker executions
    llm_rate_limit_rpm: 60    # LLM requests per minute
```

#### Security Benchmark Settings

```yaml
security:
  enabled: true
  auto_detect: true           # Auto-detect CVE/security tasks
  default_model_poc: gpt-4o
  default_model_patch: claude-3-5-sonnet-20241022
  default_model_validation: gpt-4o-mini
  poc_temperature: 0.6
  patch_temperature: 0.5
  validation_temperature: 0.2
  max_poc_attempts: 3
  max_patch_attempts: 3
  docker_timeout: 300
```

#### Output Settings

```yaml
output:
  verbose: true               # Show progress during execution
  show_progress: true
  log_level: INFO             # DEBUG, INFO, WARNING, ERROR
  directory: ./output         # Output directory for results
```

### Environment Variables

Secrets are loaded from `.env` files:

```bash
# Required
POSTGRES_PASSWORD=your_secure_password
OPENAI_API_KEY=sk-your-openai-key

# Optional (Claude Code uses `claude login` for local auth)
ANTHROPIC_API_KEY=sk-ant-your-key

# Optional overrides
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_USER=arise
POSTGRES_DB=arise_events
ARISE_ENV=development        # development, production
```

---

## CLI Commands

### Run a Task

```bash
docker compose --profile local exec app python main.py run "Your task description"

# With custom config
docker compose --profile local exec app python main.py -c /path/to/config.yaml run "Task"
```

### List Past Runs

```bash
# Text format (default)
docker compose --profile local exec app python main.py list

# JSON format
docker compose --profile local exec app python main.py list --format json

# Limit results
docker compose --profile local exec app python main.py list --limit 5
```

### View Events

```bash
# Events for last run (JSON format)
docker compose --profile local exec app python main.py events

# Specific agent
docker compose --profile local exec app python main.py events --agent-id <UUID>

# Different formats: json, jsonl, text, compact
docker compose --profile local exec app python main.py events --format compact

# Errors only
docker compose --profile local exec app python main.py events --errors-only

# Write to file
docker compose --profile local exec app python main.py events -o events.json
```

### View Summary

```bash
# Summary for last run
docker compose --profile local exec app python main.py summary

# Specific agent
docker compose --profile local exec app python main.py summary --agent-id <UUID>

# Text or JSON format
docker compose --profile local exec app python main.py summary --format text
```

### Run Tests

```bash
docker compose --profile dev exec app-dev uv run pytest
docker compose --profile dev exec app-dev uv run pytest -v --tb=short
docker compose --profile dev exec app-dev uv run pytest core/domain/tests/
```

---

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
├── presentation/            # CLI, REST API
├── config/                  # YAML configuration files
├── prompts/                 # Jinja2 LLM prompt templates
└── deployment/              # Docker Compose, Dockerfile
```

## Documentation

Detailed documentation in [`agent-docs/`](agent-docs/):

| Document | Description |
|----------|-------------|
| [architecture-concepts.md](agent-docs/architecture-concepts.md) | CQRS, Event Sourcing, Hexagonal, DDD |
| [domain-model.md](agent-docs/domain-model.md) | Agent lifecycle, domain events |
| [execution-flow.md](agent-docs/execution-flow.md) | How tasks flow through the system |
| [project-structure.md](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [configuration.md](agent-docs/configuration.md) | Config files, secrets, environments |
| [api-reference.md](agent-docs/api-reference.md) | REST endpoints, SSE streaming |
| [development.md](agent-docs/development.md) | Testing, coding standards |
| [user-manual.md](agent-docs/user-manual.md) | Docker commands reference |

## Tech Stack

- **Runtime**: Python 3.12+, `uv`, `asyncpg`
- **LLM**: LiteLLM (multi-provider), Claude Code, OpenHands, Google ADK
- **Storage**: PostgreSQL (event store)
- **API**: FastAPI, SSE streaming

## License

[License information]
