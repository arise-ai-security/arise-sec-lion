<!-- Read this when: adding, removing, or updating dependencies -->
This document covers all runtime, dev, and frontend dependencies -- their purpose, version constraints, known gotchas, and procedures for adding or updating packages.

# Dependencies

Python 3.12 project managed with `uv`. Lockfile: `uv.lock`. Config: `pyproject.toml`.

## Runtime Dependencies

| Package | Constraint | Purpose | Primary usage locations |
|---------|-----------|---------|------------------------|
| `asyncpg` | `>=0.30.0` | Async PostgreSQL driver for event store | `infrastructure/adapters/postgres_event_store.py`, `infrastructure/adapters/shared_context_adapter.py` |
| `claude-agent-sdk` | `>=0.1.18` | Anthropic Claude Code worker backend | `infrastructure/adapters/worker/claude_sdk_adapter.py` |
| `click` | `>=8.1.0` | CLI framework (styled output, prompts) | `presentation/rendering/renderer.py` |
| `fastapi` | `>=0.115.0,<0.124.0` | REST API + SSE streaming endpoints | `query/api/app.py`, `query/api/routes/`, `query/api/bootstrap.py` |
| `google-adk` | `>=1.21.0` | Google Agent Development Kit worker backend | `infrastructure/adapters/worker/google_adk_adapter.py` |
| `jinja2` | `>=3.1.6` | 4-tier prompt template rendering | `core/application/services/prompt/prompt_builder.py`, `query/api/routes/prompts.py` |
| `litellm` | `>=1.80.0` | Multi-provider LLM gateway (OpenAI, Anthropic, etc.) | `infrastructure/adapters/litellm_adapter.py`, `infrastructure/adapters/cost_calculator.py` |
| `mcp` | `>=1.9.0` | Model Context Protocol support | `infrastructure/adapters/worker/google_adk_adapter.py` (lazy import of `StdioServerParameters`) |
| `openhands-sdk` | `>=1.4.1` | OpenHands coding agent worker backend | `infrastructure/adapters/worker/openhands_adapter.py` |
| `openhands-tools` | `>=1.4.1` | OpenHands tool definitions | `infrastructure/adapters/worker/openhands_adapter.py` |
| `orjson` | `>=3.11.4` | Fast JSON serialization for event store persistence | `infrastructure/adapters/postgres_event_store.py` |
| `pydantic` | `>=2.12.4` | Domain events, value objects, config models (all frozen) | `core/domain/`, `core/domain/events/events.py`, `config/settings.py`, `plugins/security/` |
| `pydantic-settings` | `>=2.12.0` | YAML + env var config management | `config/settings.py` |
| `pyyaml` | `>=6.0.3` | YAML config file parsing | `config/settings.py` |
| `sse-starlette` | `>=3.0.3` | Server-Sent Events for real-time streaming | `query/api/routes/events.py` |
| `uvicorn` | `>=0.38.0` | ASGI server for FastAPI | `query/api/bootstrap.py` |

## Dev Dependencies

| Package | Constraint | Purpose |
|---------|-----------|---------|
| `datasets` | `>=4.8.4` | HuggingFace datasets library for loading SWE-bench benchmark data |
| `hypothesis` | `>=6.100.0` | Property-based testing for domain invariants |
| `numpy` | `<2` | Required by `datasets`; pinned below v2 for compatibility (see rationale below) |
| `pre-commit` | `>=4.4.0` | Git hooks (architecture boundary check, ruff lint/format) |
| `pyright` | `>=1.1.407` | Static type checker |
| `pytest` | `>=9.0.1` | Test framework (async auto-mode via `pytest-asyncio`) |
| `pytest-asyncio` | `>=1.3.0` | Async test support; `asyncio_mode = "auto"` in `pyproject.toml` |
| `pytest-cov` | `>=6.0.0` | Coverage reporting; `fail_under = 70` enforced |
| `ruff` | `>=0.14.5` | Linter + formatter (replaces black, isort, flake8); config in `ruff.toml` |
| `swebench` | `>=4.1.0` | SWE-bench evaluation harness for benchmark runs |

## Frontend Dependencies (query/web/)

The React SPA dashboard lives in `query/web/` with its own `package.json`. The root `package.json` is an empty `{}` placeholder -- all Node.js work happens in `query/web/`.

### Runtime

| Package | Constraint | Purpose |
|---------|-----------|---------|
| `react` | `^19.2.0` | UI framework |
| `react-dom` | `^19.2.0` | DOM rendering |
| `react-router-dom` | `^7.11.0` | Client-side routing |
| `@xyflow/react` | `^12.10.0` | Agent hierarchy tree visualization (node graph) |
| `tailwindcss` | `^4.1.17` | Utility CSS framework |
| `@tailwindcss/vite` | `^4.1.17` | TailwindCSS Vite plugin |

### Dev

| Package | Constraint | Purpose |
|---------|-----------|---------|
| `@eslint/js` | `^9.39.1` | ESLint core JS rules |
| `@types/node` | `^24.10.1` | Node.js type definitions |
| `@types/react` | `^19.2.5` | React type definitions |
| `@types/react-dom` | `^19.2.3` | React DOM type definitions |
| `@vitejs/plugin-react` | `^5.1.1` | React fast-refresh for Vite |
| `eslint` | `^9.39.1` | Linting |
| `eslint-plugin-react-hooks` | `^7.0.1` | React hooks lint rules |
| `eslint-plugin-react-refresh` | `^0.4.24` | React refresh lint rules |
| `globals` | `^16.5.0` | Global variable definitions for ESLint |
| `typescript` | `~5.9.3` | Type checking (tilde-pinned for stability) |
| `typescript-eslint` | `^8.46.4` | TypeScript ESLint integration |
| `vite` | `^7.2.4` | Build tool and dev server |

## Version Constraint Rationale

### FastAPI upper bound (`<0.124.0`)

FastAPI is upper-bounded to prevent breaking changes in routing, dependency injection, or middleware behavior. Bump the upper bound only after testing the full `query/api/` surface against the new version.

### Pydantic hard minimum (`>=2.12.4`)

Earlier Pydantic v2 releases have bugs with frozen model sequence handling and discriminated unions. This project uses `model_config = {"frozen": True}` on every event and value object. Do not lower this floor.

### numpy upper bound (`<2`)

numpy is pinned below v2 because the `datasets` library (HuggingFace) and parts of the `swebench` evaluation tooling have transitive dependencies that are not yet fully compatible with numpy 2.x. The numpy 1.x -> 2.x migration changed array scalar types, removed deprecated aliases, and altered dtype behavior. Do not remove this upper bound until `datasets` and `swebench` explicitly declare numpy 2.x support.

### TypeScript tilde pin (`~5.9.3`)

The frontend pins TypeScript with `~` (patch-only) rather than `^` (minor) to avoid type-system changes breaking the build unexpectedly.

## Known Gotchas

### OpenHands SDK imports are restricted to infrastructure/

OpenHands SDK (`openhands-sdk`, `openhands-tools`) must ONLY be imported inside `infrastructure/adapters/worker/`. The pre-commit hook `scripts/check_architecture_boundaries.py` enforces this with regex patterns that scan all files under `core/`. Importing OpenHands anywhere in `core/` will fail the boundary check and block the commit.

The boundary script also forbids `CmdOutputMetadata(` and `Observation: kind=` patterns in `core/` to prevent OpenHands-specific parsing from leaking into domain code.

### LiteLLM version sensitivity

LiteLLM updates frequently and occasionally changes provider-specific behavior (model name routing, parameter handling, error formats). The `>=1.80.0` floor pins to a known-good version. When upgrading, test all three provider paths: OpenAI, Anthropic, and Google models.

### O-series model detection may need updates

`is_o_series()` in `infrastructure/adapters/llm_common.py` (shared by the LiteLLM and OpenRouter adapters) detects OpenAI O-series reasoning models (`o1`, `o3`, `o4`) that reject the `temperature` parameter and require special message formatting. If OpenAI releases new model prefixes (e.g., `o5`), this function needs updating or calls will fail with provider errors.

### Jinja2 template loading

`PromptBuilder` uses `FileSystemLoader` pointed at the `prompts/` directory. All template paths are relative to that root. A missing template raises `TemplateNotFound`. The 4-tier hierarchy is: `system.j2` -> `roles/` -> `operations/` -> `domains/`.

### MCP is lazily imported

The `mcp` package is only imported at runtime inside `google_adk_adapter.py` via a local `from mcp import StdioServerParameters`. This avoids import-time failures when MCP is installed but not configured. If you remove the `mcp` dependency, the Google ADK adapter will fail at runtime when MCP tools are requested.

### `claude-agent-sdk` is NOT the general Anthropic API SDK

`claude-agent-sdk` is Anthropic's SDK specifically for building Claude Code agent workers. It is distinct from the `anthropic` Python package (the general-purpose API client). This project uses `claude-agent-sdk` in `infrastructure/adapters/worker/claude_sdk_adapter.py` to spawn Claude Code as a sub-agent worker. If you need to call the Anthropic API directly (e.g., for chat completions), that goes through `litellm`, not `claude-agent-sdk`.

### Worker SDK packages are only needed for their respective backends

`claude-agent-sdk`, `google-adk`, `openhands-sdk`, and `openhands-tools` are only needed if that worker backend is configured. They could theoretically be optional dependencies, but are kept as required to simplify Docker builds and CI.

## Adding a Dependency

```bash
# Runtime dependency
uv add <package-name>

# Dev dependency
uv add --dev <package-name>

# Verify the lockfile is consistent
uv sync --frozen
```

After adding, commit both `pyproject.toml` and `uv.lock`. Docker builds run `uv sync --frozen` and will fail if the lockfile is stale.

### Checklist before adding

1. **Justify why stdlib or an existing dep cannot do the job.** This project already has Pydantic for validation, orjson for serialization, and litellm as an LLM abstraction -- check those first.
2. **Determine the correct layer.** If the dependency is an external service SDK, it belongs in `infrastructure/adapters/` only. Never import third-party service SDKs in `core/`.
3. **Pin appropriately.** Use `>=X.Y.Z` for most packages. Add an upper bound (`<X.Y.0`) only when known breaking changes exist in newer versions.
4. **Check for transitive conflicts.** Run `uv sync` and verify no resolution errors. LiteLLM and the worker SDKs pull large dependency trees that occasionally conflict.

## Removing a Dependency

```bash
uv remove <package-name>
```

Before removing, search the codebase for all imports of the package. Remember that some imports are lazy (like `mcp` in the Google ADK adapter) and will not show up in a static `from X import Y` search at the top of files.

## Upgrading Dependencies

```bash
# Upgrade a specific package (preferred)
uv lock --upgrade-package <package-name>
uv sync

# Alternatively, set a new minimum and re-lock
uv add <package-name>@latest

# Upgrade all (use with caution)
uv lock --upgrade
uv sync
```

### Post-upgrade verification

1. `uv sync --frozen` -- lockfile consistency
2. `uv run pytest` -- full test suite
3. `ruff check .` -- linting still passes
4. `pyright` -- type checking still passes
5. For LiteLLM upgrades: manually test OpenAI, Anthropic, and Google model calls
6. For FastAPI upgrades: test all routes in `query/api/routes/` and SSE streaming
7. For Pydantic upgrades: verify frozen model serialization in event store round-trips

## Frontend Dependency Management

```bash
cd query/web && npm install          # Install deps
cd query/web && npm run dev          # Dev server (port 5173)
cd query/web && npm run build        # Production build to dist/
cd query/web && npm run lint         # ESLint check
```

Commit `query/web/package-lock.json` alongside `package.json` changes. The frontend is built separately from the Python backend and served as static assets.
