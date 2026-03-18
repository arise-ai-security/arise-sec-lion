# Development Guide

## Running Tests

```bash
cd deployment

# All tests
docker compose --profile local exec app uv run pytest

# Verbose
docker compose --profile local exec app uv run pytest -v --tb=short

# Specific file
docker compose --profile local exec app uv run pytest core/domain/tests/test_agent_session.py

# Pattern match
docker compose --profile local exec app uv run pytest -k "test_agent"
```

## Coding Standards

- **Type hints** mandatory on all functions
- **Async/await** for all ports and adapters
- **Docstrings** on public classes and methods
- **Tests** follow Given-When-Then structure

## Adding Features

### New Domain Event

1. Define in `core/domain/events/events.py` (frozen Pydantic model extending `DomainEvent`)
2. Add handler in `agent_session.py` via `@_apply.register`
3. Write tests

### New Port

1. Define protocol in `core/ports/runtime_ports.py` (or new file if large)
2. Create adapter in `infrastructure/adapters/`
3. Wire in `bootstrap/infrastructure.py`

### New API Endpoint

1. Add route in `query/api/routes/`
2. Add schema in `query/api/schemas.py`
3. Add TypeScript types in `query/web/src/types/api.ts`
4. Add client function in `query/web/src/api/client.ts`

## Prompt Parser

`core/application/services/prompt_parser.py` — extracts XML-tagged sections from prompts with provenance classification.

**Update `PROVENANCE_RULES` when**: adding new context tags to templates or changing tag semantics.

| Provenance | Convention | Examples |
|------------|------------|---------|
| TEMPLATE | UPPERCASE tags | `<ROLE>`, `<TASK>` |
| PARENT | lowercase with `-` | `<parent-context>`, `<ancestry>` |
| SIBLING | lowercase with `-` | `<sibling-tasks>` |
| CHILDREN | lowercase with `-` | `<child-outcomes>` |
| SHARED | lowercase with `-` | `<global-context>`, `<shared-decisions>` |
| SYSTEM | lowercase with `-` | `<cve-info>`, `<workspace>` |

Frontend flow: `PromptSent event → PromptParser.parse() → ParsedPromptSchema → React components`

## Pre-Commit (run by user, not Claude)

```bash
uv run ruff check .
uv run ruff format . --check
uv run pytest
grep -r "from infrastructure" core/   # Should return nothing
```
