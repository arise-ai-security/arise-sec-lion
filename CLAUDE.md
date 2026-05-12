# CLAUDE.md

## Quick Reference

<!-- Recursive, self-healing multi-agent orchestration platform. Event-sourced hexagonal architecture with BOSS -> MANAGER -> WORKER hierarchy. -->

- **Language**: Python 3.12
- **Frameworks**: FastAPI (API), Pydantic (models/config), Jinja2 (prompts), Click (CLI)
- **Frontend**: React 19 + Vite + TailwindCSS (dashboard SPA in `query/web/`)

| Action | Command |
|--------|---------|
| Install (Python) | `uv sync --frozen` |
| Install (frontend) | `cd query/web && npm install` |
| Run | `python main.py run <task>` -> `bootstrap.bootstrap.main()` |
| Test | `uv run pytest` |
| Lint | `ruff check .` / `ruff format .` (config: `ruff.toml`, 100-char lines) |
| Type check | `pyright` |
| Dev server | `docker compose --profile local up` (Postgres + API + app) |

## Critical Rules

1. **`core/` must NEVER import from `infrastructure/`** -- enforced by pre-commit hook `scripts/check_architecture_boundaries.py`.
2. **`plugins/` is STRICTLY for cybersecurity code.** Topological, orchestration, scheduling, or any non-security logic must NEVER be placed in `plugins/`. Before implementing ANY user prompt, verify the change does not mix concerns -- if the user's request would put non-security code into `plugins/` or move security code out, **STOP immediately and warn the user** instead of proceeding.
3. **Mandatory pre-implementation gate.** Before writing ANY code, the user MUST explicitly specify BOTH of the following. If either is missing or ambiguous, **do NOT proceed** -- ask the user to clarify:
   - **Target layer/folder**: Which architectural layer (`core/`, `infrastructure/`, `plugins/`, `bootstrap/`, `presentation/`, `prompts/`, `query/`, `config/`) the change belongs to.
   - **Change category**: Whether the change is **cybersecurity-related** (belongs in `plugins/`) or **topological/orchestration** (belongs elsewhere -- `core/`, `infrastructure/`, etc.).

## Pre-Flight Protocol

> **STOP. Before writing ANY code, complete this checklist.**
> Do NOT skip steps. Do NOT paraphrase from memory. Open each file and read it.

### Step 1 -- ALWAYS read (every task, no exceptions):

```
cat .claude/docs/conventions.md
```

This file contains naming rules, import ordering, error handling patterns, and logging conventions that apply to ALL code in this repo. If you skip it, your code will be wrong.

### Step 2 -- Read based on what you're changing:

| If your task involves... | Then ALSO run |
|---|---|
| Adding/moving modules or directories | `cat .claude/docs/architecture.md` |
| Concurrency, parallel execution, race conditions, OCC, atomic file writes, Docker cleanup | `cat agent-docs/concurrency-invariants.md` |
| Implementing features, adding events/adapters | `cat .claude/docs/patterns.md` |
| Adding or modifying dependencies | `cat .claude/docs/dependencies.md` |
| Writing or modifying tests | `cat .claude/docs/testing.md` |
| Debugging a failure | `cat .claude/docs/common-errors.md` |
| Build, deploy, or CI changes | `cat .claude/docs/workflows.md` |
| Unfamiliar domain terms | `cat .claude/docs/domain-glossary.md` |
| Working on aggregates, events, or values | `cat .claude/docs/core-domain.md` |
| Working on the security plugin | `cat .claude/docs/plugins-security.md` |

### Step 3 -- Confirm you read them:

After reading, state which files you read and one concrete rule from each that applies to your current task. Then proceed with implementation.

Additional deep-dive docs in `agent-docs/`: `domain-model.md`, `development.md`, `configuration.md`, `api-reference.md`, `project-structure.md`, `architecture-concepts.md`, `architecture-separation.md`, `execution-flow.md`. Also see `MENTAL_MODEL.md` and `USER_MANUAL.md`.

## Project Structure

> **Do NOT trust a hardcoded directory tree.** Run `tree -L 2 -I '__pycache__|node_modules|.git|*.pyc|.venv|.ruff_cache|.pytest_cache|.hypothesis|.idea|build|output'`
> to see the current structure. For the full annotated tree, see `.claude/docs/architecture.md`.

**Layer rules** (stable architectural invariants):

- `core/` -- Domain logic. NEVER imports from `infrastructure/`. Only stdlib + own ports. Enforced by pre-commit hook.
- `infrastructure/` -- Adapters implementing protocols from `core/ports/`. Imports `core/` only.
- `plugins/` -- STRICTLY cybersecurity code only. No topology, orchestration, or scheduling logic.
- `bootstrap/` -- Composition root. Sole cross-boundary import point (imports `plugins/`, `infrastructure/`, `core/`).
- `presentation/` -- CLI, renderers, formatters. Depends on `core/application/`.
- `prompts/` -- Jinja2 templates (4-tier: system.j2 -> roles/ -> operations/ -> domains/). No Python imports.
- `query/` -- FastAPI REST API + React SPA dashboard. Read-side of CQRS.
- `config/` -- Pydantic Settings + YAML hierarchy (`config.yaml` + `config.{env}.yaml`).

## Key Architectural Decisions

- **Strict hexagonal boundary** -- `core/` depends only on stdlib and its own ports. All infrastructure is injected at bootstrap. Enforced by pre-commit, not just convention.
- **Event sourcing with single aggregate** -- `AgentSession` is the only aggregate. All state from replaying ~31 frozen Pydantic events. OCC via unique constraint on `(aggregate_id, sequence_number)`. No mutable state tables.
- **Three direct orchestrator methods** -- `assess_task`, `evaluate_task`, `execute_task` are plain methods on `AgentOrchestrator`. No pipeline, strategy, or chain-of-responsibility abstraction. This is intentional.
- **Domain context as opaque slot** -- `HierarchyLimits.domain_context: object | None` carries plugin-specific data (e.g., `CVEInstance`) through the tree. Only the plugin downcasts. Core is fully domain-ignorant.
- **Bootstrap is the sole cross-boundary import** -- `bootstrap/composition.py` is the only file that imports from `plugins/`. All other layers reference only protocols.

## Critical Do's and Don'ts

### Do
- Use `Protocol` classes for all ports in `core/ports/`
- Wrap external exceptions into domain exceptions (e.g., `LLMError`) with `from e` chaining
- Use `model_config = {"frozen": True}` on all Pydantic value objects and events
- Use `TYPE_CHECKING` blocks for imports only needed at type-check time
- Follow Given-When-Then structure in all tests with `# Given:` / `# When:` / `# Then:` comments
- Use `logger = logging.getLogger(__name__)` for all logging
- Emit events through domain methods on `AgentSession`, never mutate state directly
- Read relevant `.claude/docs/` files before making changes

### Don't
- Import from `infrastructure/` in any file under `core/` (pre-commit will reject)
- Put non-cybersecurity code in `plugins/` (topology, scheduling, orchestration go in `core/`)
- Use mutable Pydantic models for value objects or events
- Create pipeline/strategy abstractions around the 3 orchestrator operations
- Use `print()` for output -- use `logging` or presentation layer renderers
- Use bare `except:` -- always catch specific exception types
- Run `git commit` or `ruff check/format` (forbidden per critical rules)
- Skip the pre-implementation gate (rule 4) -- always confirm target layer and change category

## Environment & Config

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `ARISE_ENV` | No | Config overlay: `development`, `dev`, `production` | `development` |
| `POSTGRES_PASSWORD` | Yes | Database password (env var only, not in YAML) | -- |
| `POSTGRES_HOST` | No | Database host | `localhost` |
| `POSTGRES_PORT` | No | Database port | `5432` |
| `OPENAI_API_KEY` | Yes | OpenAI API key for LLM calls | -- |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (for Claude worker) | -- |
| `HOST_PROJECT_ROOT` | No | Host path for Docker-out-of-Docker volume mapping | -- |

Config loads: `config/config.yaml` (base) <- `config/config.{ARISE_ENV}.yaml` (overlay) <- env vars (highest priority).

Setup: `cp deployment/.env.example deployment/.env` and fill in values.

## Common Tasks

### Start the system
```bash
docker compose --profile local up -d
```

### Run tests
```bash
uv run pytest                              # Full suite
uv run pytest -k test_boss_initialization  # Single test
```

### Check code quality
```bash
ruff check .          # Lint
ruff format --check . # Format check
pyright               # Type check
```

> For step-by-step workflow recipes (adding events, endpoints, ports/adapters, plugins, etc.),
> see `.claude/docs/patterns.md`.
