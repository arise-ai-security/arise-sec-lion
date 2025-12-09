"""System configuration API routes.

Provides a read-only endpoint for retrieving system configuration.
This allows the dashboard to display current hyperparameters.
"""

from fastapi import APIRouter

from config import Settings
from presentation.api.schemas import (
    ApplicationConfigSchema,
    InfrastructureConfigSchema,
    SystemConfigSchema,
)


router = APIRouter()


@router.get("", response_model=SystemConfigSchema)
async def get_system_config() -> SystemConfigSchema:
    """Get current system configuration.

    Returns a read-only view of the system configuration including:
    - Infrastructure settings (LLM models, worker tools)
    - Application settings (timeouts, retries, thresholds)

    Note: Sensitive information (passwords, API keys) is NOT exposed.
    """
    settings = Settings.load()

    return SystemConfigSchema(
        infrastructure=InfrastructureConfigSchema(
            llm_model_boss=settings.infrastructure.llm_model_boss,
            worker_tool_type=settings.infrastructure.worker_tool_type,
            worker_tool_model=settings.infrastructure.worker_tool_model,
            worker_tool_timeout=settings.infrastructure.worker_tool_timeout,
        ),
        application=ApplicationConfigSchema(
            max_retries=settings.application.max_retries,
            retry_delay=settings.application.retry_delay,
            poll_interval=settings.application.poll_interval,
            llm_timeout=settings.application.llm_timeout,
            worker_timeout=settings.application.worker_timeout,
            default_task_complexity_threshold=settings.application.default_task_complexity_threshold,
        ),
    )
