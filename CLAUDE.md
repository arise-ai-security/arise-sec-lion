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

2. **ALWAYS use `--profile local`** for Docker commands unless explicitly told otherwise.
   - `local` profile uses local PostgreSQL database
   - Other profiles (`dev`, `prod`) connect to external databases
   - This prevents accidental operations on shared/production data

3. **Forbidden Operations** - Claude shall NEVER:
   - Run `git commit` (user commits manually)
   - Run `ruff check/format` (user lints manually)

## Development

All commands run inside Docker containers via profiles:

```bash
cd deployment

# Profiles: local (local DB - DEFAULT), dev (external DB), prod, test

# Start services
docker compose --profile local up -d --build

# Run a task
docker compose --profile local exec app python main.py run "Your task"

# Run with worker configuration overrides
docker compose --profile local exec app python main.py run "Your task" \
  --worker-model gpt-4o \
  --worker-tool claude_code

# Run tests
docker compose --profile local exec app uv run pytest

# View results
docker compose --profile local exec app python main.py list
docker compose --profile local exec app python main.py events
docker compose --profile local exec app python main.py summary
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

## Orchestrator

`AgentOrchestrator` handles three operations as direct method calls:
- `evaluate_complexity()` — PENDING → WORKER/MANAGER
- `evaluate_task()` — BOSS/MANAGER → decompose → spawn children
- `execute_task()` — WORKER → tool execution → verification

Features: DAG scheduling (`depends_on`), 4-stage verification pipeline,
auto-healing retry with model escalation, circuit breaker, infeasible
decision handling with parent re-decomposition.

## Documentation

**Read relevant doc(s) before implementing changes. Ask if unsure.**

| Document | When to Read |
|----------|--------------|
| [`domain-model.md`](agent-docs/domain-model.md) | Modifying agents, events, state machine |
| [`development.md`](agent-docs/development.md) | Testing, coding standards, adding features, **prompt parser** |
| [`configuration.md`](agent-docs/configuration.md) | Config/secrets, environment setup |
| [`api-reference.md`](agent-docs/api-reference.md) | REST API, SSE, schemas |
| [`project-structure.md`](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [`architecture-concepts.md`](agent-docs/architecture-concepts.md) | CQRS, OCC, Event Sourcing, Hexagonal, DDD |
| [`execution-flow.md`](agent-docs/execution-flow.md) | How tasks flow through system |
| [`execution-flow.md`](agent-docs/execution-flow.md) | How tasks flow through system |
