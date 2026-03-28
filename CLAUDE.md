# CLAUDE.md — Arise Sec Lion

Recursive, self-healing multi-agent orchestration platform.
Event-sourced hexagonal architecture. BOSS → MANAGER → WORKER hierarchy.

## Critical Rules

1. **`core/` must NEVER import from `infrastructure/`**
2. **Forbidden** — Claude shall NEVER run `git commit` or `ruff check/format`
3. **`plugins/` is STRICTLY for cybersecurity code.** Topological, orchestration, scheduling, or any non-security logic must NEVER be placed in `plugins/`. Before implementing ANY user prompt, verify the change does not mix concerns — if the user's request would put non-security code into `plugins/` or move security code out, **STOP immediately and warn the user** instead of proceeding.
4. **Mandatory pre-implementation gate.** Before writing ANY code, the user MUST explicitly specify BOTH of the following. If either is missing or ambiguous, **do NOT proceed** — ask the user to clarify:
   - **Target layer/folder**: Which architectural layer (`core/`, `infrastructure/`, `plugins/`, `bootstrap/`, `presentation/`, `prompts/`, `query/`, `config/`) the change belongs to.
   - **Change category**: Whether the change is **cybersecurity-related** (belongs in `plugins/`) or **topological/orchestration** (belongs elsewhere — `core/`, `infrastructure/`, etc.).

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

- **Event Sourcing**: All state from replaying ~31 frozen `DomainEvent` Pydantic models. OCC via version.
- **NodeMessage union** (`values/node_message.py`): Discriminated on `direction` — `Briefing` (↓ parent→child), `Report` (↑ child→parent), `Handoff` (↔ sibling context).
- **3 orchestrator operations**: `assess_task` (PENDING — execute or decompose in one LLM call), `evaluate_task` (BOSS/MANAGER — decompose), `execute_task` (WORKER) — direct methods, no pipeline abstraction.
- **Auto-healing retry**: Model escalation chain + circuit breaker. Retry budget = chain length.
- **Infeasible handling**: LLM declares `constraints_unsatisfiable` → parent re-decomposes (capped at `max_redecompositions` per parent, default 2).
- **4-stage verification**: structural → deterministic → execution → LLM judge.
- **DAG scheduling**: `depends_on` on subtasks, Kahn's algorithm ordering.
- **Topology limits** (`orchestration.topology`): `max_depth` (soft — forces WORKER), `max_children_per_node` (hard), `max_total_agents` (hard).
- **Safeguards**: Global `max_run_duration_seconds` (default 1800s) hard-stops the system loop with deepest-first cancellation. Per-parent `max_redecompositions` (default 2) caps infeasibility-driven re-planning. `max_iterations_per_run` caps worker tool iterations.

## Architecture

```
Infrastructure → Ports ← Domain Core
      ↑                      ↑
   Bootstrap ───────────────┘
```

### Core (`core/`)

**Domain** (`domain/`): `AgentSession` aggregate, `SharedStore` (artifacts + decisions per hierarchy), domain services (`SubtaskParser`, `ConfigResolver`, `TaskScheduler`, `ContextUpdateParser`). Enums: `AgentRole` (BOSS/PENDING/MANAGER/WORKER), `AgentStatus`. Values: `HierarchyLimits`, `Subtask`, `AgentConfig`, `ReconPolicy`, `PromptCapabilities`.

**Ports** (`ports/`): 10 protocols in 3 files — `event_store_port.py` (Connect/Write/Read + composite), `runtime_ports.py` (LLM, WorkerTool, CostCalculator, RealtimeCallback, SharedContext, SiblingView, SystemLimits, Toolset, ReconTool), `domain_plugin_port.py` (DomainPlugin).

**Application** (`application/`): `AgentOrchestrator` (3 methods), `AgentExecutionService` (main loop, retry, concurrency). Services: `AgentRepository`, `ChildAgentFactory`, `ParentNotificationService`, `QueryService`, `PromptBuilder` (Jinja2 `TemplateChain`), `EventBroadcaster`, `RoleDispatch`, `ToolCallingService`, `ToolsetPolicyResolver`, `ToolsetContext`, `ContextCondenser`, `HierarchyLimitsRegistry`, `LLMQueryExecutor`, `RetryPolicy`, `VerificationPipeline`, `PromptParser`, `PromptTraceService`, `PromptStrategy` protocol.

**Query** (`query/`): Projection pipeline (EventStore → Collector → Filter → Formatter → Sink). Plugin registry. Projections: Summary, AgentList, Cost, AgentSummary.

### Infrastructure (`infrastructure/`)

Adapters: `PostgresEventStore`, `LiteLLMAdapter`, `PostgresSharedContextAdapter`, `CostCalculator`, `ReconToolAdapter`, `DockerSecBenchRuntime`, sinks. Worker adapters: `ClaudeAgentSDKAdapter`, `OpenHandsAdapter`, `GoogleADKAdapter`.

### Other Layers

- **Bootstrap** (`bootstrap/`): Composition root (`composition.py` — domain component builders), `infrastructure.py` (adapter factory), `application.py` (service wiring), `bootstrap.py` (argparse + command dispatch).
- **Config** (`config/`): `settings.py` (Pydantic Settings), YAML hierarchy (`config.yaml` + `config.{env}.yaml`).
- **Plugins** (`plugins/`): Security domain plugin — `CVEInstance`, `BenchmarkResult`, `SecBenchPromptStrategy`, container runtime. Implements `DomainPlugin` protocol; bootstrap is the sole cross-boundary import point.
- **Presentation** (`presentation/`): `CLI` class, renderers, formatters.
- **API** (`query/api/`): FastAPI + SSE + React SPA dashboard.
- **Prompts** (`prompts/`): 4-tier Jinja2 templates — `system.j2`, `roles/`, `operations/`, `context/`, `domains/secbench/`.

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
| [`architecture-separation.md`](agent-docs/architecture-separation.md) | Generic vs security domain boundary, plugin protocol |
| [`execution-flow.md`](agent-docs/execution-flow.md) | How tasks flow through system |
| [`MENTAL_MODEL.md`](MENTAL_MODEL.md) | Comprehensive conceptual guide (10 sections) |
| [`USER_MANUAL.md`](USER_MANUAL.md) | CLI usage, dashboard, plugins, recon config |
