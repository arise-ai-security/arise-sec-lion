"""Application Layer - Service Factories."""

from dataclasses import dataclass

from config import BossConfig, ManagerConfig, OrchestrationConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    ProgressCallback,
    ServiceConfig,
)
from core.application.services.agent_repository import AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.context_registry import HierarchyLimitsRegistry
from core.application.services.parent_notifier import (
    BudgetRecollectionConfig,
    ParentNotificationService,
)
from core.application.services.query_service import AgentQueryService
from core.application.services.sibling_context_builder import SiblingViewBuilder
from core.application.services.workspace_context import WorkspaceContextProvider
from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_strategy import SecBenchPromptStrategy

from .infrastructure import Infrastructure


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
    progress_callback: ProgressCallback | None = None
    # Full orchestration config for complexity budget and other features
    orchestration_config: OrchestrationConfig | None = None


@dataclass
class Application:
    """Container for all application services."""

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig,
) -> Application:
    """Create all application services with proper dependency injection."""
    # Extract complexity budget config if available
    complexity_budget_enabled = False
    complexity_budget_initial_amount = 1000.0
    if config.orchestration_config is not None:
        budget_cfg = config.orchestration_config.complexity_budget
        complexity_budget_enabled = budget_cfg.enabled
        complexity_budget_initial_amount = budget_cfg.initial_amount

    service_config = ServiceConfig(
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        default_worker_tool=config.default_worker_tool,
        boss_config=config.boss_config,
        manager_config=config.manager_config,
        complexity_budget_enabled=complexity_budget_enabled,
        complexity_budget_initial_amount=complexity_budget_initial_amount,
    )

    # Create collaborators (composition root wiring)
    prompt_builder = PromptBuilder("prompts", config.default_worker_tool)
    prompt_builder.set_strategy(SecBenchPromptStrategy(prompt_builder.chain))

    repository = AgentRepository(
        event_store=infrastructure.event_store,
        max_retries=config.max_retries,
        progress_callback=config.progress_callback,
    )
    limits_registry = HierarchyLimitsRegistry()
    workspace = WorkspaceContextProvider()
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=config.system_limits.max_total_agents,
        manager_config=config.manager_config,
    )
    orchestrator = AgentOrchestrator(
        llm_port=infrastructure.llm_adapter,
        worker_port=infrastructure.worker_tool,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
        orchestration_config=config.orchestration_config,
        shared_context_port=infrastructure.shared_context,  # Design Choice 5
    )

    # Create sibling view builder (implements SiblingViewPort)
    sibling_view_builder = SiblingViewBuilder(
        repository=repository,
        shared_context_port=infrastructure.shared_context,
    )

    # Create parent notification service with budget recollection config (Design Choice 3)
    budget_recollection_config = BudgetRecollectionConfig(enabled=False)
    if config.orchestration_config is not None:
        budget_cfg = config.orchestration_config.complexity_budget
        budget_recollection_config = BudgetRecollectionConfig(
            enabled=budget_cfg.enabled,
            success_reward_ratio=budget_cfg.success_reward_ratio,
            failure_penalty_ratio=budget_cfg.failure_penalty_ratio,
        )

    # Extract coworker knowledge config (Design Choice 5)
    publish_worker_knowledge = True
    if config.orchestration_config is not None:
        publish_worker_knowledge = config.orchestration_config.coworker_knowledge.enabled

    parent_notifier = ParentNotificationService(
        repository=repository,
        progress_callback=config.progress_callback,
        budget_recollection_config=budget_recollection_config,
        shared_context_port=infrastructure.shared_context,  # Design Choice 5
        publish_worker_knowledge=publish_worker_knowledge,
    )

    # Group collaborators into dependencies object (Parameter Object pattern)
    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        workspace=workspace,
        shared_context_port=infrastructure.shared_context,
        sibling_view_port=sibling_view_builder,
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        dependencies=dependencies,
        config=service_config,
        system_limits=config.system_limits,
        progress_callback=config.progress_callback,
    )

    return Application(execution_service=execution_service)
