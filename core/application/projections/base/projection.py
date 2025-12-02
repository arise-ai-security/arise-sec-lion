"""Abstract base class for event projections.

This module defines the Projection ABC that all projection implementations
must inherit from, establishing a clear contract and relationship between
EventLogProjection and SummaryProjection.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from core.domain.events import DomainEvent


class Projection(ABC):
    """Abstract base class for event-to-output transformations.

    Projections transform a sequence of domain events into structured output.
    Subclasses define their specific return types (covariant).

    Implementations:
        - EventLogProjection: Returns list[LogEntry]
        - SummaryProjection: Returns ProjectionSummary

    Example:
        class MyProjection(Projection):
            def project(self, events: Iterable[DomainEvent]) -> MyOutput:
                ...
    """

    @abstractmethod
    def project(self, events: Iterable[DomainEvent]) -> Any:
        """Transform events into structured output.

        Args:
            events: Iterable of domain events to transform.

        Returns:
            Structured output (type depends on implementation).

        Raises:
            TypeError: If an event type has no registered handler (fail-fast).
        """
        ...
