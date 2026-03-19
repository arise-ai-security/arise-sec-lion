# CLAUDE.md — Arise Sec Lion

Recursive, self-healing multi-agent orchestration platform.
Event-sourced hexagonal architecture. BOSS → MANAGER → WORKER hierarchy.

## Critical Rules

1. **`core/` must NEVER import from `infrastructure/`**
2. **Forbidden** — Claude shall NEVER run `git commit` or `ruff check/format`

## Agent Flow

```
BOSS (root)
  │ decomposes task
  ▼
PENDING ──────────────────────────────┐
  │ assesses task (single LLM call)   │
  ├─── EXECUTE ──► WORKER             │
  │                 └─► executes      │
  └─── DECOMPOSE ──► MANAGER          │
                      └─► spawns ─────┘ (recursive)
```

## Key Patterns

- **Event Sourcing**: All state from replaying ~30 frozen `DomainEvent` Pydantic models. OCC via version.
- **NodeMessage union** (`values/node_message.py`): Discriminated on `direction` — `Briefing` (↓ parent→child), `Report` (↑ child→parent), `Handoff` (↔ sibling context).
- **3 orchestrator operations**: `assess_task` (PENDING — execute or decompose in one LLM call), `evaluate_task` (BOSS/MANAGER — decompose), `execute_task` (WORKER) — direct methods, no pipeline abstraction.
- **Auto-healing retry**: Model escalation chain + circuit breaker. Retry budget = chain length.
- **Infeasible handling**: LLM declares `constraints_unsatisfiable` → parent re-decomposes.
- **4-stage verification**: structural → deterministic → execution → LLM judge.
- **DAG scheduling**: `depends_on` on subtasks, Kahn's algorithm ordering.
- **Limits** (`config.yaml` → `orchestration.limits`): `max_depth` (soft — forces WORKER), `max_children_per_node` (hard), `max_total_agents` (hard).

## Architecture

```
Infrastructure → Ports ← Domain Core
      ↑                      ↑
   Bootstrap ───────────────┘
```

### Core (`core/`)

**Domain** (`domain/`): `AgentSession` aggregate, `SharedStore` (artifacts + decisions per hierarchy), domain services (`SubtaskParser`, `ConfigResolver`, `TaskScheduler`, `ContextUpdateParser`). Enums: `AgentRole` (BOSS/PENDING/MANAGER/WORKER), `AgentStatus`. Values: `HierarchyLimits`, `Subtask`, `AgentConfig`, `CVEInstance`.

**Ports** (`ports/`): 7 protocols in 2 files — `event_store_port.py` (Connect/Write/Read + composite), `runtime_ports.py` (LLM, WorkerTool, CostCalculator, RealtimeCallback, SharedContext, SiblingView).

**Application** (`application/`): `AgentOrchestrator` (3 methods), `AgentExecutionService` (main loop, retry, concurrency), `AgentRepository`, `ChildAgentFactory`, `ParentNotificationService`, `AgentQueryService`, `PromptBuilder` (Jinja2 `TemplateChain`), `EventBroadcaster`.

**Query** (`query/`): Projection pipeline (EventStore → Collector → Filter → Formatter → Sink). Plugin registry. Projections: Summary, AgentList, Cost, AgentSummary.

### Infrastructure (`infrastructure/`)

Adapters: `PostgresEventStore`, `LiteLLMAdapter`, `PostgresSharedContextAdapter`, `CostCalculator`, sinks. Worker adapters: `ClaudeAgentSDKAdapter`, `OpenHandsAdapter`, `GoogleADKAdapter`.

### Other Layers

- **Bootstrap** (`bootstrap/`): Composition root. Wires infra → app → presentation. Argparse.
- **Presentation** (`presentation/`): `CLI` class, renderers, formatters.
- **API** (`query/api/`): FastAPI + SSE + React SPA dashboard.
- **Prompts** (`prompts/`): Jinja2 templates, strategy pattern for SEC-bench specialization.

## Development

Docker: see [`deployment/README.md`](deployment/README.md)

Entry: `main.py` → `bootstrap.bootstrap.main()`

CLI: `run <task>` (`--cve-file`, `--worker-model`, `--worker-tool`), `events`, `summary`, `list`, `prompts`

Worker tools: `claude_code`, `openhands`, `google_adk`

## Documentation

**Read relevant doc(s) before implementing changes. Ask if unsure.**

| Document | When to Read |
|----------|--------------|
| [`domain-model.md`](agent-docs/domain-model.md) | Modifying agents, events, state machine |
| [`development.md`](agent-docs/development.md) | Testing, coding standards, prompt parser |
| [`configuration.md`](agent-docs/configuration.md) | Config/secrets, environment setup |
| [`api-reference.md`](agent-docs/api-reference.md) | REST API, SSE, schemas |
| [`project-structure.md`](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [`architecture-concepts.md`](agent-docs/architecture-concepts.md) | CQRS, OCC, Event Sourcing, Hexagonal, DDD |
| [`execution-flow.md`](agent-docs/execution-flow.md) | How tasks flow through system |
