"""Real-time callback port: stream events to external consumers."""

from typing import Protocol
from uuid import UUID

from core.domain.events.events import DomainEvent


class RealtimeCallbackPort(Protocol):
    """Port for real-time event streaming during worker execution.

    Implementations push events to external consumers (e.g., SSE endpoints)
    without waiting for database persistence.
    """

    async def on_event(self, event: DomainEvent, root_id: UUID) -> None:
        """Called when a new event is generated during execution.

        Args:
            event: Domain event (typically ThoughtCaptured)
            root_id: Root agent ID for routing to correct subscribers
        """
        ...
