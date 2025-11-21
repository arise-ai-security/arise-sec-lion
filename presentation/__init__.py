"""Presentation Layer - User Interface Implementations.

This package contains all user-facing interface implementations (CLI, REST API,
GraphQL, etc.). The presentation layer depends on the application layer and
coordinates user interaction workflows.

In Hexagonal Architecture, these are "driving adapters" (primary adapters)
that drive the application, as opposed to infrastructure adapters which are
"driven adapters" (secondary adapters) driven by the application.

Architecture Note:
    This package contains ONLY implementations. Factory functions for creating
    and wiring these implementations are in bootstrap/ (Composition Root).
    This follows the principle that each layer should focus on one concern:
    - presentation/ = UI implementations
    - bootstrap/ = Dependency injection and wiring
"""

from .cli import CLI, CLIConfig


__all__ = [
    "CLI",
    "CLIConfig",
]
