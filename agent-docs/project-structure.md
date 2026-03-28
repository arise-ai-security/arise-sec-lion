# Project Structure

```
arise-sec-lion/
├── core/                    # Domain core (NEVER imports from infrastructure/)
│   ├── domain/
│   │   ├── aggregates/
│   │   │   └── agent_session.py     # AgentSession — event-sourced aggregate root
│   │   ├── events/
│   │   │   └── events.py            # ~30 frozen DomainEvent Pydantic models
│   │   ├── services/
│   │   │   ├── config_resolver.py   # Heuristic config adjustments per operation
│   │   │   ├── context_update_parser.py  # XML <context-update> extraction
│   │   │   ├── subtask_parser.py    # LLM JSON → list[Subtask] | ConstraintFailure
│   │   │   └── task_scheduler.py    # Kahn's algorithm DAG ordering
│   │   ├── values/
│   │   │   ├── node_message.py      # Briefing/Report/Handoff discriminated union
│   │   │   ├── limits.py            # HierarchyLimits (depth/children/agent budget)
│   │   │   ├── enums.py             # AgentRole, AgentStatus
│   │   │   ├── agent_config.py      # AgentConfig, HeuristicConfig
│   │   │   ├── subtask.py           # Subtask (with depends_on for DAG)
│   │   │   ├── cve_instance.py      # CVEInstance for SEC-bench
│   │   │   ├── benchmark_result.py  # StageResult, BenchmarkResult
│   │   │   ├── constraint_failure.py # ConstraintFailure
│   │   │   ├── llm_response.py      # LLMUsage, LLMResponse
│   │   │   ├── parsed_context.py    # ParsedDecision, ParsedArtifact, ParsedUpdate
│   │   │   ├── prompt_trace.py      # PromptSection, ParsedPrompt, HierarchyTrace
│   │   │   └── context/             # Re-export shim → node_message.py + limits.py
│   │   ├── shared_context.py        # SharedStore (ArtifactStore + DecisionLog)
│   │   └── exceptions.py
│   ├── ports/
│   │   ├── event_store_port.py      # EventStoreConnectPort, WritePort, ReadPort (+ composite)
│   │   └── runtime_ports.py         # LLMPort, WorkerToolPort, CostCalculatorPort,
│   │                                #   RealtimeCallbackPort, SharedContextPort, SiblingViewPort
│   ├── application/
│   │   ├── agent_orchestrator.py    # 3 direct methods: evaluate_complexity/task, execute_task
│   │   ├── execution_service.py     # Main loop, retry, concurrency, HierarchyLimitsRegistry
│   │   ├── dtos.py                  # AgentResultDTO, SystemStatisticsDTO
│   │   └── services/
│   │       ├── agent_repository.py  # Load/save agents (event replay + OCC)
│   │       ├── child_factory.py     # Spawn children from ChildSpawned events
│   │       ├── parent_notifier.py   # Recursive notification, infeasible re-decomposition
│   │       ├── query_service.py     # DAG scheduling, sibling view, subtree caching
│   │       ├── prompt_builder.py    # Jinja2 TemplateChain composition
│   │       ├── prompt_strategy.py   # PromptStrategy protocol and SEC-bench extensions
│   │       ├── prompt_parser.py     # XML section extraction with provenance
│   │       ├── prompt_trace_service.py # Hierarchy trace building
│   │       ├── event_broadcaster.py # In-memory pub/sub for SSE
│   │       └── cve_inference.py     # CVEInstance extraction from task text
│   └── query/
│       ├── ports/sink_port.py       # SinkPort protocol
│       └── projections/             # Pipeline: EventStore → Collector → Filter → Formatter → Sink
│           ├── pipeline.py          # ProjectionPipeline + builder
│           ├── registry.py          # Plugin registration
│           ├── base/                # Abstract Projection, EventFilter, Formatter
│           ├── filters/impl.py      # IncludeAll, ErrorOnly, Agent, EventType, Composite
│           ├── formatters/impl.py   # JSON, JSONL, Text, CompactText
│           ├── impl/                # Summary, AgentList, Cost, AgentSummary projections
│           ├── models.py            # ProjectionSummary read model
│           ├── hierarchy_collector.py
│           └── hierarchy_builder.py
├── infrastructure/
│   └── adapters/
│       ├── postgres_event_store.py  # EventStorePort → asyncpg
│       ├── litellm_adapter.py       # LLMPort → LiteLLM (any provider)
│       ├── shared_context_adapter.py # SharedContextPort → PostgreSQL
│       ├── cost_calculator.py       # CostCalculatorPort
│       ├── sinks.py                 # Stdout, Stderr, File, String, Callback, Multi, Stream
│       └── worker/
│           ├── base.py              # WorkerAdapterBase (template method)
│           ├── claude_sdk_adapter.py # Claude Agent SDK
│           ├── openhands_adapter.py  # OpenHands AI developer
│           ├── google_adk_adapter.py # Google ADK with Gemini
│           └── shared/              # EventSequencer, ModelPricing, ToolFormatters, validation
├── bootstrap/
│   ├── bootstrap.py                 # Composition root, argparse, command dispatch
│   ├── infrastructure.py            # InfrastructureConfig, adapter factory
│   ├── application.py               # ApplicationConfig, service wiring
│   └── realtime_adapter.py          # RealtimeCallbackPort → EventBroadcaster bridge
├── presentation/
│   ├── cli.py                       # CLI class: init → create BOSS → run loop → display
│   ├── formatters/                  # event_formatter.py, prompt_trace_formatter.py
│   ├── persistence/                 # run_persistence.py (last run ID)
│   └── rendering/                   # renderer.py (banner, steps, results, tables)
├── query/
│   └── api/
│       ├── app.py                   # FastAPI factory + lifespan + static SPA serving
│       ├── bootstrap.py             # API-specific wiring
│       ├── routes/                  # agents, events (SSE), prompts, config, prompt_trace
│       └── schemas.py               # Pydantic response schemas
├── config/
│   ├── settings.py                  # Pydantic Settings (YAML + env vars)
│   ├── config.yaml                  # Base defaults
│   ├── config.dev.yaml              # Dev overrides
│   ├── config.development.yaml
│   └── config.production.yaml
├── prompts/                         # Jinja2 LLM prompt templates
│   ├── base/coordinator.j2
│   ├── core/roles/                  # boss, manager, worker, pending
│   ├── core/strategies/             # complexity, decomposition
│   ├── core/output/                 # complexity, subtasks format specs
│   ├── core/context/                # ancestry, artifacts, children, decisions, etc.
│   ├── core/worker/                 # execution, security_ops
│   ├── secbench/                    # SEC-bench specialization (boss, manager/*, worker/*)
│   └── security/                    # Security overlays
├── deployment/                      # Docker, docker-compose (see deployment/README.md)
└── main.py                          # Entry: bootstrap.bootstrap.main()
```

## Dependency Flow

```
presentation/  ──┐
query/api/     ──┼──► bootstrap/ ──► core/application/ ──► core/domain/
                 │                                              │
                 │                                         core/ports/ (interfaces)
                 │                                              ▲
                 └──────────────► infrastructure/adapters/ ─────┘ (implements)
```

`core/` never imports from outer layers.
