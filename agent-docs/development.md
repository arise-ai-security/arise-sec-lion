# Development Guide

This document covers testing, coding standards, and development workflow.

---

## Running Tests

All commands run inside Docker containers. **Always use `--profile local` unless explicitly told otherwise.**

```bash
cd deployment

# Run all tests
docker compose --profile local exec app uv run pytest

# Verbose with short traceback
docker compose --profile local exec app uv run pytest -v --tb=short

# Run specific test file
docker compose --profile local exec app uv run pytest core/domain/tests/test_agent_session.py

# Run tests matching pattern
docker compose --profile local exec app uv run pytest -k "test_agent"
```

---

## Coding Standards

### Type Hints

Mandatory on all functions:

```python
# Good
async def load(self, agent_id: UUID) -> AgentSession:
    ...

# Bad - missing types
async def load(self, agent_id):
    ...
```

### Async/Await

All ports and adapters use async:

```python
# Port definition
class EventStorePort(Protocol):
    async def append(self, event: DomainEvent, expected_version: int) -> None: ...

# Adapter implementation
class PostgresEventStore(EventStorePort):
    async def append(self, event: DomainEvent, expected_version: int) -> None:
        async with self._pool.acquire() as conn:
            ...
```

### Docstrings

Required on public classes and methods:

```python
class AgentSession:
    """Event-sourced aggregate for agent sessions.

    State is derived from replaying events, never stored directly.
    """

    def assign_task(self, task_description: str) -> None:
        """Assign task, transitions to ANALYZING status."""
        ...
```

### Test Structure (Given-When-Then)

```python
def test_agent_completes_when_all_children_done():
    # Given: Manager with two children
    manager = create_manager_with_children(2)

    # When: Both children complete
    manager.handle_child_update(child1_id, "result1")
    manager.handle_child_update(child2_id, "result2")

    # Then: Manager is completed
    assert manager.status == AgentStatus.COMPLETED
    assert "result1" in manager.result
    assert "result2" in manager.result
```

---

## Pre-Commit Checklist

Before committing (run by user, not Claude):

```bash
# Lint check
uv run ruff check .

# Format check
uv run ruff format . --check

# All tests pass
uv run pytest

# No dependency violations
grep -r "from infrastructure" core/
# Should return nothing
```

---

## Adding New Features

### Adding a New Domain Event

1. Define event in `core/domain/events/events.py`:
   ```python
   class NewEventName(DomainEvent):
       """Description of when this event occurs."""
       field_name: str
   ```

2. Add handler in `core/domain/aggregates/agent_session.py`:
   ```python
   @_apply.register
   def _(self, event: NewEventName) -> None:
       # Update state
       self.version += 1
   ```

3. Write tests in `core/domain/tests/`

### Adding a New Port

1. Define interface in `core/ports/`:
   ```python
   class NewPort(Protocol):
       async def do_something(self, param: str) -> Result: ...
   ```

2. Create adapter in `infrastructure/adapters/`:
   ```python
   class ConcreteAdapter(NewPort):
       async def do_something(self, param: str) -> Result:
           # Implementation
   ```

3. Wire in `bootstrap/infrastructure.py`

### Adding an API Endpoint

1. Add route in `query/api/routes/`
2. Add schema in `query/api/schemas.py`
3. Add TypeScript types in `query/web/src/types/api.ts`
4. Add client function in `query/web/src/api/client.ts`

---

## Common Patterns

### Loading an Agent

```python
# Via repository (handles not-found)
agent = await repository.load(agent_id)

# Via event store (manual replay)
events = await event_store.get_events(agent_id)
agent = AgentSession.load_from_history(events)
```

### Persisting Events with OCC

```python
current_version = agent.version

# Make changes (appends to agent.events)
agent.assign_task("Do something")

# Persist with version check
for event in agent.events:
    await event_store.append(event, expected_version=current_version)
    current_version += 1

agent.mark_changes_as_committed()
```

### Parallel Async Operations

```python
# Use asyncio.gather to avoid N+1 queries
results = await asyncio.gather(*[
    fetch_child_status(child_id)
    for child_id in child_ids
])
```

---

## Prompt Parser

**Location**: `core/application/services/prompt_parser.py`

Extracts XML-tagged sections from prompts and classifies their **provenance** (origin) for frontend display.

### When to Update

Update `PROVENANCE_RULES` in the parser when:
- **Adding new context fields** to prompts (new tags in templates, `SpawnPayload`, or `SharedExecutionContext`)
- **Changing tag semantics** (e.g., moving a section from parent-provided to system-provided)

**No update needed** for:
- Changing content within existing tags
- Modifying template logic that doesn't add/remove XML tags

### Provenance Types

| Provenance | Convention | Examples |
|------------|------------|----------|
| TEMPLATE | UPPERCASE tags | `<ROLE>`, `<TASK>`, `<DECISION_GUIDE>` |
| PARENT | lowercase with `-` | `<parent-context>`, `<ancestry>` |
| SIBLING | lowercase with `-` | `<sibling-tasks>`, `<coworker_knowledge>` |
| CHILDREN | lowercase with `-` | `<child-outcomes>` |
| SHARED | lowercase with `-` | `<global-context>`, `<shared-decisions>` |
| SYSTEM | lowercase with `-` | `<cve-info>`, `<workspace>`, `<hierarchy-limits>` |

### Frontend Display

```
PromptSent Event → PromptParser.parse() → ParsedPromptSchema → React Components
```

- **API**: `GET /trace/{root_id}` returns parsed prompts with sections grouped by provenance
- **UI**: `ParsedPromptCard` shows prompts with color-coded sections (blue=template, green=parent, yellow=sibling, etc.)
- **Views**: Toggle between "Highlighted" (inline colored sections) and "Raw" (full text)
