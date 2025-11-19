# CLAUDE.md - Recursive Multi-Agent System Context

## Project Overview
A recursive, self-healing multi-agent orchestration platform (Boss -> Manager -> Worker) built with strict **Hexagonal Architecture (Ports & Adapters)** and **Event Sourcing**.
- **Goal:** Recursively decompose tasks and execute them using "Black Box" tools (Claude Code, OpenHands) while capturing their inner thinking process.
- **Key Tech:** Python 3.12+, `uv`, `asyncpg`, `litellm`, `pydantic`.

## Architectural Constraints (Strict Enforcement)
1.  **Dependency Rule:** `core/` must NEVER depend on `infrastructure/`.
    - `core/domain`: Pure business logic and data classes (Pydantic). NO external imports (no SQL, no HTTP).
    - `core/ports`: Abstract interfaces (`typing.Protocol`).
    - `infrastructure/`: Concrete implementations (PostgreSQL, CLI wrappers, LiteLLM).
2.  **Event Sourcing:**
    - State is derived *only* by replaying events.
    - **Optimistic Concurrency Control (OCC)** is mandatory for all DB writes (`expected_version`).
    - Never mutate state in the DB directly; always append an event.
3.  **Worker Capture:**
    - External tools (Claude Code/OpenHands) must be wrapped in **PTY Adapters** to capture real-time stdout/stderr "thinking" logs.

## Development Environment
- **Package Manager:** `uv` (Do not use pip/poetry commands directly).
- **Python Version:** 3.12+
- **Linter/Formatter:** `ruff` (via `uv run ruff check .`)

## Common Commands

### Lifecycle & Dependencies
- **Install Dependencies:** `uv sync`
- **Add Package:** `uv add <package_name>`
- **Add Dev Package:** `uv add --dev <package_name>`

### Testing & Quality
- **Run All Tests:** `uv run pytest`
- **Run Specific Test:** `uv run pytest tests/path/to/test.py`
- **Lint:** `uv run ruff check .`
- **Format:** `uv run ruff format .`

### Infrastructure (Docker)
- **Start Event Store (Postgres):**
  ```bash
  docker run --name event-store \
    -e POSTGRES_PASSWORD=secret \
    -e POSTGRES_DB=agent_events \
    -p 5432:5432 \
    -d postgres:16-alpine
    ```

  - **Stop Event Store:** `docker stop event-store`

## Coding Standards

1.  **Typing:** All functions must have strict type hints. Use `typing.Optional`, `typing.List`, etc., or standard collections in 3.12+.
2.  **Async:** The system is fully asynchronous. Use `async/await` for all Ports and Adapters.
3.  **Docstrings:** Required for all public modules, classes, and methods.
4.  **Error Handling:** Custom exceptions should live in `core/domain/exceptions.py`.
5.  **Configuration:** Use `pydantic-settings` for loading config from environment variables.

## Implementation Roadmap Status

  - [ ] Phase 1: Project Skeleton & Event Store (Postgres + OCC)
  - [ ] Phase 2: Core Domain (AgentSession Aggregate & Events)
  - [ ] Phase 3: LiteLLM Adapter & Manager Logic
  - [ ] Phase 4: Claude Code PTY Adapter (Thinking Capture)
