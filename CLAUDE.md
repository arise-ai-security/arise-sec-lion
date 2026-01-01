# CLAUDE.md - Arise Sec Lion

Recursive, self-healing multi-agent orchestration platform.
Hexagonal Architecture + Event Sourcing | BOSS → MANAGER → WORKER hierarchy

## Critical Rules

1. **`core/` must NEVER import from `infrastructure/`**
   ```
   Infrastructure → Ports ← Domain Core
         ↑                      ↑
      Bootstrap ───────────────┘
   ```
   Verify: `grep -r "from infrastructure" core/` returns nothing

2. **Forbidden Operations** - Claude shall NEVER:
   - Run `git commit` (user commits manually)
   - Run `ruff check/format` (user lints manually)

3. **Off-Limits:** Do not read `user-docs/` directory

## Development

All commands run inside Docker containers via profiles:

```bash
cd deployment

# Profiles: local (local DB), dev (external DB), prod, test

# Start services
docker compose --profile dev up -d --build

# Run a task
docker compose --profile dev exec app-dev python main.py run "Your task"

# Run tests
docker compose --profile dev exec app-dev uv run pytest

# View results
docker compose --profile dev exec app-dev python main.py list
docker compose --profile dev exec app-dev python main.py events
docker compose --profile dev exec app-dev python main.py summary
```

## Agent Flow

```
BOSS (root)
  │ decomposes task
  ▼
PENDING ──────────────────────────────┐
  │ evaluates complexity              │
  ├─── SIMPLE ──► WORKER              │
  │                 └─► executes      │
  └─── COMPLEX ──► MANAGER            │
                     └─► spawns ──────┘ (recursive)
```

## Limit Enforcement

Three configurable limits in `config/config.yaml` → `orchestration.limits`:

| Limit | Enforcement | Behavior |
|-------|-------------|----------|
| `max_depth` | **Soft** | At limit, children forced to WORKER role (no further decomposition) |
| `max_children_per_node` | **Hard** | Agent fails if LLM generates more subtasks than allowed |
| `max_total_agents` | **Hard** | Agent fails if spawning would exceed global agent budget |

Limits are passed to LLM prompts. LLM can respond with `constraints_unsatisfiable` if task cannot fit.

## Pipeline Architecture

`AgentOrchestrator` uses composable Pipeline/Chain pattern:

```
core/application/pipeline/
├── context.py      # PipelineState (delegates to HierarchyLimits)
├── executor.py     # Pipeline executor (short-circuits on failure)
└── steps/          # 14 focused steps across 8 modules
```

Three pipelines: `complexity_evaluation`, `task_decomposition`, `worker_execution`

## Documentation

**Read relevant doc(s) before implementing changes. Ask if unsure.**

| Document | When to Read |
|----------|--------------|
| [`domain-model.md`](agent-docs/domain-model.md) | Modifying agents, events, state machine |
| [`development.md`](agent-docs/development.md) | Testing, coding standards, adding features |
| [`configuration.md`](agent-docs/configuration.md) | Config/secrets, environment setup |
| [`api-reference.md`](agent-docs/api-reference.md) | REST API, SSE, schemas |
| [`project-structure.md`](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [`architecture-concepts.md`](agent-docs/architecture-concepts.md) | CQRS, OCC, Event Sourcing, Hexagonal, DDD |
| [`execution-flow.md`](agent-docs/execution-flow.md) | How tasks flow through system |
| [`context-passing-mechanism.md`](agent-docs/context-passing-mechanism.md) | SharedContext, artifacts, decisions, budget |
| [`context-passing-guide.md`](agent-docs/context-passing-guide.md) | ContextComposer API, practical scenarios |
| [`user-manual.md`](agent-docs/user-manual.md) | Docker commands reference |
