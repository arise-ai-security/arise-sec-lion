"""Application Layer - Service Factories.

This module provides factory functions for creating application services
that orchestrate domain logic using infrastructure adapters.

Dependency: Application depends on Infrastructure (via Port interfaces)
"""

from dataclasses import dataclass

from core.application.execution_service import AgentExecutionService, ProgressCallback

from .infrastructure import Infrastructure


@dataclass
class ApplicationConfig:
    """Configuration for application services."""

    # Optimistic Concurrency Control retry settings
    max_retries: int = 3

    # System orchestration loop polling interval (seconds)
    poll_interval: float = 0.5

    # Role-based LLM model mapping
    model_config: dict[str, str] | None = None

    # Working directory for worker tools (code generation output)
    working_directory: str | None = None

    # Progress callback for real-time event notifications
    # Signature: (event: DomainEvent, agent: AgentSession) -> None
    progress_callback: ProgressCallback | None = None

    # Default worker tool to use in subtask prompt examples
    # This guides the LLM to specify this tool in subtask configs
    default_worker_tool: str = "claude_code"


@dataclass
class Application:
    """Container for all application services.

    Currently contains only AgentExecutionService, but this structure
    allows for easy addition of other application services (e.g.,
    QueryService, ReportingService, etc.)
    """

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig | None = None,
) -> Application:
    """Create and return all application services.

    This factory function wires infrastructure adapters into application
    services. The services use ports (interfaces) to interact with
    infrastructure, maintaining the dependency inversion principle.

    Args:
        infrastructure: Infrastructure adapters (event store, LLM, worker tools).
        config: Application configuration. Uses defaults if not provided.

    Returns:
        Application container with all services.

    Architecture Note:
        Application services orchestrate domain logic and depend on infrastructure
        via port interfaces. This is the "use case" layer that implements business
        workflows (e.g., executing agent steps, managing child agents, etc.).

        The application layer should NOT contain domain logic (that's in aggregates)
        or infrastructure details (that's in adapters). It only coordinates.
    """
    if config is None:
        config = ApplicationConfig()

    # Agent Execution Service - orchestrates multi-agent workflow
    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        llm_port=infrastructure.llm_adapter,
        worker_tool_port=infrastructure.worker_tool,
        model_config=config.model_config,
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        working_directory=config.working_directory,
        progress_callback=config.progress_callback,
        default_worker_tool=config.default_worker_tool,
    )

    return Application(execution_service=execution_service)
