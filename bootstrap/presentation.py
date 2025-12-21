"""Presentation Layer - Factory Functions.

This module provides factory functions for the presentation layer.
The Click command group is exported here to keep main.py depending only on bootstrap.
"""

import click

from core.application.execution_service import AgentExecutionService
from presentation.cli import CLI, CLIConfig
from presentation.cli import cli as click_group


def get_cli(
    execution_service: AgentExecutionService,
    config: CLIConfig | None = None,
) -> CLI:
    """Create CLI interface."""
    return CLI(execution_service=execution_service, config=config)


def get_click_group() -> click.Group:
    """Return the Click command group.

    This allows main.py to depend only on bootstrap (Composition Root pattern).
    The Click commands internally call bootstrap() for dependency injection.
    """
    return click_group
