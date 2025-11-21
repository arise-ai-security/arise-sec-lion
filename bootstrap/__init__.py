"""Bootstrap layer for dependency injection and application wiring.

This package is the Composition Root - the single place where all layers
are wired together through dependency injection.

It contains factory functions and configuration objects for:
1. Infrastructure Layer - Adapters that implement domain ports
2. Application Layer - Services that orchestrate domain logic
3. Presentation Layer - User interfaces (CLI, REST API, etc.)

Architecture Note:
    This follows the Composition Root pattern where ALL dependency injection
    and wiring happens in one place (bootstrap/), while implementation layers
    (core/, infrastructure/, presentation/) contain ONLY business logic.

    Reference: "Dependency Injection in .NET" by Mark Seemann

Main Entry Point:
    Use the bootstrap() function to wire the entire application:
    >>> from bootstrap import bootstrap
    >>> cli = bootstrap()
    >>> await cli.run()
"""

# Re-export all public types for convenient importing
from .application import Application, ApplicationConfig, get_application
from .bootstrap import bootstrap
from .infrastructure import Infrastructure, InfrastructureConfig, get_infrastructure
from .presentation import get_cli


__all__ = [
    "Application",
    "ApplicationConfig",
    "Infrastructure",
    "InfrastructureConfig",
    "bootstrap",
    "get_application",
    "get_cli",
    "get_infrastructure",
]
