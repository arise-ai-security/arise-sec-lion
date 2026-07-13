"""Application Layer - Service Factories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    FlatInvariantBuilder,
    HierarchyLimitsRegistry,
    ProgressCallback,
    ServiceConfig,
)
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    ContextCondenser,
    EventBroadcaster,
    LLMQueryExecutor,
    LoopPolicy,
    ParentNotificationService,
    PromptBuilder,
    SiblingViewService,
    ToolCallingService,
    ToolsetPolicyResolver,
)

from .realtime_adapter import RealtimeCallbackAdapter


if TYPE_CHECKING:
    from typing import Literal

    from config import (
        BossConfig,
        ConcurrencyConfig,
        ManagerConfig,
        ToolCallingConfig,
        TopologyConfig,
    )
    from core.application.services import PromptStrategy
    from core.ports.domain_plugin_port import DomainPlugin
    from core.ports.event_store_port import EventStoreReadPort
    from core.ports.worker_port import WorkerPort
    from presentation.cli import CLI, CLIConfig

    from .infrastructure import Infrastructure


@dataclass(frozen=True)
class ExecutionLimitsBridge:
    """Combines topology + concurrency for AgentExecutionService.

    Temporary bridge until Change B destructures these into the service
    constructor directly.
    """

    max_depth: int
    max_children_per_node: int
    max_total_agents: int
    max_concurrent_workers: int
    max_concurrent_llm_calls: int = 5
    llm_jitter_max_ms: int = 500
    max_run_duration_seconds: float = 1800
    max_agent_step_seconds: float = 900.0

    def is_workers_limited(self) -> bool:
        return self.max_concurrent_workers > 0

    def is_llm_limited(self) -> bool:
        return self.max_concurrent_llm_calls > 0


@dataclass
class ApplicationConfig:
    """Configuration for application services."""

    topology: TopologyConfig
    concurrency: ConcurrencyConfig
    tool_calling: ToolCallingConfig
    max_retries: int
    poll_interval: float
    boss_config: BossConfig
    manager_config: ManagerConfig
    output_directory: str
    default_worker_tool: str
    max_run_duration_seconds: float = 1800
    max_redecompositions: int = 2
    skip_judge: bool = False
    workspace_listing_dirs: tuple[str, ...] | None = None
    workspace_listing_max_entries: int | None = None
    verification_max_retries: int = 2
    capture_recon_reads: bool = False
    share_boss_recon: bool = False
    procedural_dispatch: bool = False
    treatment_version: str | None = None
    config_hash: str | None = None
    domain_plugin: DomainPlugin | None = None
    prompt_strategy: PromptStrategy | None = None
    prompt_builder: PromptBuilder | None = None
    progress_callback: ProgressCallback | None = None
    domain_key: str | None = None
    mode: Literal["hierarchical", "flat"] = "hierarchical"
    flat_worker: WorkerPort | None = None
    flat_invariant_builder: FlatInvariantBuilder | None = None

    def __post_init__(self) -> None:
        if self.mode == "flat" and (
            self.flat_worker is None or self.flat_invariant_builder is None
        ):
            raise ValueError(
                "orchestration.mode='flat' requires both flat_worker and flat_invariant_builder"
            )


@dataclass
class Application:
    """Container for all application services."""

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig,
) -> Application:
    service_config = ServiceConfig(
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        default_worker_tool=config.default_worker_tool,
        boss_config=config.boss_config,
        manager_config=config.manager_config,
        mode=config.mode,
        workspace_listing_dirs=config.workspace_listing_dirs,
        workspace_listing_max_entries=config.workspace_listing_max_entries,
        verification_max_retries=config.verification_max_retries,
        treatment_version=config.treatment_version,
        config_hash=config.config_hash,
    )

    # Create collaborators (composition root wiring). Prefer the
    # PromptBuilder constructed by ``create_runtime_cli`` so the flat-mode
    # closure and the live service share one instance; fall back to
    # constructing one for callers that don't set the field.
    prompt_builder = config.prompt_builder or PromptBuilder(
        "prompts",
        config.default_worker_tool,
        strategy=config.prompt_strategy,
    )

    repository = AgentRepository(
        event_store=infrastructure.event_store,
        max_retries=config.max_retries,
        progress_callback=config.progress_callback,
    )
    limits_registry = HierarchyLimitsRegistry()
    query_service = AgentQueryService(
        repository=repository,
        shared_context_port=infrastructure.shared_context,
    )
    sibling_view = SiblingViewService(
        repository=repository,
        shared_context_port=infrastructure.shared_context,
    )
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=config.topology.max_total_agents,
        manager_config=config.manager_config,
        default_worker_tool=config.default_worker_tool,
    )

    # Create event broadcaster and adapter for real-time streaming
    event_broadcaster = EventBroadcaster.get_instance()
    realtime_callback = RealtimeCallbackAdapter(event_broadcaster)

    condenser = ContextCondenser(
        llm_port=infrastructure.llm_adapter,
        result_char_limit=config.tool_calling.result_char_limit,
        condense_after_iteration=config.tool_calling.condense_after_iteration,
        token_budget=config.tool_calling.token_budget,
    )

    tool_calling_service = ToolCallingService(
        llm_port=infrastructure.llm_adapter,
        condenser=condenser,
    )
    llm_query_executor = LLMQueryExecutor(
        llm_port=infrastructure.llm_adapter,
        tool_calling_service=tool_calling_service,
    )
    toolset_resolver = ToolsetPolicyResolver(
        toolsets=[infrastructure.recon_tool],
        config=config.tool_calling.policies.to_raw_dict(),
        default_loop_policy=LoopPolicy(
            max_iterations=config.tool_calling.max_iterations,
            result_char_limit=config.tool_calling.result_char_limit,
        ),
        domain_key=config.domain_key,
    )

    orchestrator = AgentOrchestrator(
        llm_port=infrastructure.llm_adapter,
        worker_port=infrastructure.worker_tool,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
        realtime_callback=realtime_callback,
        llm_query_executor=llm_query_executor,
        toolset_resolver=toolset_resolver,
        skip_judge=config.skip_judge,
        format_repairer=infrastructure.format_repairer,
        shared_code_port=infrastructure.shared_code_context,
        capture_recon_reads=config.capture_recon_reads,
        share_boss_recon=config.share_boss_recon,
        decomposition_policy=(
            config.domain_plugin.get_decomposition_policy()
            if config.domain_plugin is not None
            else None
        ),
        decomposition_validator=(
            config.domain_plugin.get_decomposition_validator()
            if config.domain_plugin is not None
            else None
        ),
        include_manager_layer=config.topology.include_manager_layer,
        max_redecompositions=config.max_redecompositions,
        procedure_executor=(
            config.domain_plugin.get_procedure_executor()
            if config.procedural_dispatch and config.domain_plugin is not None
            else None
        ),
    )

    parent_notifier = ParentNotificationService(
        repository=repository,
        max_redecompositions=config.max_redecompositions,
        progress_callback=config.progress_callback,
    )

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=infrastructure.shared_context,
        sibling_view_port=sibling_view,
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
        domain_plugin=config.domain_plugin,
        recon_tool=infrastructure.recon_tool,
        flat_worker=config.flat_worker,
        flat_invariant_builder=config.flat_invariant_builder,
    )

    system_limits = ExecutionLimitsBridge(
        max_depth=config.topology.max_depth,
        max_children_per_node=config.topology.max_children_per_node,
        max_total_agents=config.topology.max_total_agents,
        max_concurrent_workers=config.concurrency.max_concurrent_workers,
        max_concurrent_llm_calls=config.concurrency.max_concurrent_llm_calls,
        llm_jitter_max_ms=config.concurrency.llm_jitter_max_ms,
        max_agent_step_seconds=config.concurrency.max_agent_step_seconds,
        max_run_duration_seconds=config.max_run_duration_seconds,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        dependencies=dependencies,
        config=service_config,
        system_limits=system_limits,
        progress_callback=config.progress_callback,
    )

    return Application(execution_service=execution_service)


def get_cli(
    execution_service: AgentExecutionService,
    event_store: EventStoreReadPort,
    config: CLIConfig | None = None,
) -> CLI:
    from presentation.cli import CLI

    return CLI(execution_service=execution_service, event_store=event_store, config=config)
