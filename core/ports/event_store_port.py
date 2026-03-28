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

from core.domain.events.events import DomainEvent


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

    async def get_hierarchy_events_grouped(
        self,
        root_id: UUID,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for an agent hierarchy using recursive CTE.

        Efficiently fetches only events belonging to the specified hierarchy
        (root agent and all descendants via ChildSpawned events).
        Much faster than get_all_events_grouped + Python filter.

        Args:
            root_id: Root agent UUID to start hierarchy traversal.

        Returns:
            Dict mapping aggregate_id to list of events for hierarchy only.
        """
        ...

    async def get_children_events_grouped(
        self,
        parent_id: UUID,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for all children of a parent agent.

        Filters at database level for agents with matching parent_id
        in their AgentCreated event payload.
        Much faster than get_all_events_grouped + Python filter.

        Args:
            parent_id: Parent agent UUID to find children for.

        Returns:
            Dict mapping aggregate_id to list of events for children only.
        """
        ...

    async def get_boss_agent_summaries(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get BOSS agent summaries directly from SQL (no event processing).

        Returns pre-computed summary data directly from database,
        avoiding event fetching, deserialization, and Python projection.

        Performance: ~0.3ms vs ~22ms for event-based approach.

        Args:
            limit: Maximum number of BOSS agents to return.
            offset: Number of agents to skip for pagination.

        Returns:
            List of dicts with keys: agent_id, role, status, task_description, created_at,
            domain_metadata
        """
        ...

    async def get_events_batch_incremental(
        self,
        agent_sequences: dict[UUID, int | None],
    ) -> dict[UUID, list[DomainEvent]]:
        """Get new events for multiple agents in a single query.

        Optimized for SSE streaming: fetches events for N agents where each
        agent has a different after_sequence threshold. Reduces N queries to 1.

        Args:
            agent_sequences: Dict mapping agent_id to last seen sequence number.
                             If sequence is None, fetch all events for that agent.

        Returns:
            Dict mapping aggregate_id to list of new events ordered by sequence_number.
            Only includes agents that have new events.
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

