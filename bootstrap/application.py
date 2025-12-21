"""Application Layer - Service Factories."""

from dataclasses import dataclass

from config import OrchestrationConfig
from core.application.execution_service import (
    AgentExecutionService,
    ProgressCallback,
    ServiceConfig,
)

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
    """Create all application services."""
    service_config = ServiceConfig(
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        default_worker_tool=config.default_worker_tool,
        model_config=config.model_config,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        llm_port=infrastructure.llm_adapter,
        worker_tool_port=infrastructure.worker_tool,
        system_limits=config.system_limits,
        config=service_config,
        progress_callback=config.progress_callback,
    )

    return Application(execution_service=execution_service)
