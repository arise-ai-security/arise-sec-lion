"""Application Layer - Service Factories."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import BossConfig, ManagerConfig, OrchestrationConfig, ReconConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    HierarchyLimitsRegistry,
    ProgressCallback,
    ServiceConfig,
)
from core.application.services.agent_repository import AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.event_broadcaster import EventBroadcaster
from core.application.services.parent_notifier import ParentNotificationService
from core.application.services.query_service import AgentQueryService
from core.application.services.prompt_builder import PromptBuilder
from core.application.services.context_condenser import ContextCondenser
from core.application.services.tool_calling_service import ToolCallingService

from .infrastructure import Infrastructure
from .realtime_adapter import RealtimeCallbackAdapter

if TYPE_CHECKING:
    from core.ports.domain_plugin_port import DomainPlugin


@dataclass
class ApplicationConfig:
    """Configuration for application services."""

    system_limits: OrchestrationConfig.LimitsConfig
    max_retries: int
    poll_interval: float
    boss_config: BossConfig
    manager_config: ManagerConfig
    output_directory: str
    default_worker_tool: str
    recon_config: ReconConfig = field(default_factory=ReconConfig)
    domain_plugin: "DomainPlugin | None" = None
    progress_callback: ProgressCallback | None = None


@dataclass
class Application:
    """Container for all application services."""

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig,
) -> Application:
    """Create all application services with proper dependency injection."""
    service_config = ServiceConfig(
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        default_worker_tool=config.default_worker_tool,
        boss_config=config.boss_config,
        manager_config=config.manager_config,
    )

    # Create collaborators (composition root wiring)
    prompt_builder = PromptBuilder(
        "prompts",
        config.default_worker_tool,
        domain_plugin=config.domain_plugin,
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
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=config.system_limits.max_total_agents,
        manager_config=config.manager_config,
    )

    # Create event broadcaster and adapter for real-time streaming
    event_broadcaster = EventBroadcaster.get_instance()
    realtime_callback = RealtimeCallbackAdapter(event_broadcaster)

    condenser = ContextCondenser(
        llm_port=infrastructure.llm_adapter,
        result_char_limit=config.system_limits.recon_result_char_limit,
        condense_after_iteration=config.system_limits.recon_condense_after_iteration,
        token_budget=config.system_limits.recon_token_budget,
    )

    tool_calling_service = ToolCallingService(
        llm_port=infrastructure.llm_adapter,
        recon_port=infrastructure.recon_tool,
        max_iterations=config.system_limits.max_recon_iterations,
        condenser=condenser,
    )

    orchestrator = AgentOrchestrator(
        llm_port=infrastructure.llm_adapter,
        worker_port=infrastructure.worker_tool,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
        realtime_callback=realtime_callback,
        tool_calling_service=tool_calling_service,
        recon_config=config.recon_config.to_raw_dict(),
    )

    parent_notifier = ParentNotificationService(
        repository=repository,
        progress_callback=config.progress_callback,
    )

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=infrastructure.shared_context,
        sibling_view_port=query_service,  # AgentQueryService implements SiblingViewPort
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
        domain_plugin=config.domain_plugin,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        dependencies=dependencies,
        config=service_config,
        system_limits=config.system_limits,
        progress_callback=config.progress_callback,
    )

    return Application(execution_service=execution_service)


def get_cli(
    execution_service: AgentExecutionService,
    config: "CLIConfig | None" = None,
) -> "CLI":
    """Create CLI interface."""
    from presentation.cli import CLI, CLIConfig

    return CLI(execution_service=execution_service, config=config)
