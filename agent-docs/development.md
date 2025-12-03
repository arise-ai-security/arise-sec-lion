# Development Guide

## Environment Setup

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Runtime |
| `uv` | Latest | Package manager (**not pip/poetry**) |
| Docker | Latest | PostgreSQL, deployment |
| `ruff` | Via uv | Linting & formatting |

## Quick Commands

### Dependencies

```bash
uv sync                      # Install all dependencies
uv add <package>             # Add runtime dependency
uv add --dev <package>       # Add dev dependency
```

### Testing

```bash
uv run pytest                           # Run all tests
uv run pytest tests/path/to/test.py     # Run specific test
uv run pytest -v --tb=short             # Verbose with short traceback
uv run pytest -k "test_manager"         # Run tests matching pattern
```

### Linting & Formatting

```bash
uv run ruff check .          # Lint (pre-commit runs this)
uv run ruff format .         # Format code
uv run ruff check . --fix    # Auto-fix linting issues
```

### Running the System

```bash
# Start PostgreSQL
cd deployment && docker compose up -d db

# Set API keys
export OPENAI_API_KEY="sk-..."           # For OpenHands (default)
export ANTHROPIC_API_KEY="sk-ant-..."    # For Claude Code

# Run a task
uv run python main.py run "Your task description"

# View results
uv run python main.py events             # Event log
uv run python main.py summary            # Summary stats
uv run python main.py list               # Past runs
```

## Infrastructure (Docker)

### Development Database

```bash
# Using docker-compose (recommended)
cd deployment
cp .env.example .env   # Edit with your settings
docker compose up -d db

# Or standalone
docker run --name arise-db \
  -e POSTGRES_USER=arise \
  -e POSTGRES_PASSWORD=arise \
  -e POSTGRES_DB=arise_events \
  -p 5432:5432 \
  -d postgres:16-alpine
```

### Docker Files

| File | Purpose |
|------|---------|
| `deployment/docker-compose.yml` | Development setup |
| `deployment/docker-compose.test.yml` | CI/testing |
| `deployment/docker-compose.prod.yml` | Production |
| `deployment/.env.example` | Environment template |

## Coding Conventions

### Type Hints (Mandatory)

All functions must have strict type hints:
```python
async def create_agent(
    self,
    role: AgentRole,
    parent_id: UUID | None = None
) -> AgentSession:
```

### Async/Await

The system is fully asynchronous. All ports and adapters use `async/await`:
- `core/ports/` - All methods are `async`
- `infrastructure/adapters/` - All implementations are `async`

### Docstrings (Required)

All public modules, classes, and methods need docstrings:
```python
def evaluate_task(self, llm_port: LLMPort) -> None:
    """Decompose task into subtasks using LLM.

    Args:
        llm_port: LLM adapter for reasoning.

    Raises:
        AssertionError: If agent is not MANAGER role.
    """
```

### Error Handling

- Custom exceptions: `core/domain/exceptions.py`
- Domain errors → Domain exceptions or events
- Infrastructure errors → Adapter-specific exceptions
- Use `WorkFailed` event for LLM/tool failures (see event-sourcing.md)

### Configuration

- Secrets: Environment variables (`.env`)
- Settings: YAML files (`config/default.yaml`)
- Loading: `pydantic-settings` in `config/settings.py`

## Test Structure

```
tests/                          # Integration tests
  core/                         # Core domain tests
  infrastructure/               # Adapter tests

core/domain/tests/              # Domain unit tests
core/application/tests/         # Application layer tests
presentation/tests/             # CLI tests
```

### Test Conventions

- **Given-When-Then** structure (BDD)
- Use test doubles: `FakeLLM`, `FakeEventStore`
- Verify events, not just state
- Mark integration tests: `@pytest.mark.integration`

## Verification Checklist

Before committing:
```bash
uv run ruff check .              # No linting errors
uv run ruff format . --check     # Formatting correct
uv run pytest                    # All tests pass
grep -r "from infrastructure" core/  # No dependency violations (should be empty)
```
