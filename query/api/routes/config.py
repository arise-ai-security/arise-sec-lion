"""System configuration API routes.

Provides a read-only endpoint for retrieving system configuration.
This allows the dashboard to display current hyperparameters.
"""

from fastapi import APIRouter, Request

from config import ApiSettings
from query.api.schemas import (
    ApplicationConfigSchema,
    InfrastructureConfigSchema,
    SystemConfigSchema,
)


router = APIRouter()


@router.get("", response_model=SystemConfigSchema)
async def get_system_config(request: Request) -> SystemConfigSchema:
    """Get current system configuration.

    Returns a read-only view of query/runtime configuration including:
    - Infrastructure settings (worker tool metadata)
    - Application settings (timeouts, retries, thresholds)

    Note: Sensitive information (passwords, API keys) is NOT exposed.
    Provider model names are intentionally absent from the base API config.
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, ApiSettings):
        settings = ApiSettings.load()

    return SystemConfigSchema(
        infrastructure=InfrastructureConfigSchema(
            llm_model_boss=None,
            worker_tool_type=settings.worker.tool,
            worker_tool_model=None,
            worker_tool_timeout=settings.worker.timeout,
        ),
        application=ApplicationConfigSchema(
            max_retries=settings.orchestration.max_retries,
            poll_interval=settings.orchestration.poll_interval,
        ),
    )
