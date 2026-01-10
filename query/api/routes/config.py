"""System configuration API routes.

Provides a read-only endpoint for retrieving system configuration.
This allows the dashboard to display current hyperparameters.
"""

import os

from fastapi import APIRouter

from config import Settings
from query.api.schemas import (
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

    # Detect unified model mode from env var (set by CLI --unified-model)
    unified_model = os.environ.get("ARISE_UNIFIED_MODEL")
    boss_model = unified_model if unified_model else settings.llm.model_boss
    worker_model = unified_model if unified_model else settings.worker.tool_model

    return SystemConfigSchema(
        infrastructure=InfrastructureConfigSchema(
            llm_model_boss=boss_model,
            worker_tool_type=settings.worker.tool_type,
            worker_tool_model=worker_model,
            worker_tool_timeout=settings.worker.tool_timeout,
            unified_model=unified_model,
        ),
        application=ApplicationConfigSchema(
            max_retries=settings.orchestration.max_retries,
            retry_delay=settings.orchestration.retry_delay,
            poll_interval=settings.orchestration.poll_interval,
            llm_timeout=settings.orchestration.llm_timeout,
            worker_timeout=settings.orchestration.worker_timeout,
            default_task_complexity_threshold=settings.orchestration.default_task_complexity_threshold,
        ),
    )
