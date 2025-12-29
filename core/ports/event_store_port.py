"""Event Store port: append-only event log with OCC.

Interfaces are segregated following ISP (Interface Segregation Principle):
- EventStoreConnectPort: Connection lifecycle management
- EventStoreWritePort: Append-only write operations with OCC
- EventStoreReadPort: Read-only query operations

EventStorePort is the composite interface for full implementations.
Read-only clients can depend only on EventStoreReadPort.
"""

from typing import Protocol
from uuid import UUID

from core.domain.events import DomainEvent


class EventStoreConnectPort(Protocol):
    """Connection lifecycle management for event store backends."""

    async def connect(self) -> None:
        """Connect to backend."""
        ...

    async def disconnect(self) -> None:
        """Close connection."""
        ...

    async def initialize_schema(self) -> None:
        """Create tables/schema."""
        ...


class EventStoreWritePort(Protocol):
    """Append-only write operations with Optimistic Concurrency Control."""

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append event with OCC. Raises ConcurrencyError if version mismatch."""
        ...

    async def append_batch(self, events: list[DomainEvent], expected_version: int) -> None:
        """Append multiple events atomically in a single transaction.

        Significantly faster than individual appends for workers
        that produce many events (thoughts, tool uses, etc.).

        Args:
            events: List of events to persist.
            expected_version: Expected version before first event.
        """
        ...


class EventStoreReadPort(Protocol):
    """Read-only query operations for event store.

    Use this interface for read-only clients (projections, queries, API endpoints)
    that don't need write access to the event store.
    """

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        """Get events for aggregate, ordered by sequence_number.

        Args:
            aggregate_id: The aggregate to fetch events for.
            limit: Maximum number of events to return (None = unlimited).
            after_sequence: Only return events with sequence_number > this value.

        Returns:
            List of events ordered by sequence_number.
        """
        ...

    async def get_all_aggregate_ids(self) -> list[UUID]:
        """Get all aggregate UUIDs that have events."""
        ...

    async def get_all_events_grouped(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get all events grouped by aggregate_id in a single query.

        Args:
            limit: Maximum number of aggregates to return (None = unlimited).
            offset: Number of aggregates to skip (for pagination).

        Returns:
            Dict mapping aggregate_id to list of events ordered by sequence_number.
        """
        ...

    async def get_boss_agents_grouped(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for BOSS agents only, using optimized single-query approach.

        Filters at database level for role='boss' in AgentCreated events,
        then fetches all events for those aggregates in a single subquery.
        Much faster than get_all_events_grouped + Python filter.

        Args:
            limit: Maximum number of BOSS agents to return (None = unlimited).
            offset: Number of BOSS agents to skip (for pagination).

        Returns:
            Dict mapping aggregate_id to list of events for BOSS agents only.
        """
        ...


class EventStorePort(EventStoreConnectPort, EventStoreWritePort, EventStoreReadPort, Protocol):
    """Composite event store interface with full capabilities.

    Combines:
    - EventStoreConnectPort: Connection lifecycle (connect, disconnect, initialize_schema)
    - EventStoreWritePort: Append-only writes with OCC
    - EventStoreReadPort: Read-only queries

    Implementations should inherit from this composite interface.
    Clients should depend on the narrowest interface they need.
    """

    pass
