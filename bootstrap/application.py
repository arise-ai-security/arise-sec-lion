"""Application Layer - Service Factories."""

from dataclasses import dataclass

from core.application.execution_service import AgentExecutionService, ProgressCallback

from .infrastructure import Infrastructure


@dataclass
class ApplicationConfig:
    """Configuration for application services."""

    max_retries: int = 3
    poll_interval: float = 0.5
    model_config: dict[str, str] | None = None
    output_directory: str | None = None
    progress_callback: ProgressCallback | None = None
    default_worker_tool: str = "claude_code"


@dataclass
class Application:
    """Container for all application services."""

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig | None = None,
) -> Application:
    """Create all application services."""
    if config is None:
        config = ApplicationConfig()

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        llm_port=infrastructure.llm_adapter,
        worker_tool_port=infrastructure.worker_tool,
        model_config=config.model_config,
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        progress_callback=config.progress_callback,
        default_worker_tool=config.default_worker_tool,
    )

    return Application(execution_service=execution_service)
