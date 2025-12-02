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

## Design Principles & Philosophies

This project strictly adheres to industry-standard design principles from renowned software engineers and seminal books. **Always apply these principles when writing code.**

### 1. SOLID Principles (Robert C. Martin)

**Source:** Robert C. Martin (Uncle Bob), coined by Michael Feathers (2004)
**Reference:** [Clean Code](https://blog.cleancoder.com/uncle-bob/2020/10/18/Solid-Relevance.html)

- **S - Single Responsibility Principle (SRP):** A class should have one and only one reason to change.
- **O - Open-Closed Principle (OCP):** Software entities should be open for extension, but closed for modification.
- **L - Liskov Substitution Principle (LSP):** Objects should be replaceable with instances of their subtypes without altering correctness.
- **I - Interface Segregation Principle (ISP):** Clients should not be forced to depend upon interfaces they do not use.
- **D - Dependency Inversion Principle (DIP):** High-level modules should not depend on low-level modules; both should depend on abstractions.

**Application in this project:**
- Each aggregate (e.g., `AgentSession`) has a single responsibility (SRP)
- Ports enable extension without modifying core domain (OCP)
- All infrastructure implements port interfaces correctly (LSP)
- Narrow port interfaces like `LLMPort`, `EventStorePort` (ISP)
- Core depends on port abstractions, not concrete infrastructure (DIP)

### 2. DRY - Don't Repeat Yourself (Andy Hunt & Dave Thomas)

**Source:** Andy Hunt and Dave Thomas, *The Pragmatic Programmer* (1999)
**Reference:** [The Pragmatic Programmer](https://en.wikipedia.org/wiki/The_Pragmatic_Programmer)

**Definition:** "Every piece of knowledge must have a single, unambiguous, authoritative representation within a system."

**Application in this project:**
- Prompts are extracted to Jinja2 templates in `prompts/` directory (not duplicated in code)
- Event application logic centralized in `_apply()` method with singledispatchmethod
- Domain events defined once in `core/domain/events.py`
- Validation logic lives in one place (domain aggregates)

**Note:** DRY applies to **knowledge duplication**, not just code duplication. Duplicate code representing different knowledge is acceptable.

### 3. Test-Driven Development (Kent Beck)

**Source:** Kent Beck, *Test-Driven Development: By Example* (2002)
**Reference:** [Martin Fowler on TDD](https://martinfowler.com/bliki/TestDrivenDevelopment.html)

**Process: Red-Green-Refactor**
1. **Red:** Write a failing test for next functionality
2. **Green:** Write minimal code to make test pass
3. **Refactor:** Improve design without changing behavior

**Application in this project:**
- All features start with failing tests (e.g., `test_manager_decomposition()` before implementation)
- Tests use Given-When-Then (BDD) structure for clarity
- FakeLLM and other test doubles enable isolated unit testing
- Tests verify events, not just state (event sourcing compatibility)

### 4. Domain-Driven Design (Eric Evans)

**Source:** Eric Evans, *Domain-Driven Design: Tackling Complexity in the Heart of Software* (2003)
**Reference:** [DDD Reference](https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf)

**Core Concepts:**
- **Entities:** Objects with unique identity (e.g., `AgentSession` identified by `session_id`)
- **Value Objects:** Immutable objects defined by attributes (e.g., `DomainEvent` instances)
- **Aggregates:** Consistency boundary around entities (e.g., `AgentSession` is aggregate root)
- **Bounded Context:** Logical boundary where domain model is consistent

**Application in this project:**
- `AgentSession` is the aggregate root for agent state
- Domain events are value objects (frozen Pydantic models)
- Each aggregate enforces its own invariants via `_apply()` handlers
- Clear bounded context: multi-agent orchestration domain

### 5. Hexagonal Architecture / Ports & Adapters (Alistair Cockburn)

**Source:** Alistair Cockburn (2005), also called "Ports and Adapters"
**Reference:** [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture)

**Purpose:** "Create your application to work without either a UI or a database so you can run automated regression-tests against it, work when the database becomes unavailable, and link applications together without any source code dependencies."

**Core Components:**
- **Ports:** Define purposeful conversations (abstract interfaces using `Protocol`)
- **Adapters:** Glue between domain and external world (e.g., `PostgresEventStore`, `LiteLLMAdapter`)
- **Domain Core:** Pure business logic with NO infrastructure dependencies

**Application in this project:**
```
core/
  domain/           # Pure business logic (events.py, model.py, subtask.py, etc.)
  ports/            # Abstract interfaces (LLMPort, EventStorePort, WorkerToolPort)
  application/      # Use cases and orchestration (execution_service.py)
infrastructure/
  adapters/         # Concrete implementations (PostgresEventStore, LiteLLMAdapter, ClaudePTYAdapter)
  sql/              # Database schemas
bootstrap/          # Dependency injection and wiring
config/             # Environment-based configuration (YAML + pydantic-settings)
presentation/       # CLI interface
prompts/            # Jinja2 templates (hierarchical prompt composition)
  system/           # Role identity prompts (role_boss.j2, role_manager.j2, etc.)
  strategies/       # Operational methodology prompts
  tasks/            # Task-specific prompts
  output_formats/   # JSON schema prompts for structured output
```

**Strict Rule:** Core NEVER imports from infrastructure. Dependencies point inward.

### 6. Event Sourcing (Greg Young & Martin Fowler)

**Source:** Greg Young (2010), Martin Fowler
**Reference:** [Martin Fowler - Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html), [Greg Young - CQRS Documents](https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf)

**Definition:** "Capture all changes to an application state as a sequence of events." (Martin Fowler)

**Core Principles:**
- State is derived by replaying events, never stored directly
- Events are immutable facts that have occurred
- All state changes MUST create events
- Event store is append-only (never update/delete events)

**Application in this project:**
- `AgentSession` state reconstructed via `load_from_history()`
- `_apply()` is the ONLY place where state mutations occur
- All business operations (e.g., `evaluate_task()`) create events
- Events stored with sequence numbers for OCC

**Greg Young's Guidance on Failures:**
> "If a failure affects aggregate state, it should be an event, not an exception."

**Application:** LLM failures publish `WorkFailed` event (not ValueError) because:
- Failure affects state (ANALYZING → FAILED)
- Parent agents need to know about failure
- Event replay must include failures
- Analytics: track failure rates

### 7. CQRS - Command Query Responsibility Segregation (Greg Young)

**Source:** Greg Young (2010), described by Martin Fowler
**Reference:** [Martin Fowler - CQRS](https://martinfowler.com/bliki/CQRS.html)

**Definition:** "Use a different model to update information than the model you use to read information."

**Application in this project:**
- **Command side:** Domain methods (e.g., `assign_task()`, `evaluate_task()`) that create events
- **Query side:** Not yet implemented (future: read models, projections)
- Event store enables separate read/write models

**Martin Fowler's Caution:**
> "You should be very cautious about using CQRS. Many information systems fit well with the notion of an information base that is updated in the same way that it's read."

**Note:** CQRS fits naturally with Event Sourcing but adds complexity. Use when needed for scalability.

### 8. Design by Contract vs Defensive Programming (Bertrand Meyer)

**Source:** Bertrand Meyer, *Object-Oriented Software Construction* (1988), Eiffel language
**Reference:** [Design by Contract Chapter](https://se.inf.ethz.ch/~meyer/publications/old/dbc_chapter.pdf)

**Design by Contract Elements:**
- **Preconditions:** Requirements before method execution (caller's responsibility)
- **Postconditions:** Guarantees after method execution (callee's responsibility)
- **Invariants:** Constraints that always hold true for aggregate

**Contract vs Validation:**

| Aspect | Contract (DbC) | Validation |
|--------|---------------|------------|
| **Trust** | Both parties trust contract | Neither party trusts the other |
| **Location** | Heart of domain (aggregates) | Layer boundaries (controllers, APIs) |
| **Enforcement** | Assertions (fail fast) | Try-catch, error handling |
| **Source** | Internal callers (trusted) | External input (untrusted) |
| **Purpose** | Programmer error detection | User error handling |

**Application in this project:**

**Use Assertions (Contract) for:**
- Invariants in event store: "First event must be `AgentCreated`"
- Preconditions: `evaluate_task()` requires MANAGER role in ANALYZING status
- Internal method contracts between core components

**Use Validation (ValueError/Exceptions) for:**
- External LLM responses (untrusted)
- User input from APIs (future)
- Configuration files

**Example from codebase:**
```python
# Contract: Fail fast on programmer error
assert self.role == AgentRole.MANAGER, "evaluate_task requires MANAGER agent"

# Validation: Handle expected external failures as events
try:
    subtasks = json.loads(response)
except json.JSONDecodeError as e:
    # LLM is external/untrusted → publish WorkFailed event
    failed_event = WorkFailed(...)
```

**DDD + DbC Principle:**
> "Invariant enforcement is best performed by the thing that is being mutated (or created) itself, like a self-protection reflex." ([Stack Overflow: Invariants vs Validation](https://stackoverflow.com/questions/30190302/what-is-the-difference-between-invariants-and-validation-rules))

### 9. Offensive Programming (Fail Fast)

**Philosophy:** Code should "fail hard" with contract verification as safety net, differing from defensive programming where supplier handles all error cases.

**Application in this project:**
- Use assertions for preconditions/invariants
- Let system crash during development if contracts violated
- Production: Failed assertions indicate bugs, not runtime conditions

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

  - [x] Phase 1: Project Skeleton & Event Store (Postgres + OCC)
  - [x] Phase 2: Core Domain (AgentSession Aggregate & Events)
  - [x] Phase 3: LiteLLM Adapter & Manager Logic
  - [x] Phase 4: Claude Code PTY Adapter (Thinking Capture)

## Current Project Structure

```
arise-sec-lion/
├── core/                           # Domain core (NO infrastructure imports)
│   ├── domain/
│   │   ├── model.py               # AgentSession aggregate, AgentRole, AgentStatus enums
│   │   ├── events.py              # Domain events (AgentCreated, TaskAssigned, etc.)
│   │   ├── subtask.py             # Subtask value object
│   │   ├── agent_config.py        # LLMConfig, AgentConfig (per-operation/heuristic/hybrid)
│   │   ├── config_resolver.py     # Resolves config for specific operations
│   │   ├── prompt_builder.py      # Hierarchical prompt composition service
│   │   ├── services.py            # Domain services (SubtaskParser)
│   │   └── exceptions.py          # Custom domain exceptions
│   ├── ports/
│   │   ├── event_store_port.py    # EventStorePort Protocol
│   │   ├── llm_port.py            # LLMPort Protocol
│   │   └── worker_port.py         # WorkerToolPort Protocol (AsyncIterator[DomainEvent])
│   └── application/
│       ├── execution_service.py   # AgentExecutionService (The Brain)
│       └── dtos.py                # Data transfer objects
├── infrastructure/
│   ├── adapters/
│   │   ├── postgres_event_store.py  # PostgreSQL + asyncpg + OCC
│   │   ├── litellm_adapter.py       # Multi-provider LLM via litellm
│   │   ├── claude_pty_adapter.py    # Claude Code PTY wrapper (thinking capture)
│   │   └── openhands_adapter.py     # OpenHands adapter (planned)
│   └── sql/
│       └── create_events_table.sql  # Event store schema
├── prompts/                         # Jinja2 templates (hierarchical prompt structure)
│   ├── system/                      # Agent identity prompts
│   │   ├── role_boss.j2
│   │   ├── role_manager.j2
│   │   ├── role_pending.j2
│   │   └── role_worker.j2
│   ├── strategies/                  # Operational methodology
│   │   ├── boss_delegation.j2
│   │   ├── manager_decomposition.j2
│   │   └── complexity_evaluation.j2
│   ├── tasks/                       # Task-specific prompts
│   │   ├── task_decomposition.j2
│   │   └── complexity_evaluation.j2
│   └── output_formats/              # JSON schema for structured output
│       ├── subtask_list.j2
│       └── complexity_result.j2
├── bootstrap/                       # Dependency injection
├── config/                          # Environment configuration (YAML + pydantic-settings)
├── presentation/                    # CLI interface
└── tests/                           # Comprehensive test suite (87+ tests)
```

## Domain Events Reference

| Event | Description | Key Attributes |
|-------|-------------|----------------|
| `AgentCreated` | New agent session created | `role`, `parent_id`, `config` |
| `TaskAssigned` | Task assigned to agent | `task_description`, `constraints` |
| `StatusChanged` | Agent status transition | `old_status`, `new_status`, `reason` |
| `ComplexityEvaluated` | PENDING agent evaluates task | `complexity`, `determined_role`, `reasoning` |
| `SubtasksDefined` | MANAGER decomposes task | `subtasks: list[Subtask]` |
| `ChildSpawned` | Parent spawns child agent | `child_id`, `child_role`, `subtask`, `child_config` |
| `CodeGenerationStarted` | WORKER starts execution | `tool_name` |
| `ThoughtCaptured` | Worker tool emits thinking | `content`, `stream` |
| `ChildCompleted` | Child agent finishes | `child_id`, `result` |
| `WorkCompleted` | Agent completes successfully | `result` |
| `WorkFailed` | Agent fails | `reason` |

## Agent Lifecycle

```
                    ┌─────────────────────────────────────────┐
                    │              BOSS (root)                │
                    │  - Initiates task decomposition         │
                    │  - Only ONE in the system               │
                    └────────────────┬────────────────────────┘
                                     │ spawns children
                                     ▼
                    ┌─────────────────────────────────────────┐
                    │              PENDING                    │
                    │  - Evaluates task complexity via LLM    │
                    │  - Determines: SIMPLE or COMPLEX?       │
                    └────────────────┬────────────────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              │ SIMPLE                                      │ COMPLEX
              ▼                                             ▼
┌─────────────────────────┐               ┌─────────────────────────┐
│        WORKER           │               │        MANAGER          │
│  - Executes via tools   │               │  - Decomposes further   │
│  - Claude Code/OpenHands│               │  - Spawns PENDING       │
│  - Captures thinking    │               │  - Waits for children   │
└─────────────────────────┘               └─────────────────────────┘
```
