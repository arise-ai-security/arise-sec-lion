"""Protocol for event filtering."""

from typing import Protocol, runtime_checkable

from core.domain.events.events import DomainEvent


@runtime_checkable
class EventFilter(Protocol):
    """Protocol for stateless event filters in the projection pipeline."""

    def matches(self, event: DomainEvent) -> bool:
        """Return True if event should pass through, False to filter out."""
        ...
