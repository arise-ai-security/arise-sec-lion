<!-- Read this when: writing any code in this repository -->
All code follows Python 3.12 conventions enforced by ruff (`ruff.toml` at repo root): 100-character line limit, double quotes, Google-style docstrings, 26 lint rule categories.

---

## Naming Conventions

| Entity | Convention | Example | Where |
|---|---|---|---|
| Module | `snake_case.py` | `agent_session.py`, `postgres_event_store.py` | All layers |
| Class | PascalCase + role suffix | `AgentOrchestrator`, `LiteLLMAdapter`, `SecurityDomainPlugin` | All layers |
| Protocol / port | PascalCase ending in `Port` | `EventStorePort`, `LLMPort`, `WorkerToolPort` | `core/ports/` only |
| Exception | PascalCase ending in `Error` | `DomainInvariantError`, `ConcurrencyError`, `LLMError` | `core/domain/exceptions.py` |
| Enum class | PascalCase | `AgentRole`, `AgentStatus` | `core/domain/values/enums.py` |
| Enum member | `UPPER_CASE` | `AgentRole.BOSS`, `AgentStatus.PENDING` | `core/domain/values/enums.py` |
| Value object | PascalCase, frozen Pydantic | `Subtask`, `HierarchyLimits`, `AgentConfig` | `core/domain/values/` |
| Domain event | PascalCase verb phrase | `AgentCreated`, `TaskAssigned`, `ChildSpawned`, `WorkCompleted` | `core/domain/events/events.py` |
| Function / method | `snake_case` | `assign_task`, `query_with_usage` | All layers |
| Private method | `_snake_case` | `_call_litellm`, `_deep_copy_value` | All layers |
| Constant | `UPPER_SNAKE_CASE` | `DEFAULT_WORKER_TOOL`, `EVENT_TYPE_REGISTRY` | Module level |
| Test function | `test_<descriptive_name>` | `test_boss_initialization_flow` | `**/tests/test_*.py` |
| Test fake/stub | `Fake` prefix + PascalCase | `FakeLLM`, `FakeWorkerTool` | Test files only |
| Test helper | `_snake_case` | `_config()`, `_make_worker_agent()` | Test files only |

### Class Suffix Conventions

Suffixes signal architectural role. Use them consistently so readers know a class's purpose at a glance.

| Suffix | Meaning | Example |
|---|---|---|
| `Port` | Protocol interface in `core/ports/` | `LLMPort`, `EventStoreReadPort`, `CostCalculatorPort` |
| `Adapter` | Infrastructure implementation of a port | `LiteLLMAdapter`, `PostgresEventStore` (historical exception) |
| `Service` | Application-layer service | `ExecutionService`, `AgentQueryService` |
| `Error` | Exception class | `LLMError`, `EventStoreError`, `InfeasibleError` |
| `Plugin` | Domain plugin implementation | `SecurityDomainPlugin` |
| `Strategy` | Strategy protocol or implementation | `PromptStrategy`, `SecBenchPromptStrategy` |

Do **not** invent new suffixes without a clear architectural reason. If a class does not fit any suffix above, it likely belongs in `core/domain/values/` as a plain value object (no suffix needed).

---

## Import Ordering

Ruff isort handles sorting automatically. The configured section order in `ruff.toml`:

1. `from __future__ import annotations` (future -- always first line when present)
2. Standard library (`uuid`, `logging`, `json`, `collections.abc`, `enum`)
3. Third-party (`pydantic`, `litellm`, `asyncpg`, `pytest`)
4. First-party (`core`, `infrastructure`, `config`)
5. Local-folder (relative imports: `from .application import ...`)

Two blank lines separate the last import block from the first line of code.

### Concrete Example

From `core/domain/aggregates/agent_session.py`:

```python
"""Core domain models for the multi-agent system."""

from __future__ import annotations

from functools import singledispatchmethod
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from core.domain.events.events import (
    AgentCreated,
    AgentExecutionFinished,
    ChildSpawned,
    DomainEvent,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import DomainInvariantError, InvalidEventHistoryError
from core.domain.values.agent_config import AgentConfig
from core.domain.values.enums import AgentRole, AgentStatus


if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.subtask import Subtask
```

### Import Rules

- `combine-as-imports = true`: merge multiple names from the same module into one `from X import A, B, C` line.
- `split-on-trailing-comma = true`: a trailing comma in the import list forces one-name-per-line formatting.
- First-party packages are `core`, `infrastructure`, `config` (defined in `ruff.toml` `known-first-party`).
- Use `from __future__ import annotations` in files with many cross-layer forward references (common in `bootstrap/` and `core/application/`). Not required in every file.
- Place the `if TYPE_CHECKING:` block after all runtime imports, separated by two blank lines. Types imported there are only available at type-check time.

### What NOT to Do

- Do not import from `infrastructure/` inside any `core/` file. The pre-commit hook `scripts/check_architecture_boundaries.py` rejects this.
- Do not use `from typing import Dict, List, Optional`. Use Python 3.12 builtins: `dict`, `list`, `X | None`.
- Do not use `from typing import AsyncIterator`. Use `from collections.abc import AsyncIterator`.

---

## Error Handling

### Domain Exceptions

All domain exceptions live in `core/domain/exceptions.py`. These are the established types:

| Exception | Base | When to raise |
|---|---|---|
| `DomainInvariantError` | `ValueError` | Aggregate or event invariant violated |
| `InvalidEventHistoryError` | `ValueError` | Event replay encounters malformed history |
| `LLMError` | `Exception` | Any LLM API failure (wraps provider errors) |
| `ConcurrencyError` | `Exception` | OCC conflict on event store write |
| `EventStoreError` | `Exception` | Database-level event store failure |
| `ToolNotAvailableError` | `Exception` | Requested worker tool not configured |
| `InfeasibleError` | `ValueError` | LLM declares task constraints unsatisfiable |
| `CostInvariantViolation` | `Exception` | Bug in cost calculation logic |

### Wrapping External Exceptions

Infrastructure adapters catch third-party library exceptions and re-raise them as domain exceptions. Always chain with `from e` and store `original_error`:

```python
# infrastructure/adapters/litellm_adapter.py

async def _call_litellm(self, model: str, **kwargs: Any) -> Any:
    try:
        return await litellm.acompletion(model=model, **kwargs)

    except litellm.exceptions.AuthenticationError as e:
        raise LLMError(
            f"LLM authentication failed for model '{model}': {e}",
            original_error=e,
        ) from e

    except litellm.exceptions.RateLimitError as e:
        raise LLMError(
            f"LLM rate limit exceeded for model '{model}': {e}",
            original_error=e,
        ) from e
```

### Exception Class Pattern

Wrapping exceptions follow this structure -- `original_error` attribute and a `__str__` that includes the causal chain:

```python
# core/domain/exceptions.py

class LLMError(Exception):
    """LLM operation failed (API error, rate limit, timeout)."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message
```

### Rules

- Never use bare `except:`. Always catch a specific exception type.
- Always chain with `from e` when re-raising as a different type.
- Catch the most specific exception first, then broader types.
- Domain exceptions that wrap external errors store the original via `original_error`.
- Exceptions with structured context (like `ConcurrencyError`) accept typed constructor arguments, not just a message string.

---

## Logging

Every module that logs declares a module-level logger immediately after imports:

```python
import logging

logger = logging.getLogger(__name__)
```

### Rules

- Always use `__name__` -- never hardcode a logger name like `logging.getLogger("my_module")`.
- Never use `print()` for any output. Use `logger.debug()`, `logger.info()`, `logger.warning()`, or `logger.error()`.
- Place `logger = logging.getLogger(__name__)` after all import blocks, before the `TYPE_CHECKING` block or the first class/function definition.
- For user-facing output, use the presentation layer renderers, not logging.

---

## Comments and Docstrings

### Module-Level Docstrings

Every `.py` file starts with a module-level docstring. One to three sentences describing purpose:

```python
"""Domain events for the multi-agent system (event sourcing)."""
```

```python
"""Runtime ports: protocols for LLM, worker, cost, context, and streaming.

Small port protocols consolidated into one file. EventStorePort stays
in its own file due to size and ISP split.
"""
```

### Class Docstrings

Public classes get a short docstring. One sentence is usually enough:

```python
class AgentSession:
    """Event-sourced aggregate for agent sessions. State derived from replaying events."""
```

### Method Docstrings

Add docstrings when the method's behavior is not obvious from its name and signature. Use Google-style (`Args:`, `Returns:`, `Raises:` sections):

```python
async def append_batch(self, events: list[DomainEvent], expected_version: int) -> None:
    """Append multiple events atomically in a single transaction.

    Significantly faster than individual appends for workers
    that produce many events (thoughts, tool uses, etc.).

    Args:
        events: List of events to persist.
        expected_version: Expected version before first event.
    """
```

### What NOT to Do

- Do not add boilerplate docstrings to trivial methods (e.g., `"""Gets the name."""` on `get_name()`).
- Do not comment *what* the code does. Comment *why* when reasoning is non-obvious.
- Do not leave `TODO`/`FIXME` without a clear explanation of what remains and why it was deferred.

---

## Type Annotations

### General Rules

- All public functions and methods must have return type annotations. Ruff `ANN` rules enforce this.
- Use Python 3.12 builtin syntax (enforced by `UP` rules):

```python
# Correct
def process(items: list[str], config: dict[str, Any]) -> str | None: ...

# Wrong -- do not use
def process(items: List[str], config: Dict[str, Any]) -> Optional[str]: ...
```

- Use `collections.abc.AsyncIterator` not `typing.AsyncIterator`.
- Test files are exempt from annotation requirements (`ANN` rules disabled in `**/tests/*.py`).

### `TYPE_CHECKING` Blocks

Use `TYPE_CHECKING` to import types needed only for annotations, avoiding circular imports and unnecessary runtime dependencies. This is the primary mechanism for cross-layer type references:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ports.event_store_port import EventStorePort
    from core.domain.values.limits import HierarchyLimits
```

When a type is imported under `TYPE_CHECKING` and the file does **not** have `from __future__ import annotations`, reference it as a string literal:

```python
def __init__(self, event_store: "EventStorePort") -> None:
    self._event_store = event_store
```

When the file has `from __future__ import annotations`, all annotations are strings automatically -- no quoting needed.

### Enum Typing

Enums that participate in serialization (stored in events, sent over API) extend `str, Enum` so their `.value` is a plain string:

```python
class AgentRole(str, Enum):
    BOSS = "boss"
    PENDING = "pending"
    MANAGER = "manager"
    WORKER = "worker"
```

---

## Value Objects and Data Classes

### Frozen Pydantic Models

Domain value objects and events use frozen Pydantic `BaseModel`. The `frozen=True` config prevents attribute reassignment after construction:

```python
class Subtask(BaseModel):
    """Immutable subtask with description, config, and optional scheduling metadata."""

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    config: dict[str, Any]
    depends_on: list[int] = Field(default_factory=list)
    estimated_complexity: Literal["simple", "complex", "unknown"] = "unknown"
    target_paths: tuple[str, ...] = Field(default=())
    symbols: tuple[str, ...] = Field(default=())
```

Domain events inherit from `DomainEvent` which has a `model_validator` that deep-copies all dict/list values on construction, ensuring true immutability even for mutable container fields:

```python
class DomainEvent(BaseModel):
    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _deep_copy_mutable_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: _deep_copy_value(v) for k, v in data.items()}
        return data
```

### Frozen Dataclasses

For simple immutable data bundles that do not need Pydantic validation or serialization, use `@dataclass(frozen=True, slots=True)`:

```python
@dataclass(frozen=True, slots=True)
class SubtaskScope:
    """Structured scoping info from parent's subtask decomposition."""

    target_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    search_hints: tuple[str, ...] = ()
```

### Rules

- Prefer `tuple[str, ...]` over `list[str]` for frozen model fields holding sequences. Tuples are inherently immutable; lists require deep-copy defense.
- Use `Field(default_factory=...)` for mutable default values (`dict`, `list`). Never use a mutable literal as a default.
- All Pydantic value objects and events must have `model_config = {"frozen": True}`.
- Enums that participate in serialization extend `str, Enum`.
- Use `model_copy(update={...})` (not direct mutation) to derive new instances from frozen models. See `HierarchyLimits.for_child()` in `core/domain/values/limits.py`.

---

## Test Conventions

### File Layout

Tests live next to the layer they test, in a `tests/` subdirectory:

| Layer | Test location |
|---|---|
| Domain (aggregates, values, services) | `core/domain/tests/test_*.py` |
| Application (orchestrator, services) | `core/application/tests/test_*.py` |
| Query (projections) | `core/query/tests/test_*.py` |
| Infrastructure (adapters) | `infrastructure/tests/test_*.py` |
| Presentation | `presentation/tests/test_*.py` |
| Bootstrap | `bootstrap/tests/test_*.py` |

### Given-When-Then Structure

Every test follows Given-When-Then with explicit section comments:

```python
def test_boss_initialization_flow() -> None:
    """Test that a BOSS agent is properly initialized with correct events."""

    # Given: Define UUID and configuration
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }

    # When: Create a new AgentSession
    agent = AgentSession.create(
        agent_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )

    # Then: Verify uncommitted events contain exactly 1 AgentCreated event
    assert len(agent.events) == 1
    assert isinstance(agent.events[0], AgentCreated)

    # And: Verify agent status is PENDING
    assert agent.status == AgentStatus.PENDING
```

Use `# And:` for additional assertions within the same section.

### Naming

- Test files: `test_<module_or_feature>.py` -- describe the subject, not the method.
- Test functions: `test_<descriptive_behavior>` -- describe the scenario, not the implementation.
- Helpers: private functions with `_` prefix (e.g., `_config()`, `_make_worker_agent()`).
- Fakes/stubs: `Fake` prefix, defined in the test file (e.g., `FakeLLM`, `FakeWorkerTool`).

### Async Tests

Mark async tests with `@pytest.mark.asyncio`:

```python
@pytest.mark.asyncio
async def test_litellm_adapter_successful_query() -> None:
    ...
```

### Relaxed Lint Rules in Tests

These ruff rules are disabled in test files (`**/tests/*.py`, `**/tests/test_*.py`, `**/tests/conftest.py`):

- `S101` -- `assert` is allowed.
- `ANN` -- type annotations not required.
- `PLR2004` -- magic values allowed.

### Rules

- Prefer inline `Fake*` classes over complex mock setups. Define them in the test file when they need behavior beyond simple return values.
- Use `unittest.mock.patch` for patching third-party library calls (e.g., `litellm.acompletion`).
- One logical scenario per test function. Multiple assertions within a scenario are fine when grouped under `# Then:` / `# And:`.
