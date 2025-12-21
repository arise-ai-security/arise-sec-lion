"""Bootstrap layer - Composition Root for dependency injection.

All entry points should depend only on this module, not on infrastructure or presentation.
This follows the Composition Root pattern (Mark Seemann - "Dependency Injection in .NET").
"""

from .application import Application, ApplicationConfig, get_application
from .bootstrap import bootstrap
from .infrastructure import Infrastructure, InfrastructureConfig, get_infrastructure
from .presentation import get_cli, get_click_group


__all__ = [
    "Application",
    "ApplicationConfig",
    "Infrastructure",
    "InfrastructureConfig",
    "bootstrap",
    "get_application",
    "get_cli",
    "get_click_group",
    "get_infrastructure",
]
