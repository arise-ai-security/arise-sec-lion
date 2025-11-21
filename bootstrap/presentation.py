"""Presentation Layer - Factory Functions.

This module provides factory functions for creating presentation layer
components (CLI, REST API, etc.) with their dependencies properly wired.

Dependency: Presentation → Application (services, NOT bootstrap!)

CRITICAL: Presentation must NOT depend on Bootstrap layer!
- Presentation depends on core.application.execution_service (CORRECT)
- Presentation does NOT depend on bootstrap.* (that would be backwards!)

This ensures proper layering:
  Domain ← Application ← Infrastructure
             ↑
         Presentation
             ↑
         Bootstrap (outermost - wires everything)
"""

from core.application.execution_service import AgentExecutionService
from presentation.cli import CLI, CLIConfig


def get_cli(
    execution_service: AgentExecutionService,
    config: CLIConfig | None = None,
) -> CLI:
    """Create and return the CLI interface.

    This factory function wires the CLI with its application dependencies.
    As a factory in the bootstrap layer, it knows about both the CLI
    implementation and how to compose it with application services.

    Args:
        execution_service: Application service for agent execution.
        config: CLI configuration. Uses defaults if not provided.

    Returns:
        CLI instance ready to run.

    Architecture Note:
        This factory lives in bootstrap/ (Composition Root) rather than
        presentation/ because:
        1. Bootstrap is the single place where all composition happens
        2. Presentation layer should only contain implementations, not wiring
        3. This follows the Composition Root pattern from DI principles

        The CLI depends on AgentExecutionService (Application layer), NOT on
        Bootstrap containers. This ensures correct dependency direction.
    """
    return CLI(execution_service=execution_service, config=config)
