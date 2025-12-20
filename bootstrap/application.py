"""Application Layer - Service Factories."""

from dataclasses import dataclass

from config import OrchestrationConfig
from core.application.execution_service import (
    AgentExecutionService,
    BudgetConfig,
    ProgressCallback,
    StatusCallback,
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
    budget_max_total_cost_usd: float
    budget_cost_warning_threshold: float
    budget_cost_tracking_enabled: bool
    worker_shortcut_probability: float = 0.0
    progress_callback: ProgressCallback | None = None
    status_callback: StatusCallback | None = None


@dataclass
class Application:
    """Container for all application services."""

    execution_service: AgentExecutionService


def get_application(
    infrastructure: Infrastructure,
    config: ApplicationConfig,
) -> Application:
    """Create all application services."""
    budget_config = BudgetConfig(
        max_total_cost_usd=config.budget_max_total_cost_usd,
        cost_warning_threshold=config.budget_cost_warning_threshold,
        cost_tracking_enabled=config.budget_cost_tracking_enabled,
    )

    execution_service = AgentExecutionService(
        event_store=infrastructure.event_store,
        llm_port=infrastructure.llm_adapter,
        worker_tool_port=infrastructure.worker_tool,
        system_limits=config.system_limits,
        model_config=config.model_config,
        max_retries=config.max_retries,
        poll_interval=config.poll_interval,
        output_directory=config.output_directory,
        progress_callback=config.progress_callback,
        status_callback=config.status_callback,
        default_worker_tool=config.default_worker_tool,
        budget_config=budget_config,
        worker_shortcut_probability=config.worker_shortcut_probability,
    )

    return Application(execution_service=execution_service)
