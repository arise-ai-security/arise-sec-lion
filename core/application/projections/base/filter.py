"""Protocol for event filtering strategies.

This module defines the EventFilter protocol that all filter implementations
must satisfy.
"""

from typing import Protocol, runtime_checkable

from core.domain.events import DomainEvent


@runtime_checkable
class EventFilter(Protocol):
    """Protocol for event filtering strategies.

    Filters determine which events pass through the projection pipeline.
    Implementations should be stateless and side-effect free.

    Example implementations:
        - IncludeAllFilter: Passes all events
        - ErrorOnlyFilter: Only passes WorkFailed events
        - AgentFilter: Only passes events from specific agents
    """

    def matches(self, event: DomainEvent) -> bool:
        """Determine if an event should be included in the projection.

        Args:
            event: The domain event to evaluate.

        Returns:
            True if the event should pass through, False to filter it out.
        """
        ...
