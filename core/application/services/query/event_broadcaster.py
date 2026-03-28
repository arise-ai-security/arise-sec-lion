"""In-memory event broadcaster for real-time event streaming.

Enables real-time streaming of ThoughtCaptured events to SSE endpoints
without waiting for database persistence.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID


if TYPE_CHECKING:
    from core.domain.events.events import DomainEvent

logger = logging.getLogger(__name__)


class EventBroadcaster:
    """In-memory pub/sub for real-time event streaming.

    Manages per-root_id subscription queues for SSE connections.
    Events are published during worker execution and delivered
    to all active subscribers immediately.

    Thread-safe through asyncio primitives.
    """

    _instance: "EventBroadcaster | None" = None
    _lock: asyncio.Lock | None = None

    def __init__(self) -> None:
        """Initialize broadcaster with empty subscriber registry."""
        self._subscribers: dict[UUID, list[asyncio.Queue[DomainEvent]]] = (
            defaultdict(list)
        )
        self._registry_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "EventBroadcaster":
        """Get or create singleton instance.

        Note: Should be called from async context for proper lock initialization.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton for testing."""
        cls._instance = None

    async def publish(self, event: "DomainEvent", root_id: UUID) -> None:
        """Publish event to all subscribers for a root_id.

        Non-blocking - drops events if subscriber queue is full
        to prevent backpressure from affecting worker execution.

        Args:
            event: Domain event to broadcast (typically ThoughtCaptured)
            root_id: Root agent ID for subscription matching
        """
        async with self._registry_lock:
            subscribers = self._subscribers.get(root_id, [])
            for queue in subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Event queue full for root_id=%s, dropping event",
                        root_id,
                    )

    @asynccontextmanager
    async def subscribe(
        self, root_id: UUID, maxsize: int = 1000
    ) -> AsyncIterator[asyncio.Queue["DomainEvent"]]:
        """Subscribe to events for a root_id.

        Creates a bounded queue for receiving events. Queue is automatically
        unregistered when context exits.

        Args:
            root_id: Root agent ID to subscribe to
            maxsize: Maximum queue size for backpressure (default 1000)

        Yields:
            asyncio.Queue for receiving events
        """
        queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=maxsize)

        async with self._registry_lock:
            self._subscribers[root_id].append(queue)
            logger.debug("Subscriber added for root_id=%s", root_id)

        try:
            yield queue
        finally:
            async with self._registry_lock:
                try:
                    self._subscribers[root_id].remove(queue)
                    if not self._subscribers[root_id]:
                        del self._subscribers[root_id]
                    logger.debug("Subscriber removed for root_id=%s", root_id)
                except (ValueError, KeyError):
                    pass  # Already removed
