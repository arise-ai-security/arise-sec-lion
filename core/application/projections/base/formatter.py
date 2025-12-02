"""Protocol for output formatters.

This module defines the Formatter protocol that all formatter implementations
must satisfy.
"""

from typing import Protocol, runtime_checkable

from core.application.projections.models import ProjectionSummary
from core.domain.events import DomainEvent


@runtime_checkable
class Formatter(Protocol):
    """Protocol for output formatters.

    Formatters convert domain events or summaries into string representations
    suitable for output (JSON, text, etc.).

    Example implementations:
        - JSONFormatter: JSON array format
        - JSONLinesFormatter: JSONL streaming format (one object per line)
        - TextFormatter: Human-readable text format
    """

    def format(self, events: list[DomainEvent]) -> str:
        """Format domain events into a string.

        Args:
            events: List of DomainEvent records to format.

        Returns:
            Formatted string representation.
        """
        ...

    def format_summary(self, summary: ProjectionSummary) -> str:
        """Format a summary into a string.

        Args:
            summary: ProjectionSummary to format.

        Returns:
            Formatted string representation.
        """
        ...
