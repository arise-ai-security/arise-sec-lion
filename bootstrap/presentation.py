"""Presentation Layer - Factory Functions."""

from core.application.execution_service import AgentExecutionService
from presentation.cli import CLI, CLIConfig


def get_cli(
    execution_service: AgentExecutionService,
    config: CLIConfig | None = None,
) -> CLI:
    """Create CLI interface."""
    return CLI(execution_service=execution_service, config=config)
