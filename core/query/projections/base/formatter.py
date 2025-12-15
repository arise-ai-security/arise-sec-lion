"""Protocol for output formatters."""

from typing import Protocol, runtime_checkable

from core.domain.events import DomainEvent
from core.query.projections.models import ProjectionSummary


@runtime_checkable
class Formatter(Protocol):
    """Convert events or summaries into string output (JSON, text, etc.)."""

    def format(self, events: list[DomainEvent]) -> str:
        """Format events into string."""
        ...

    def format_summary(self, summary: ProjectionSummary) -> str:
        """Format summary into string."""
        ...
