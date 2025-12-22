"""Event Store port: append-only event log with OCC."""

from typing import Protocol
from uuid import UUID

from core.domain.events import DomainEvent


class EventStorePort(Protocol):
    """Persist events with Optimistic Concurrency Control. Append-only, ordered by sequence."""

    async def connect(self) -> None:
        """Connect to backend."""
        ...

    async def disconnect(self) -> None:
        """Close connection."""
        ...

    async def initialize_schema(self) -> None:
        """Create tables/schema."""
        ...

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append event with OCC. Raises ConcurrencyError if version mismatch."""
        ...

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
