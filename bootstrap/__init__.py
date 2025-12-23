"""Bootstrap layer - Composition Root for dependency injection.

All entry points should depend only on this module, not on infrastructure or presentation.
This follows the Composition Root pattern (Mark Seemann - "Dependency Injection in .NET").
"""

from .application import Application, ApplicationConfig, get_application
from .bootstrap import bootstrap, create_cli_app
from .infrastructure import (
    Infrastructure,
    InfrastructureConfig,
    get_infrastructure,
)
from .presentation import get_cli


__all__ = [
    "Application",
    "ApplicationConfig",
    "Infrastructure",
    "InfrastructureConfig",
    "bootstrap",
    "create_cli_app",
    "get_application",
    "get_cli",
    "get_infrastructure",
]
