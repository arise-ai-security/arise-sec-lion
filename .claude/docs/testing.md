<!-- Read this when: writing or modifying tests -->
This guide covers the test framework, conventions, file locations, test doubles, and templates for arise-sec-lion.

## Test Strategy

The project uses **pytest** with **pytest-asyncio** (auto mode) and **hypothesis** (property-based testing). Every architectural layer has its own `tests/` directory. Tests follow a strict **Given-When-Then** style with explicit section comments.

Coverage is tracked at the branch level with a **70% failure threshold**. Source directories: `core`, `infrastructure`, `bootstrap`, `presentation`.

## Running Tests

| Command | Description |
|---|---|
| `uv run pytest` | Full suite |
| `uv run pytest core/domain/tests/` | Single layer |
| `uv run pytest core/domain/tests/test_agent_session.py` | Single file |
| `uv run pytest core/domain/tests/test_agent_session.py::test_boss_initialization_flow` | Single test |
| `uv run pytest -k test_boss_initialization` | Match test by name substring |
| `uv run pytest -m integration` | Integration tests only |
| `uv run pytest -m property` | Property-based tests only |
| `uv run pytest -m e2e` | End-to-end tests only |
| `uv run pytest -m "not e2e"` | Exclude a marker |
| `uv run pytest -x` | Stop on first failure |
| `uv run pytest --cov --cov-report=term-missing` | With coverage report |
| `uv run pytest --cov --cov-report=html` | With HTML coverage |

Default options from `pyproject.toml`: `-v --tb=short`.

## Test File Locations

Each layer owns its tests. Place new test files in the matching layer's `tests/` directory.

| Layer | Test directory | What to test |
|---|---|---|
| Domain aggregates | `core/domain/tests/` | Aggregate behavior, event replay, value objects, domain services, parsers |
| Application services | `core/application/tests/` | Orchestrator logic, execution service, prompt parsing, architecture boundaries |
| CQRS projections | `core/query/tests/projections/` | Projection calculations, sink formatting, property-based invariants |
| Infrastructure | `infrastructure/tests/` | Adapter integration (event store, LLM, worker tools, sinks) |
| Presentation | `presentation/tests/` | CLI commands, formatters, renderers |
| Bootstrap | `bootstrap/tests/` | Composition wiring, E2E flows, characterization tests |
| Security plugin | `plugins/security/tests/` | CVE inference, container lifecycle, security prompt building |

Naming convention: `test_<module_or_concept>.py`. Each test directory has an `__init__.py`.

## Async Tests

`asyncio_mode = "auto"` is configured in `pyproject.toml`. You do **not** need the `@pytest.mark.asyncio` decorator. Any `async def test_*` function is automatically recognized as async.

Some existing tests in `infrastructure/tests/` use the explicit `@pytest.mark.asyncio` decorator. Both styles work; omitting it is preferred for new tests.

```python
# Correct -- no decorator needed
async def test_event_store_appends_event(event_store):
    event = make_event()
    await event_store.append(event, expected_version=0)
    events = await event_store.get_events(event.aggregate_id)
    assert len(events) == 1
```

## Given-When-Then Style (Mandatory)

Every test must use explicit `# Given:`, `# When:`, `# Then:` section comments. Use `# And:` for additional assertions within a section.

### Real Example: Domain Aggregate Test

From `core/domain/tests/test_agent_session.py`:

```python
from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import AgentCreated


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
    assert len(agent.events) == 1, "Agent should have exactly 1 uncommitted event"

    first_event = agent.events[0]
    assert isinstance(first_event, AgentCreated), "First event must be AgentCreated"
    assert first_event.aggregate_id == agent_id
    assert first_event.role == AgentRole.BOSS.value

    # And: Verify agent status is PENDING
    assert agent.status == AgentStatus.PENDING
```

Key conventions:
- Return type annotation `-> None` on every test function.
- `# Given:`, `# When:`, `# Then:`, `# And:` comments to structure test phases.
- Assertion messages on non-obvious checks.
- Group related tests in classes when testing a single unit with multiple scenarios.

### Template: New Test File

```python
"""Tests for <module under test>."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession


def test_<behavior_being_tested>() -> None:
    """<One-line description of what is tested>."""

    # Given: <setup description>

    # When: <action description>

    # Then: <expected outcome>
    assert ...
```

### Template: Application Service Test (with AsyncMock)

```python
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.domain.aggregates.agent_session import AgentRole
from core.domain.events.events import AgentCreated, TaskAssigned
from core.domain.values.llm_response import LLMResponse, LLMUsage


def _make_llm_response(content: str, model: str = "gpt-4") -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        model=model,
        cost_usd=0.0,
    )


@pytest.fixture
def mock_event_store():
    return AsyncMock()


@pytest.fixture
def mock_llm_port():
    return AsyncMock()


async def test_pending_agent_evaluates_complexity(
    execution_service, mock_event_store, mock_llm_port
) -> None:
    """Test that PENDING agent calls evaluate_complexity."""

    # Given: A PENDING agent in ANALYZING status
    agent_id = uuid4()
    mock_event_store.get_events.return_value = [
        AgentCreated(
            aggregate_id=agent_id, sequence_number=1,
            role=AgentRole.PENDING.value, parent_id=uuid4(), config={"model": "gpt-4"},
        ),
        TaskAssigned(
            aggregate_id=agent_id, sequence_number=2,
            task_description="Build a web scraper",
        ),
    ]
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should have been called for complexity evaluation
    assert mock_llm_port.query_with_usage.called
```

### Template: Infrastructure Integration Test

Infrastructure tests that need external services must skip gracefully when the service is unavailable.

```python
import os

import pytest

from core.domain.events.events import AgentCreated
from infrastructure.adapters.postgres_event_store import PostgresEventStore

TEST_DB_URL = os.environ.get("TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL environment variable not set"
)


@pytest.fixture
async def event_store():
    store = PostgresEventStore(TEST_DB_URL)
    await store.connect()
    await store.initialize_schema()
    yield store
    if store.pool:
        async with store.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS events")
    await store.disconnect()


async def test_append_and_retrieve(event_store) -> None:
    # Given: A domain event
    event = AgentCreated(aggregate_id=uuid4(), sequence_number=1, role="BOSS")

    # When: Append and retrieve
    await event_store.append(event, expected_version=0)
    events = await event_store.get_events(event.aggregate_id)

    # Then: Should return the event
    assert len(events) == 1
    assert isinstance(events[0], AgentCreated)
```

Run infrastructure tests with:

```bash
# Start test database
docker compose -f docker-compose.test.yml up -d

# Run with database URL
TEST_DB_URL="postgresql://testuser:testpass@localhost:5433/testdb" \
    uv run pytest infrastructure/tests/test_event_store.py -v

# Stop database
docker compose -f docker-compose.test.yml down -v
```

## Test Doubles

### Preference Order

1. **In-memory fakes** -- preferred for most tests. Implement the same Protocol interface as the real adapter.
2. **AsyncMock** -- use when the service under test has many collaborators and faking all of them would be excessive.

### Existing Fakes

| Fake | Location | Replaces |
|---|---|---|
| `FakeEventStore` | `core/query/tests/projections/conftest.py` | `PostgresEventStore` (simplified, for projections) |
| `InMemoryEventStore` | `bootstrap/tests/test_characterization.py` | `PostgresEventStore` (full OCC, for characterization) |
| `FakeLLMPort` | `bootstrap/tests/conftest.py` | `LiteLLMAdapter` |
| `FakeWorkerToolPort` | `bootstrap/tests/conftest.py` | Worker adapters (Claude SDK, OpenHands, etc.) |
| `FakeExecutionService` | `presentation/tests/conftest.py` | `AgentExecutionService` |
| `FakeStringSink` | `core/query/tests/projections/conftest.py` | Sink adapters |
| `FakeSharedContextPort` | `bootstrap/tests/test_characterization.py` | Shared context adapter |
| `MockLLMPort` | `core/application/tests/test_tool_calling.py` | LLM with scripted tool-call responses |
| `MockReconToolPort` | `core/application/tests/test_tool_calling.py` | Recon tool with canned results |

### Writing a New Fake

Fakes should implement the same `Protocol` from `core/ports/`. Keep them minimal -- only implement the methods your tests actually call.

```python
from uuid import UUID

from core.domain.events.events import DomainEvent


class FakeEventStore:
    def __init__(self) -> None:
        self._events: dict[UUID, list[DomainEvent]] = {}

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        agent_id = event.aggregate_id
        if agent_id not in self._events:
            self._events[agent_id] = []
        self._events[agent_id].append(event)

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        return self._events.get(aggregate_id, [])
```

## Property-Based Testing (Hypothesis)

Use hypothesis for testing invariants that must hold across all inputs. Mark tests with `@pytest.mark.property`.

Good candidates for property tests:
- Cost sums equal their parts
- Token counts are non-negative
- Projection idempotence (same events produce same result)
- Parser robustness against arbitrary input
- Additivity (combined events cost == sum of individual costs)

### Example: Cost Invariant

From `core/query/tests/projections/test_summary_properties.py`:

```python
from hypothesis import given, settings, strategies as st

from core.query.projections.impl import SummaryProjection


# Reusable strategies
model_names = st.sampled_from(["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-20241022"])
token_count = st.integers(min_value=0, max_value=1_000_000)
cost_usd = st.floats(
    min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6))


@st.composite
def cost_event_sequence(draw):
    """Generate structurally valid event sequences (agents created before costs)."""
    num_agents = draw(st.integers(min_value=1, max_value=5))
    agent_pool = [uuid4() for _ in range(num_agents)]
    events = []
    for agent_id in agent_pool:
        events.append(AgentCreated(aggregate_id=agent_id, sequence_number=1, role=draw(agent_roles)))
    num_cost_events = draw(st.integers(min_value=0, max_value=20))
    for _ in range(num_cost_events):
        agent_id = draw(st.sampled_from(agent_pool))
        events.append(draw(tokens_consumed_event(agent_id=agent_id)))
    return events


@pytest.mark.property
class TestSummaryProjectionCostProperties:

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_total_cost_equals_sum_of_parts(self, events: list[DomainEvent]) -> None:
        """Property: total_cost_usd == llm_cost_usd + worker_cost_usd."""
        projection = SummaryProjection()
        summary = projection.project(events)
        if summary.cost is None:
            return
        expected = summary.cost.llm_cost_usd + summary.cost.worker_cost_usd
        assert abs(summary.cost.total_cost_usd - expected) < 1e-6
```

Key patterns:
- `@st.composite` for building event strategies that maintain structural invariants (agents created before their cost events).
- `@settings(deadline=None)` to prevent flaky failures from slow hypothesis examples.
- `max_examples=50-100` for a good balance of coverage vs. speed.
- Early return for empty/None cases rather than conditional assertions.

## Markers

| Marker | Description | When to use |
|---|---|---|
| `e2e` | End-to-end tests requiring full infrastructure | Tests that spin up Postgres + real service wiring |
| `integration` | Integration tests requiring external services | Tests against real Postgres, LLM APIs, etc. |
| `property` | Property-based tests (hypothesis) | Tests using `@given` decorator |

Unmarked tests are pure unit tests that run with no external dependencies. Define markers in `pyproject.toml` under `[tool.pytest.ini_options]`.

## Architecture Boundary Test

`core/application/tests/test_architecture_boundaries.py` verifies that `core/` never imports from `infrastructure/`. This runs the same check as the pre-commit hook:

```python
from scripts.check_architecture_boundaries import find_boundary_violations

def test_repository_has_no_boundary_violations() -> None:
    assert find_boundary_violations() == []
```

If you add a new module to `core/`, this test catches accidental infrastructure imports.

## Coverage

Configuration from `pyproject.toml`:

```toml
[tool.coverage.run]
source = ["core", "infrastructure", "bootstrap", "presentation"]
branch = true
omit = ["*/tests/*", "*/__pycache__/*"]

[tool.coverage.report]
fail_under = 70
show_missing = true
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

- **Branch coverage** is enabled -- both sides of conditionals must be exercised.
- **Fail threshold**: 70%. CI fails if coverage drops below this.
- Lines matching `pragma: no cover`, `if TYPE_CHECKING:`, or `raise NotImplementedError` are excluded.

## Ruff Relaxations in Tests

These rules are disabled for files matching `**/tests/*.py`, `**/tests/test_*.py`, and `**/tests/conftest.py` (configured in `ruff.toml`):

| Rule | Description | Why disabled |
|---|---|---|
| `S101` | Use of `assert` | Tests use assert for assertions |
| `ANN` | Type annotation requirements | Test functions don't need full annotations |
| `PLR2004` | Magic values in comparisons | Test assertions frequently compare literal values |

## Fixtures

### Scope

- Use **function scope** (default) for all fixtures unless you have a compelling reason.
- Use **session scope** only for expensive setup like event loops (`bootstrap/tests/conftest.py`).

### Placement

- Place fixtures in `conftest.py` within the test directory when shared across multiple test files.
- Place fixtures directly in the test file when used only by that file.
- Never import fixtures from one layer's tests into another.

### conftest.py Locations

| File | Provides |
|---|---|
| `core/query/tests/projections/conftest.py` | Fixed UUIDs (`BOSS_ID`, `MANAGER_ID`, etc.), event factory fixtures, `FakeEventStore`, `FakeStringSink` |
| `presentation/tests/conftest.py` | `FakeExecutionService`, pre-wired CLI instances (`cli_with_fake_service`) |
| `bootstrap/tests/conftest.py` | `FakeLLMPort`, `FakeWorkerToolPort`, E2E infrastructure config, `e2e_cli` |

## Pre-Commit Hooks

Defined in `.pre-commit-config.yaml` (10 hooks total). Relevant to testing:

| Hook | Effect |
|---|---|
| `check-architecture-boundaries` | Fails if `core/` imports `infrastructure/` |
| `ruff` | Lint check with autofix |
| `ruff-format` | Format check |
| `trailing-whitespace` | Strips trailing whitespace |
| `end-of-file-fixer` | Ensures files end with a newline |
| `check-yaml` | Validates YAML syntax |
| `check-added-large-files` | Blocks large binary files |
| `check-merge-conflict` | Catches unresolved merge markers |
| `check-toml` | Validates TOML syntax |
| `mixed-line-ending` | Catches mixed line endings |

## Checklist for New Tests

1. Place the test file in the correct layer's `tests/` directory.
2. Use `# Given:` / `# When:` / `# Then:` / `# And:` section comments.
3. Prefer in-memory fakes over `AsyncMock` when practical.
4. Add `-> None` return type annotation to test functions.
5. For infrastructure tests, use `pytest.mark.skipif` when external services are required.
6. For property tests, use `@pytest.mark.property` and `@settings(deadline=None)`.
7. Do not add `@pytest.mark.asyncio` to async tests -- `asyncio_mode = "auto"` handles it.
8. Keep test names descriptive: `test_<unit>_<scenario>_<expected_outcome>`.
9. Use assertion messages on non-obvious checks.
