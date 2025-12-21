"""Presentation layer - CLI and API interfaces."""

from presentation.cli import CLI, CLIConfig, CLIContext, format_event_progress
from presentation.context import EventStoreContext
from presentation.formatters import EventFormatter, ProgressDisplayFormatter
from presentation.persistence import RunPersistence
from presentation.rendering import OutputRenderer


__all__ = [
    # Core CLI
    "CLI",
    "CLIConfig",
    "CLIContext",
    # Event formatting (Strategy pattern)
    "EventFormatter",
    "ProgressDisplayFormatter",
    "format_event_progress",  # Backward compatibility
    # Persistence
    "RunPersistence",
    # Async context management
    "EventStoreContext",
    # Output rendering
    "OutputRenderer",
]
