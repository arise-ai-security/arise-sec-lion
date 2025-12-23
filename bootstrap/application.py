"""Application Layer - Service Factories."""

from dataclasses import dataclass

from config import OrchestrationConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ProgressCallback,
    ServiceConfig,
)
from core.application.services.agent_repository import AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.context_registry import ExecutionContextRegistry
from core.application.services.query_service import AgentQueryService
from core.application.services.workspace_context import WorkspaceContextProvider
from core.domain.prompt_builder import PromptBuilder

from .infrastructure import Infrastructure


@dataclass
class ApplicationConfig:
    """Configuration for application services."""

    system_limits: OrchestrationConfig.LimitsConfig
    max_retries: int
    poll_interval: float
    model_config: dict[str, str]
    output_directory: str
    default_worker_tool: str
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
        model_config=config.model_config,
    )

    # Create collaborators (composition root wiring)
    prompt_builder = PromptBuilder(default_tool=config.default_worker_tool)

    repository = AgentRepository(
        event_store=infrastructure.event_store,
        max_retries=config.max_retries,
        progress_callback=config.progress_callback,
    )
    context_registry = ExecutionContextRegistry()
    workspace = WorkspaceContextProvider()
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        context_registry=context_registry,
        max_total_agents=config.system_limits.max_total_agents,
    )
    orchestrator = AgentOrchestrator(
        llm_port=infrastructure.llm_adapter,
        worker_port=infrastructure.worker_tool,
        prompt_builder=prompt_builder,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        system_limits=config.system_limits,
        config=service_config,
        repository=repository,
        orchestrator=orchestrator,
        context_registry=context_registry,
        child_factory=child_factory,
        query_service=query_service,
        workspace=workspace,
        shared_context_port=infrastructure.shared_context,
        progress_callback=config.progress_callback,
    )

    return Application(execution_service=execution_service)
