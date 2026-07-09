<!-- Read this when: adding/moving modules or directories, understanding system design, or determining where new code goes -->
Hexagonal (ports-and-adapters) event-sourced multi-agent platform with strict dependency inversion, two event-sourced aggregates (`AgentSession` primary + `SharedStore`), and 38 frozen Pydantic domain events.

> **If you create, move, or delete a directory/module, update the directory tree in this file.**

## Annotated Directory Tree

Generated with `tree -L 2 -I '__pycache__|node_modules|.git|*.pyc|.venv|.ruff_cache|.pytest_cache|.hypothesis|.idea|build|output'`.

```
arise-sec-lion/
├── main.py                        # CLI entry point; delegates to bootstrap.bootstrap.main()
├── bootstrap/                     # Composition root -- sole cross-boundary import point
│   ├── bootstrap.py               # argparse CLI, command dispatch (run/events/summary/list/prompts)
│   ├── composition.py             # Domain plugin builder registry; ONLY file importing plugins/
│   ├── infrastructure.py          # Adapter factory -- creates all infra instances
│   ├── application.py             # Wires application services with ports + adapters
│   ├── realtime_adapter.py        # SSE RealtimeCallbackPort bridge
│   └── tests/                     # Bootstrap integration tests
├── config/                        # Pydantic Settings + YAML config hierarchy
│   ├── settings.py                # All config models, _load_yaml_hierarchy()
│   ├── overlay.py                 # extends:/overrides: overlay resolver behind Settings.from_yaml
│   ├── _paths.py                  # REPO_ROOT resolver (anchored on pyproject.toml)
│   ├── config.yaml                # Base config (always loaded)
│   ├── config.{env}.yaml          # Environment overlay (development/dev/production)
│   └── experiment-*.yaml          # Experiment-specific config overrides
├── core/                          # Domain core -- NEVER imports from infrastructure/
│   ├── ports/                     # Protocol interfaces (18+ protocols across 7 files)
│   │   ├── event_store_port.py    # EventStoreConnectPort, WritePort, ReadPort, composite Port
│   │   ├── runtime_ports.py       # LLMPort, WorkerToolPort, CostCalculatorPort, RealtimeCallbackPort, etc.
│   │   ├── worker_port.py         # WorkerPort -- flat-mode single-agent worker (vs hierarchical WorkerToolPort)
│   │   ├── domain_plugin_port.py  # DomainPlugin protocol + PreparedRunWorkspace, WorkerExecutionContext
│   │   ├── decomposition_validator_port.py  # DecompositionValidator + verdict/violation values
│   │   ├── shared_code_context_port.py      # SharedCodeContextPort (worker source-read/edit capture)
│   │   └── procedure_ports.py     # ProcedureExecutorPort + NullProcedureExecutor (deterministic procedure tier)
│   ├── domain/                    # Pure domain logic, zero external dependencies
│   │   ├── aggregates/            # AgentSession -- primary aggregate (SharedStore is the 2nd, in shared_context.py)
│   │   ├── events/                # 38 frozen Pydantic DomainEvent subclasses (events.py)
│   │   ├── values/                # AgentConfig, enums, NodeMessage, Subtask, HierarchyLimits, failure, procedure, etc.
│   │   │   └── context/           # Contextual value objects
│   │   ├── services/              # SubtaskParser, TaskScheduler, ConfigResolver, ContextUpdateParser
│   │   ├── exceptions.py          # DomainInvariantError, ConcurrencyError, InfeasibleError, etc.
│   │   ├── shared_context.py      # SharedStore value object for cross-agent context
│   │   └── tests/                 # Domain unit tests (Given-When-Then)
│   ├── application/               # Application services orchestrating domain + ports
│   │   ├── agent_orchestrator.py  # AgentOrchestrator -- 3 direct methods (assess/evaluate/execute)
│   │   ├── execution_service.py   # AgentExecutionService -- system loop, concurrency, retry
│   │   ├── dtos.py                # AgentResultDTO, SystemStatisticsDTO
│   │   ├── types.py               # ProgressCallback type alias
│   │   ├── services/              # Application service modules (see breakdown below)
│   │   │   ├── lifecycle/         # ChildAgentFactory, AgentRepository, ParentNotifier, etc.
│   │   │   ├── orchestration/     # LLMQueryExecutor, ContextCondenser, VerificationPipeline, RetryPolicy, failure_digest, truncation
│   │   │   ├── prompt/            # PromptBuilder, PromptParser, PromptStrategy, PromptTraceService
│   │   │   ├── toolset/           # ToolCallingService, ToolsetContext, ToolsetPolicyResolver, LoopPolicy
│   │   │   └── query/             # QueryService, EventBroadcaster
│   │   └── tests/                 # Application-level tests
│   └── query/                     # CQRS read-side projections
│       ├── ports/                 # sink_port.py -- output sink protocol
│       ├── projections/           # Pipeline: base/, filters/, formatters/, impl/, registry
│       │   └── impl/              # Summary, AgentList, Cost, AgentSummary projections
│       └── tests/                 # Query projection tests
├── infrastructure/                # Adapters implementing core/ports/ protocols
│   ├── adapters/                  # All adapter implementations
│   │   ├── postgres_event_store.py  # EventStorePort implementation (PostgreSQL, JSONB)
│   │   ├── litellm_adapter.py     # LLMPort implementation (LiteLLM proxy)
│   │   ├── openrouter_adapter.py  # LLMPort implementation (direct OpenRouter transport)
│   │   ├── shared_context_adapter.py  # SharedContextPort (PostgreSQL)
│   │   ├── cost_calculator.py     # CostCalculatorPort
│   │   ├── recon_tool_adapter.py  # ReconToolPort (read-only codebase inspection)
│   │   ├── sinks.py               # Query pipeline output sinks (decorated registry)
│   │   ├── worker/                # WorkerToolPort implementations
│   │   │   ├── claude_sdk_adapter.py  # Claude Agent SDK
│   │   │   ├── openhands_adapter.py   # OpenHands (formerly OpenDevin)
│   │   │   ├── google_adk_adapter.py  # Google Agent Development Kit
│   │   │   ├── base.py            # Shared worker adapter base
│   │   │   └── shared/            # Cross-worker utilities (container_session, event_sequencer, etc.)
│   │   ├── research/              # Research-specific adapters (tools/)
│   │   └── secbench/              # SEC-bench infrastructure (currently empty; runtime lives in plugins/)
│   ├── workers/                   # Flat-mode WorkerPort impls (claude_code_worker, openhands_worker)
│   ├── cleanup/                   # CleanupRegistry -- PID-labeled signal/atexit teardown handlers
│   ├── sql/                       # Raw SQL files (create_events_table.sql)
│   └── tests/                     # Infrastructure integration tests
├── plugins/                       # STRICTLY cybersecurity code only
│   └── security/                  # SecurityDomainPlugin + SecBenchPromptStrategy
│       ├── plugin.py              # SecurityDomainPlugin (implements DomainPlugin protocol)
│       ├── prompt_strategy.py     # SecBenchPromptStrategy (implements PromptStrategy protocol)
│       ├── cve_instance.py        # CVEInstance value object (opaque domain context)
│       ├── cve_inference.py       # Infers CVEInstance from task text
│       ├── container_runtime.py   # SecurityContainerRuntime protocol + SecBenchWorkspace/Session
│       ├── docker_runtime.py      # DockerSecBenchRuntime (concrete container lifecycle)
│       ├── image_resolver.py      # Resolves Docker image for a CVE
│       ├── security_tool.py       # Security-domain tooling
│       └── tests/                 # Security plugin tests
├── presentation/                  # User-facing output layer
│   ├── cli.py                     # CLI class (Click commands, delegates to ExecutionService)
│   ├── formatters/                # Output formatting (event_formatter, prompt_trace_formatter)
│   ├── rendering/                 # Rich-based terminal rendering (renderer.py)
│   ├── persistence/               # Result persistence (run_persistence.py)
│   └── tests/                     # Presentation tests
├── prompts/                       # 4-tier Jinja2 template hierarchy (no Python)
│   ├── system.j2                  # Tier 1: Global system identity + constraints
│   ├── roles/                     # Tier 2: Per-role persona (boss, manager, pending, worker)
│   ├── operations/                # Tier 3: Per-operation format (assess, decomposition, execution)
│   ├── domains/                   # Tier 4: Optional domain extension
│   │   └── secbench/              # SEC-bench CVE templates (boss, assess, worker/*, manager/*, cve, tools)
│   └── context/                   # Contextual fragments (scope, sibling, workspace)
├── query/                         # Read-side API + dashboard
│   ├── api/                       # FastAPI REST + SSE endpoints
│   │   ├── app.py                 # FastAPI application factory
│   │   ├── bootstrap.py           # API-specific DI wiring
│   │   ├── dependencies.py        # FastAPI Depends providers
│   │   ├── schemas.py             # API response schemas
│   │   └── routes/                # Route modules (agents, config, events, prompt_trace, prompts)
│   └── web/                       # React 19 + Vite + TailwindCSS dashboard SPA
│       └── src/                   # App.tsx, api/, components/, hooks/, pages/, types/
├── deployment/                    # Docker, docker-compose, CVE fixture files
│   ├── Dockerfile                 # Main application image
│   ├── secbench-tools.Dockerfile  # SEC-bench tooling image
│   ├── docker-compose.yml         # Profiles: local/dev/prod/test
│   └── *.json                     # CVE instance fixtures
├── scripts/                       # Dev tooling scripts
│   ├── check_architecture_boundaries.py  # Pre-commit: rejects core/ -> infrastructure/ imports
│   ├── seed_demo_events.py        # Seed demo data into event store
│   ├── run_swebench_batch.sh      # SWE-bench batch runner
│   └── swebench_sampler.py        # SWE-bench instance sampler
├── agent-docs/                    # Deep-dive documentation (not code)
├── docs/                          # User-facing documentation
├── logs/                          # Run evaluation logs
├── pyproject.toml                 # Project metadata, dependencies
├── ruff.toml                      # Linter/formatter config (100-char lines)
└── uv.lock                        # Locked dependencies
```

## Layer Diagram

```
                    ┌─────────────────────────────────┐
                    │          presentation/           │
                    │   CLI, renderers, formatters     │
                    └──────────────┬──────────────────┘
                                   │ depends on
                    ┌──────────────▼──────────────────┐
                    │          bootstrap/              │
                    │  Composition root, DI wiring     │◄── ONLY place that imports plugins/
                    └──┬───────────┬──────────────┬───┘
                       │           │              │
          ┌────────────▼──┐  ┌────▼────────┐  ┌──▼──────────────┐
          │infrastructure/│  │   core/     │  │    plugins/      │
          │  Adapters      │  │  Domain     │  │  Security domain │
          │  (implements   │──►  Ports  ◄──│──│  (implements     │
          │   ports)       │  │  App logic  │  │   DomainPlugin)  │
          └───────────────┘  └─────┬───────┘  └─────────────────┘
                                   │ read-side
                    ┌──────────────▼──────────────────┐
                    │         query/                   │
                    │  FastAPI + SSE + React SPA       │
                    └─────────────────────────────────┘
```

Arrows point toward the dependency: `infrastructure/` and `plugins/` both depend on `core/ports/`. `core/` depends on nothing outside itself.

## Import Rules

| From \ To | `core/domain` | `core/ports` | `core/application` | `infrastructure/` | `plugins/` | `bootstrap/` | `presentation/` | `query/` |
|-----------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `core/domain` | self | -- | -- | **NEVER** | **NEVER** | -- | -- | -- |
| `core/ports` | YES | self | -- | **NEVER** | **NEVER** | -- | -- | -- |
| `core/application` | YES | YES | self | **NEVER** | **NEVER** | -- | -- | -- |
| `core/query` | YES | YES | -- | **NEVER** | **NEVER** | -- | -- | -- |
| `infrastructure/` | YES | YES | -- | self | -- | -- | -- | -- |
| `plugins/` | YES | YES | YES | **NEVER** | self | -- | -- | -- |
| `bootstrap/` | YES | YES | YES | YES | YES | self | YES | -- |
| `presentation/` | YES | YES | YES | -- | -- | -- | self | -- |
| `query/api/` | YES | YES | YES | YES | -- | -- | -- | self |

The pre-commit hook `scripts/check_architecture_boundaries.py` enforces the `core/` -> `infrastructure/` boundary at commit time. It scans every non-test `.py` file under `core/` for forbidden import patterns: `from infrastructure`, `import infrastructure`, and OpenHands SDK leaks (`openhands`, `CmdOutputMetadata(`, `Observation: kind=`). Violations block the commit.

## Core Internals

### Domain (`core/domain/`)

`AgentSession` (`core/domain/aggregates/agent_session.py`) is the **primary aggregate** (mutated by 35 of the 38 event types); `SharedStore` (`core/domain/shared_context.py`) is a second event-sourced aggregate (3 event types) sharing the events table via a `uuid5`-derived `aggregate_id`. All mutable state is derived by replaying frozen Pydantic `DomainEvent` instances. The replay mechanism uses `functools.singledispatchmethod` for `_apply`, dispatching on event type.

Optimistic concurrency control: the event store enforces a unique constraint on `(aggregate_id, sequence_number)`. Version mismatch raises `ConcurrencyError`; the system loop retries.

Key domain services (pure logic, no infrastructure dependencies):

| Service | File | Purpose |
|---------|------|---------|
| `SubtaskParser` | `core/domain/services/subtask_parser.py` | Parses LLM JSON output into `Subtask` list |
| `TaskScheduler` | `core/domain/services/task_scheduler.py` | Kahn's algorithm for DAG topological ordering |
| `ConfigResolver` | `core/domain/services/config_resolver.py` | Resolves agent LLM config per operation type |
| `ContextUpdateParser` | `core/domain/services/context_update_parser.py` | Extracts `<context-update>` XML from worker output |

Key value objects in `core/domain/values/`: `AgentConfig` (LLM config per agent), `AgentRole`/`AgentStatus` (str enums), `Subtask` (frozen decomposition unit, incl. `execution_mode`/`procedure_ref`), `HierarchyLimits` (depth/child/total limits + opaque `domain_context: object | None` slot), `LLMResponse`/`LLMToolResponse` (token usage wrappers), `NodeMessage`/`Handoff` (sibling coordination), `PromptTrace`/`SectionProvenance` (prompt audit trail), failure-context (`ThoughtExcerpt`, `ChildFailureRecord` in `failure.py`), procedure results (`ProcedureResult`, `ProcedureEvidence` in `procedure.py`), `ReconPolicy`, `ConstraintFailure`, `PromptCapabilities`.

### Ports (`core/ports/`)

18+ Protocol interfaces in 7 files. The event store follows ISP (Interface Segregation Principle) -- clients depend on the narrowest interface they need.

| File | Protocols | Design note |
|------|-----------|-------------|
| `event_store_port.py` | `EventStoreConnectPort`, `EventStoreWritePort`, `EventStoreReadPort`, composite `EventStorePort` | ISP split so read-only clients (projections, API) use `EventStoreReadPort` only |
| `runtime_ports.py` | `LLMPort`, `WorkerToolPort`, `CostCalculatorPort`, `RealtimeCallbackPort`, `SharedContextPort`, `SiblingViewPort`, `SystemLimitsPort`, `Toolset`, `ReconToolPort` | Consolidated because each is small. `ReconToolPort` extends `Toolset`. |
| `worker_port.py` | `WorkerPort` | Flat-mode single-agent worker (one process runs all phases). Distinct from the hierarchical `WorkerToolPort`; implementations live in `infrastructure/workers/` |
| `domain_plugin_port.py` | `DomainPlugin` | Separate file because it carries `PreparedRunWorkspace` and `WorkerExecutionContext` frozen dataclasses. Adds `get_procedure_executor()` for the deterministic tier |
| `decomposition_validator_port.py` | `DecompositionValidator` + `DecompositionVerdict`/`DecompositionViolation`/`SuggestedSubtask` values | Domain plugins validate/repair LLM decompositions against a role catalog |
| `shared_code_context_port.py` | `SharedCodeContextPort` | Capture worker source reads/edits for the shared code-prefix cache |
| `procedure_ports.py` | `ProcedureExecutorPort` (`match`/`resolve`/`execute`) + `NullProcedureExecutor` default | Deterministic procedure tier; `execute` runs host-side and returns a `ProcedureResult`. Null default => agentic-only |

### Application (`core/application/`)

**`AgentOrchestrator`** (`core/application/agent_orchestrator.py`) has three direct methods -- no pipeline, strategy pattern, or chain-of-responsibility wrapping. This is intentional; the operations are distinct enough that abstraction adds indirection without value.

| Method | Input role | What it does |
|--------|-----------|--------------|
| `assess_task` | PENDING | Single LLM call (optionally with tool-calling loop) decides execute (-> WORKER) or decompose (-> MANAGER + children) |
| `evaluate_task` | BOSS or MANAGER | LLM decomposes task -> `SubtasksDefined` -> spawn `ChildSpawned` events |
| `execute_task` | WORKER | Delegates to `WorkerToolPort.run_session()` -> streams events -> runs `VerificationPipeline` |

**`AgentExecutionService`** (`core/application/execution_service.py`) runs the system loop: poll for active agents, dispatch each via `run_agent_step`, manage concurrency via `asyncio.Semaphore`, handle retries and timeouts. One step = load aggregate -> dispatch action -> persist events -> handle post-step (spawn children, notify parents).

Application services in `core/application/services/`:

| Subdirectory | Key classes | Purpose |
|-------------|------------|---------|
| `lifecycle/` | `ChildAgentFactory`, `AgentRepository`, `ParentNotificationService`, `HierarchyLimitsRegistry`, `RoleDispatch` | Agent lifecycle, parent-child relationships, limit tracking |
| `orchestration/` | `LLMQueryExecutor`, `ContextCondenser`, `VerificationPipeline`, `RetryPolicy` | LLM interaction, context management, result verification, retry logic |
| `prompt/` | `PromptBuilder`, `PromptParser`, `PromptStrategy`, `PromptTraceService` | Jinja2 template composition, prompt parsing, domain-specific augmentation |
| `toolset/` | `ToolCallingService`, `ToolsetContext`, `ToolsetPolicyResolver`, `LoopPolicy` | Tool availability per role/phase, tool call dispatch, iteration limits |
| `query/` | `AgentQueryService`, `EventBroadcaster` | CQRS reads, SSE event broadcasting. `AgentQueryService` also implements `SiblingViewPort`. |

### Query (`core/query/`)

CQRS read-side projections. Pipeline architecture: `base/projection.py` defines the interface, `base/filter.py` and `base/formatter.py` define filter/format stages. Concrete implementations live in `projections/impl/`.

| Projection | File | Purpose |
|-----------|------|---------|
| `Summary` | `impl/summary.py` | Run-level summary with timing and status |
| `AgentList` | `impl/agent_list.py` | Flat list of all agents with metadata |
| `Cost` | `impl/cost.py` | Token/cost aggregation across agents |
| `AgentSummary` | `impl/agent_summary.py` | Per-agent detail with event timeline |

The `hierarchy_builder.py` and `hierarchy_collector.py` modules build tree views from flat event streams. The sink registry (`registry.py`) uses decorators for output sink discoverability, with concrete sinks in `infrastructure/adapters/sinks.py`.

## Infrastructure (`infrastructure/`)

Every adapter implements a `core/ports/` Protocol. No adapter is referenced by `core/` -- all injection happens in `bootstrap/`.

| Adapter | Port | File |
|---------|------|------|
| `PostgresEventStore` | `EventStorePort` | `infrastructure/adapters/postgres_event_store.py` |
| `LiteLLMAdapter` | `LLMPort` | `infrastructure/adapters/litellm_adapter.py` |
| `PostgresSharedContextAdapter` | `SharedContextPort` | `infrastructure/adapters/shared_context_adapter.py` |
| `CostCalculator` | `CostCalculatorPort` | `infrastructure/adapters/cost_calculator.py` |
| `ReconToolAdapter` | `ReconToolPort` | `infrastructure/adapters/recon_tool_adapter.py` |
| `ClaudeAgentSDKAdapter` | `WorkerToolPort` | `infrastructure/adapters/worker/claude_sdk_adapter.py` |
| `OpenHandsAdapter` | `WorkerToolPort` | `infrastructure/adapters/worker/openhands_adapter.py` |
| `GoogleADKAdapter` | `WorkerToolPort` | `infrastructure/adapters/worker/google_adk_adapter.py` |
| `OpenRouterAdapter` | `LLMPort` | `infrastructure/adapters/openrouter_adapter.py` |
| `ClaudeCodeWorker` | `WorkerPort` (flat mode) | `infrastructure/workers/claude_code_worker.py` |
| `OpenHandsWorker` | `WorkerPort` (flat mode; wraps `OpenHandsAdapter`) | `infrastructure/workers/openhands_worker.py` |

Flat-mode `WorkerPort` implementations in `infrastructure/workers/` are wired by
`bootstrap/composition.py::_build_flat_worker`; hierarchical `WorkerToolPort` adapters in
`infrastructure/adapters/worker/` are wired by `bootstrap/infrastructure.py`. They are two
distinct worker abstractions, not duplicates.

Worker adapters share cross-cutting utilities in `infrastructure/adapters/worker/shared/`: `container_session.py` (container lifecycle), `event_sequencer.py` (event ordering), `cost_calculator.py` (worker cost), `tool_formatters.py` (tool output formatting), `validation.py` (output validation).

`infrastructure/sql/create_events_table.sql` holds the DDL for the event store. Complex queries (recursive CTEs for hierarchy traversal, batch incremental reads for SSE) are built in `PostgresEventStore` methods.

## Bootstrap (`bootstrap/`)

Composition root. The ONLY module that imports from `plugins/`. `bootstrap/composition.py` contains a builder registry (`_DOMAIN_COMPONENT_BUILDERS`) mapping domain names (e.g., `"security"`) to factory functions that return `DomainComponents(plugin, prompt_strategy, domain_key)`.

| File | Responsibility |
|------|----------------|
| `bootstrap.py` | argparse CLI entry, command dispatch (`run`, `events`, `summary`, `list`, `prompts`) |
| `composition.py` | Domain plugin builder registry; imports `SecurityDomainPlugin` and `DockerSecBenchRuntime` from `plugins/` |
| `infrastructure.py` | `get_infrastructure()` -- creates all adapter instances from `InfrastructureConfig` |
| `application.py` | `get_application()` -- wires services with ports via `ApplicationConfig`. Creates `ExecutionLimitsBridge` for system limits. |
| `realtime_adapter.py` | `RealtimeCallbackAdapter` bridging domain events to SSE via `EventBroadcaster` |

Wiring flow: `bootstrap.py` -> `composition.py` (resolves domain plugin) -> `create_runtime_cli()` -> `get_infrastructure()` -> `get_application()` -> returns `CLI`.

## Plugins (`plugins/`)

**STRICTLY cybersecurity code only.** No orchestration, topology, or scheduling logic. If your change involves agent scheduling, tree traversal, or concurrency -- it does not belong here.

`plugins/security/` implements `DomainPlugin` and `PromptStrategy` protocols. It also defines its own port (`SecurityContainerRuntime` in `container_runtime.py`) implemented by `DockerSecBenchRuntime` in `docker_runtime.py`.

Domain context flows as `object | None` through `HierarchyLimits.domain_context`. Only the plugin downcasts to `CVEInstance`. Core remains fully domain-ignorant. This is why `domain_context` is typed as `object | None` rather than a concrete type -- the opaque slot keeps core decoupled from any specific domain.

Key files:

| File | Purpose |
|------|---------|
| `plugin.py` | `SecurityDomainPlugin` -- implements `DomainPlugin` protocol |
| `prompt_strategy.py` | `SecBenchPromptStrategy` -- domain-specific prompt template selection |
| `cve_instance.py` | `CVEInstance` frozen dataclass -- the opaque domain context object |
| `cve_inference.py` | Infers `CVEInstance` from task text or JSON fixture files |
| `container_runtime.py` | `SecurityContainerRuntime` protocol + `SecBenchWorkspace`, `SecBenchContainerSession` value objects |
| `docker_runtime.py` | `DockerSecBenchRuntime` -- concrete container lifecycle (prepare_workspace, start_session) |
| `image_resolver.py` | Resolves the correct Docker image tag for a given CVE |

## Prompt System (`prompts/`)

4-tier Jinja2 hierarchy loaded by `PromptBuilder` (`core/application/services/prompt/prompt_builder.py`). Templates are pure Jinja2 with no Python imports.

| Tier | Path | Content |
|------|------|---------|
| 1 | `prompts/system.j2` | Global system identity, constraints, output format |
| 2 | `prompts/roles/{boss,manager,pending,worker}.j2` | Per-role persona and behavioral rules |
| 3 | `prompts/operations/{assess,decomposition,execution}.j2` | Per-operation output format and instructions |
| 4 | `prompts/domains/secbench/*.j2` | Optional domain extension (SEC-bench CVE-specific templates) |

Context fragments in `prompts/context/`: `scope.j2` (file/symbol targeting), `sibling.j2` (coordination with sibling agents), `workspace.j2` (working directory state).

SEC-bench domain templates in `prompts/domains/secbench/`: `boss.j2`, `assess.j2`, `worker.j2`, `manager.j2`, `cve.j2` (CVE-specific context), `tools.j2` (available security tools). Subdirectories `worker/` and `manager/` hold operation-specific variants.

## Critical Data Flows

### 1. Task Execution (Happy Path)

```
main.py -> bootstrap.main() -> _run_task()
  -> composition.get_run_domain_components() resolves plugin
  -> create_runtime_cli() wires all dependencies
  -> CLI.run_task()
    -> AgentExecutionService.create_boss_agent(task)
      -> create AgentSession(role=BOSS)
      -> emit RunStarted event
      -> persist to EventStore
    -> AgentExecutionService.run_system_loop(root_id)
      -> poll for active agents (AgentQueryService)
      -> for each active agent:
          -> run_agent_step(agent_id)
            -> load AgentSession from events (AgentRepository)
            -> dispatch by role (RoleDispatch):
                BOSS     -> evaluate_task -> LLM decomposes -> SubtasksDefined + ChildSpawned
                PENDING  -> assess_task   -> LLM decides    -> WORKER or MANAGER+children
                MANAGER  -> evaluate_task -> LLM decomposes -> SubtasksDefined + ChildSpawned
                WORKER   -> execute_task  -> dispatch ladder: host procedure (deterministic) OR WorkerToolPort (agentic) -> verify
            -> persist uncommitted events (OCC via append_batch)
            -> handle post-step (spawn children, notify parent, retry if failed)
      -> all agents terminal -> emit RunCompleted
```

### 2. Event Sourcing Lifecycle

```
Command side:
  AgentSession.some_domain_method()
    -> _emit(DomainEvent)                    # create frozen Pydantic event
    -> _apply(event) via singledispatchmethod # mutate internal state
    -> event appended to self._changes       # uncommitted event buffer

Persistence:
  AgentRepository.persist_events(agent, base_version)
    -> EventStoreWritePort.append_batch(changes, expected_version=base_version)
    -> PostgreSQL INSERT with unique(aggregate_id, sequence_number)
    -> on conflict -> ConcurrencyError -> system loop retries

Read side:
  AgentRepository.load(agent_id)
    -> EventStoreReadPort.get_events(aggregate_id)
    -> AgentSession.load_from_history(events)
    -> replay each event through _apply singledispatch
    -> returns fully hydrated aggregate
```

### 3. Child Spawning

```
evaluate_task(BOSS/MANAGER)
  -> LLM response parsed into list[Subtask] (SubtaskParser)
  -> _check_limit_violations (max_children_per_node, max_total_agents)
  -> agent.apply_subtasks_and_spawn_children()
    -> emits SubtasksDefined event
    -> emits ChildSpawned event per subtask (role=PENDING or forced WORKER)
  -> post-step: ChildAgentFactory.create_children_from_events()
    -> creates child AgentSession instances
    -> registers in HierarchyLimitsRegistry (depth + 1, inherited domain_context)
    -> persists each child
  -> system loop picks up new active children on next poll
```

### 4. Verification (WORKER completion)

```
execute_task completes -> agent.status == COMPLETED
  -> VerificationPipeline.verify(agent)
    -> Stage 1: Structural checks (output format)
    -> Stage 2: Deterministic checks (file existence, syntax)
    -> Stage 3: Execution checks (compile, test run)
    -> Stage 4: LLM judge (semantic correctness assessment, skippable via skip_judge)
  -> on failure -> VerificationFailed event -> agent may be retried
```

### 5. SEC-bench Container Lifecycle

```
SecurityDomainPlugin.prepare_run()
  -> cve_inference: task text -> CVEInstance
  -> image_resolver: CVEInstance -> Docker image tag
  -> DockerSecBenchRuntime.prepare_workspace()
    -> creates host directory structure (source, testcase, work)
    -> returns SecBenchWorkspace

SecurityDomainPlugin.prepare_worker_execution()
  -> DockerSecBenchRuntime.start_session(workspace)
    -> docker create + start container
    -> returns SecBenchContainerSession
  -> returns WorkerExecutionContext(working_directory, task_context)

Worker executes inside container...

SecurityDomainPlugin.cleanup_worker_execution()
  -> intentional no-op: the run's shared container persists across workers
     and is reaped at process exit by the PID-labeled cleanup registry
```

### 6. Self-Healing Failure Loop (worker crash -> informed retry -> re-decomposition)

Deterministic, LLM-free resilience layered on top of `RetryPolicy`. Turns an opaque crash into context-rich guidance for the next attempt.

```
WORKER raises / returns failure
  -> worker adapter attaches a bounded traceback (describe_error, ~1.5 KB)
     to the failure reason           (infrastructure/adapters/worker/shared/errors.py)
  -> execution_service._handle_post_step: role==WORKER && no verification_feedback && failure_digest is None
       -> digest = build_failure_digest(agent)      # LLM-free; FAILURE / LAST TOOL CALLS / ATTEMPT, cap 4000
       -> agent.record_failure_digest(digest, "worker_crash")   # FailureDigestRecorded (best-effort)
  -> _maybe_retry_worker: retry if (verification_feedback OR failure_digest), budget verification_max_retries
       -> RetryScheduled (digest + feedback retained) -> retry prompt gains "## Previous Attempt Failure (Retry)"
  -> if the worker still fails as one of N sibling children:
       parent.handle_child_failure(child_id, reason, child_task, digest, max_redecompositions)
         -> ChildFailed (enriched) -> ChildFailureRecord kept in failed_children
         -> ALL children failed?
              budget left -> trigger_redecomposition: failure_history <- failed_children
                             -> RedecompositionTriggered -> parent re-plans with a
                                <previous_attempt_failures> block (prompts/operations/decomposition_variable.j2,
                                the volatile tail; cached prompt prefix stays byte-identical)
              no budget   -> WorkFailed (propagate up)
```

### 7. Procedural Dispatch Ladder (deterministic tier; `settings.orchestration.procedural_dispatch`)

Flag-gated. Default off binds `NullProcedureExecutor`, so `_resolve_procedure_ref` always returns `None` and the flow is byte-identical to the agentic-only path.

```
execute_task(WORKER)
  -> agent_orchestrator._resolve_procedure_ref(agent) reads subtask.execution_mode + procedure_ref:
       "agentic"                         -> None            (bypass; always agentic)
       "procedural" + executor.resolve(ref) -> ref
       "procedural" + unknown ref        -> None + warning  (fail open -> auto)
       "auto"                            -> executor.match(task, domain_context)   (may be None)
  -> ref is None  -> agentic path: WorkerToolPort.run_session -> stream events -> VerificationPipeline
  -> ref present  -> deterministic path (zero LLM turns: no prompt, no worker session, no cost events):
       agent.start_procedure(ref)                # ProcedureExecutionStarted; last_attempt_procedural=True
       result = executor.execute(ref, task, domain_context, {root_id from hierarchy_limits, ...})
       agent.finish_procedure(ref, success, summary, evidence)   # ProcedureExecutionFinished (host evidence)
       success -> complete_with_result(summary)  # WorkCompleted -> normal VerificationPipeline
       failure -> record_failure_digest(digest, "procedure_failure") + fail_with_reason   # WorkFailed
  -> escalation (execution_service._handle_post_step):
       failed procedural attempt (last_attempt_procedural && retry_count==0)
         -> schedule_retry: exactly one guaranteed AGENTIC retry (dispatch never routes procedural twice),
            the agentic retry prompt carrying the procedure's digest
```

## Agent Hierarchy and Roles

```
BOSS (root, created by ExecutionService)
  -> evaluate_task -> spawns PENDING children
    PENDING (initial state for all non-root agents)
      -> assess_task -> becomes WORKER or MANAGER
        WORKER -> execute_task -> external tool runs task -> COMPLETED or FAILED
        MANAGER -> evaluate_task -> spawns more PENDING children -> recurse
```

Roles: `BOSS` (always root), `PENDING` (unassessed), `MANAGER` (decomposes), `WORKER` (executes).
Statuses: `CREATED` -> `ANALYZING` -> `IN_PROGRESS` / `COMPLETED` / `FAILED`.

DAG scheduling: When a parent decomposes into subtasks with `depends_on` edges, `TaskScheduler` uses Kahn's algorithm to determine topological execution order. Children whose dependencies have completed become active; others wait.

## Where Does New Code Go?

Use this decision tree:

```
Is it pure domain logic (no I/O, no external deps)?
  YES -> core/domain/
    Is it a state transition or business rule?  -> core/domain/aggregates/ or core/domain/services/
    Is it a new event type?                     -> core/domain/events/events.py + _apply handler
    Is it a value object?                       -> core/domain/values/

  NO -> Does it define an interface the domain needs?
    YES -> core/ports/ (Protocol class)

  NO -> Does it implement an external integration?
    YES -> infrastructure/adapters/ (must implement a port)

  NO -> Does it orchestrate domain + ports without I/O decisions?
    YES -> core/application/services/

  NO -> Is it cybersecurity-specific?
    YES -> plugins/security/ (ONLY security code)

  NO -> Is it about wiring dependencies together?
    YES -> bootstrap/

  NO -> Is it user-facing output or CLI?
    YES -> presentation/

  NO -> Is it a prompt template?
    YES -> prompts/ (appropriate tier)

  NO -> Is it a read-side query or API endpoint?
    YES -> query/api/ (FastAPI) or core/query/ (projection)

  NO -> Is it configuration?
    YES -> config/settings.py (model) + config/config.yaml (default value)
```

Quick reference table:

| You want to... | Put it in... | Key constraint |
|----------------|-------------|----------------|
| Add business rules or state transitions | `core/domain/` | No infrastructure imports |
| Add a new event type | `core/domain/events/events.py` + `_apply` in `agent_session.py` | Must be frozen Pydantic model |
| Add a new value object | `core/domain/values/` | Frozen Pydantic model or frozen dataclass |
| Define a new interface | `core/ports/` | Protocol class only |
| Implement an external integration | `infrastructure/adapters/` | Must implement a port |
| Add an application service | `core/application/services/` | Can use ports, not adapters |
| Wire adapters to ports | `bootstrap/` | Only place for cross-boundary imports |
| Add cybersecurity code | `plugins/security/` | ONLY security code allowed |
| Add a prompt template | `prompts/` (appropriate tier) | Jinja2 only, loaded by PromptBuilder |
| Add a CLI command | `presentation/cli.py` + `bootstrap/bootstrap.py` | Click command |
| Add an API endpoint | `query/api/routes/` | FastAPI router |
| Add a CQRS projection | `core/query/projections/impl/` | Register in pipeline/registry |
| Add configuration | `config/settings.py` + `config/config.yaml` | Pydantic model + YAML key |

## What NOT To Do

- **Never** import from `infrastructure/` in any `core/` file. The pre-commit hook will reject it.
- **Never** put orchestration, topology, or scheduling logic in `plugins/`. It belongs in `core/`.
- **Never** create pipeline/strategy/chain-of-responsibility abstractions around the 3 orchestrator methods. They are intentionally direct.
- **Never** mutate `AgentSession` state directly. All mutations flow through `_emit(event)` -> `_apply(event)`.
- **Never** reference a concrete adapter from `core/`. Use the Protocol from `core/ports/`.
- **Never** import `plugins/` from anywhere except `bootstrap/composition.py`.
- **Never** use mutable Pydantic models for events or value objects. All must have `model_config = {"frozen": True}`.
- **Never** add new worker/tool adapter classes without implementing `WorkerToolPort` protocol and registering in `bootstrap/infrastructure.py`.

## Cross-References

For deeper context on specific topics:

```bash
cat .claude/docs/conventions.md      # Naming, imports, error handling, logging, types
cat .claude/docs/patterns.md         # Step-by-step recipes for common implementation tasks
cat .claude/docs/dependencies.md     # Adding or modifying dependencies
cat .claude/docs/testing.md          # Test conventions and fixtures
cat .claude/docs/core-domain.md      # AgentSession aggregate, events, value objects in detail
cat .claude/docs/plugins-security.md # Security plugin internals
cat .claude/docs/common-errors.md    # Debugging common failures
cat .claude/docs/workflows.md        # Build, deploy, CI changes
```
