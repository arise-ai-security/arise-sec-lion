# Project Structure

```
arise-sec-lion/
├── core/                    # Domain core (NEVER imports from infrastructure/ or plugins/)
│   ├── domain/
│   │   ├── aggregates/
│   │   │   └── agent_session.py     # AgentSession — event-sourced aggregate root
│   │   ├── events/
│   │   │   └── events.py            # ~31 frozen DomainEvent Pydantic models
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
│   │   │   ├── constraint_failure.py # ConstraintFailure
│   │   │   ├── llm_response.py      # LLMUsage, LLMResponse, LLMToolResponse, ToolCall
│   │   │   ├── parsed_context.py    # ParsedDecision, ParsedArtifact, ParsedUpdate
│   │   │   ├── prompt_capabilities.py # PromptToolDescriptor, PromptCapabilities
│   │   │   ├── prompt_trace.py      # PromptSection, ParsedPrompt, HierarchyTrace
│   │   │   ├── recon_policy.py      # ReconPolicy (tool-calling loop control)
│   │   │   ├── json_types.py        # JSON type aliases
│   │   │   └── context/             # Re-export shim → node_message.py + limits.py
│   │   ├── shared_context.py        # SharedStore (ArtifactStore + DecisionLog)
│   │   └── exceptions.py
│   ├── ports/
│   │   ├── event_store_port.py      # EventStoreConnectPort, WritePort, ReadPort (+ composite)
│   │   ├── runtime_ports.py         # LLMPort, WorkerToolPort, CostCalculatorPort,
│   │   │                            #   RealtimeCallbackPort, SharedContextPort, SiblingViewPort,
│   │   │                            #   SystemLimitsPort, Toolset, ReconToolPort
│   │   └── domain_plugin_port.py    # DomainPlugin protocol (optional domain extensions)
│   ├── application/
│   │   ├── agent_orchestrator.py    # 3 direct methods: assess_task, evaluate_task, execute_task
│   │   ├── execution_service.py     # Main loop, retry, concurrency
│   │   ├── dtos.py                  # AgentResultDTO, SystemStatisticsDTO
│   │   └── services/
│   │       ├── agent_repository.py      # Load/save agents (event replay + OCC)
│   │       ├── child_factory.py         # Spawn children from ChildSpawned events
│   │       ├── parent_notifier.py       # Recursive notification, infeasible re-decomposition
│   │       ├── query_service.py         # DAG scheduling, sibling view, subtree caching
│   │       ├── prompt_builder.py        # Jinja2 TemplateChain composition
│   │       ├── prompt_strategy.py       # PromptStrategy protocol
│   │       ├── prompt_parser.py         # XML section extraction with provenance
│   │       ├── prompt_trace_service.py  # Hierarchy trace building
│   │       ├── event_broadcaster.py     # In-memory pub/sub for SSE
│   │       ├── role_dispatch.py         # Role-based handler dispatch (Pending/Evaluator/Worker)
│   │       ├── tool_calling_service.py  # Iterative tool-calling with result aggregation
│   │       ├── toolset_policy_resolver.py # Per-role/per-domain tool access control
│   │       ├── toolset_context.py       # Active tool context building
│   │       ├── context_condenser.py     # Context compression for prompts
│   │       ├── hierarchy_limits_registry.py # Limit tracking registry
│   │       ├── llm_query_executor.py    # LLM invocation wrapper
│   │       ├── retry_policy.py          # Retry configuration
│   │       └── verification_pipeline.py # Multi-stage response verification
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
│       ├── recon_tool_adapter.py    # ReconToolPort → tree-sitter + grep + glob
│       ├── secbench_runtime.py      # DockerSecBenchRuntime (container management)
│       ├── sinks.py                 # Stdout, Stderr, File, String, Callback, Multi, Stream
│       └── worker/
│           ├── base.py              # WorkerAdapterBase (template method)
│           ├── claude_sdk_adapter.py # Claude Agent SDK
│           ├── openhands_adapter.py  # OpenHands AI developer
│           ├── google_adk_adapter.py # Google ADK with Gemini
│           └── shared/              # EventSequencer, ModelPricing, ToolFormatters, validation
├── plugins/
│   └── security/                    # Security domain plugin (optional, fully removable)
│       ├── plugin.py                # SecurityDomainPlugin (implements DomainPlugin)
│       ├── prompt_strategy.py       # SecBenchPromptStrategy (implements PromptStrategy)
│       ├── cve_instance.py          # CVEInstance frozen model
│       ├── cve_inference.py         # CVEInstanceInferenceService
│       ├── benchmark_result.py      # BenchmarkResult, StageResult
│       ├── container_runtime.py     # Container runtime management
│       ├── image_resolver.py        # Docker image resolution
│       └── security_tool.py         # Valgrind, KLEE tool interfaces
├── bootstrap/
│   ├── bootstrap.py                 # Argparse, command dispatch entry point
│   ├── composition.py               # Domain component builders (sole cross-boundary import)
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
├── prompts/                         # Jinja2 LLM prompt templates (4-tier)
│   ├── system.j2                    # Tier 1: System identity
│   ├── roles/                       # Tier 2: Role personas (boss, manager, worker, pending)
│   ├── operations/                  # Tier 3: Operation instructions (assess, decomposition, execution)
│   ├── context/                     # Context injection (workspace, sibling, scope)
│   └── domains/                     # Tier 4: Domain specialization
│       └── secbench/                # SEC-bench (boss, cve, tools, manager/*, worker/*)
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

`core/` never imports from outer layers. Only `bootstrap/composition.py` imports from `plugins/`.
