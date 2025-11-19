"""PostgreSQL implementation of the Event Store.

This adapter provides persistent storage for domain events using PostgreSQL
with asyncpg. It implements the EventStorePort protocol from core.ports.
"""

from uuid import UUID

import asyncpg

from core.domain.events import DomainEvent
from core.ports.event_store_port import EventStorePort


class PostgresEventStore(EventStorePort):
    """PostgreSQL-based event store with Optimistic Concurrency Control.

    This adapter stores domain events in an append-only PostgreSQL table,
    ensuring atomicity and version control for concurrent writes.

    Attributes:
        connection_string: PostgreSQL connection string (e.g., "postgresql://user:pass@host/db").
        pool: Connection pool for efficient database access.
    """

    def __init__(self, connection_string: str) -> None:
        """Initialize the PostgreSQL event store.

        Args:
            connection_string: PostgreSQL connection URI.
        """
        self.connection_string = connection_string
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Establish connection pool to PostgreSQL.

        Should be called during application startup.

        Raises:
            ConnectionError: If unable to connect to the database.
        """
        # TODO: Implement connection pool initialization
        self.pool = await asyncpg.create_pool(self.connection_string)

    async def disconnect(self) -> None:
        """Close the connection pool.

        Should be called during application shutdown.
        """
        if self.pool:
            await self.pool.close()

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append a domain event with Optimistic Concurrency Control.

        This method ensures that events are only appended if the aggregate's
        current version matches the expected_version. This prevents lost updates
        in concurrent scenarios.

        Args:
            event: The domain event to append.
            expected_version: The expected current version of the aggregate.

        Raises:
            ConcurrencyError: When expected_version doesn't match actual version.
            EventStoreError: On database failures.
        """
        # TODO: Implement Optimistic Concurrency Control (OCC)
        # TODO: 1. Begin transaction
        # TODO: 2. Query current max version for aggregate_id
        # TODO: 3. Verify current_version == expected_version
        # TODO: 4. If match: INSERT event, COMMIT
        # TODO: 5. If mismatch: ROLLBACK, raise ConcurrencyError
        # TODO: 6. Handle serialization of event to JSON/JSONB
        raise NotImplementedError("OCC append not yet implemented")

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        """Retrieve all events for an aggregate, ordered by sequence number.

        Args:
            aggregate_id: The UUID of the aggregate (AgentSession).

        Returns:
            List of domain events in sequence order.

        Raises:
            EventStoreError: On database failures.
        """
        # TODO: Implement event retrieval
        # TODO: 1. Query events table WHERE aggregate_id = $1 ORDER BY sequence_number
        # TODO: 2. Deserialize JSON/JSONB to DomainEvent subclasses
        # TODO: 3. Return list of events
        raise NotImplementedError("Event retrieval not yet implemented")
