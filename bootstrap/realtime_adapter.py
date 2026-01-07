"""Adapter for real-time event streaming.

Implements RealtimeCallbackPort by delegating to EventBroadcaster.
"""

from uuid import UUID

from core.application.services.event_broadcaster import EventBroadcaster
from core.domain.events.events import DomainEvent


class RealtimeCallbackAdapter:
    """Adapter that bridges RealtimeCallbackPort to EventBroadcaster.

    This adapter implements the port interface and delegates to the
    in-memory EventBroadcaster for real-time event distribution.
    """

    def __init__(self, broadcaster: EventBroadcaster) -> None:
        """Initialize with broadcaster instance.

        Args:
            broadcaster: EventBroadcaster for publishing events
        """
        self._broadcaster = broadcaster

    async def on_event(self, event: DomainEvent, root_id: UUID) -> None:
        """Publish event to broadcaster for real-time streaming.

        Args:
            event: Domain event to broadcast
            root_id: Root agent ID for routing
        """
        await self._broadcaster.publish(event, root_id)
