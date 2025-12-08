"""PostgreSQL implementation of the Event Store.

This adapter provides persistent storage for domain events using PostgreSQL
with asyncpg. It implements the EventStorePort protocol from core.ports.
"""

from pathlib import Path
from uuid import UUID

import asyncpg
import orjson

from core.domain.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import ConcurrencyError, EventStoreError
from core.ports.event_store_port import EventStorePort


# Event type registry for deserialization
# Maps event_type string to the corresponding Pydantic event class
EVENT_TYPE_REGISTRY: dict[str, type[DomainEvent]] = {
    "AgentCreated": AgentCreated,
    "TaskAssigned": TaskAssigned,
    "StatusChanged": StatusChanged,
    "SubtasksDefined": SubtasksDefined,
    "ChildSpawned": ChildSpawned,
    "WorkCompleted": WorkCompleted,
    "WorkFailed": WorkFailed,
    "CodeGenerationStarted": CodeGenerationStarted,
    "ThoughtCaptured": ThoughtCaptured,
    "ChildCompleted": ChildCompleted,
    "ComplexityEvaluated": ComplexityEvaluated,
}


class PostgresEventStore(EventStorePort):
    """PostgreSQL-based event store with Optimistic Concurrency Control.

    This adapter stores domain events in an append-only PostgreSQL table,
    ensuring atomicity and version control for concurrent writes.

    Attributes:
        connection_string: PostgreSQL connection string (e.g., "postgresql://user:pass@host/db").
        pool: Connection pool for efficient database access.
    """

    # Path to SQL scripts directory
    SQL_DIR = Path(__file__).parent.parent / "sql"

    def __init__(self, connection_string: str) -> None:
        """Initialize the PostgreSQL event store.

        Args:
            connection_string: PostgreSQL connection URI.
        """
        self.connection_string = connection_string
        self.pool: asyncpg.Pool | None = None

    @classmethod
    def _load_sql(cls, filename: str) -> str:
        """Load SQL script from file.

        Args:
            filename: Name of the SQL file in the sql directory.

        Returns:
            SQL script content as string.

        Raises:
            FileNotFoundError: If SQL file doesn't exist.
        """
        sql_path = cls.SQL_DIR / filename
        return sql_path.read_text(encoding="utf-8")

    async def connect(self) -> None:
        """Establish connection pool to PostgreSQL.

        Should be called during application startup.

        Raises:
            ConnectionError: If unable to connect to the database.
        """

        async def init_connection(conn: asyncpg.Connection) -> None:
            """Initialize connection with custom JSON codec using orjson."""
            # Register orjson for JSONB encoding/decoding
            # This ensures dicts are properly serialized to JSONB without double-encoding
            await conn.set_type_codec(
                "jsonb",
                encoder=lambda v: orjson.dumps(v).decode("utf-8"),
                decoder=orjson.loads,
                schema="pg_catalog",
            )

        self.pool = await asyncpg.create_pool(
            self.connection_string,
            init=init_connection,
        )

    async def disconnect(self) -> None:
        """Close the connection pool.

        Should be called during application shutdown.
        """
        if self.pool:
            await self.pool.close()

    async def initialize_schema(self) -> None:
        """Create the events table if it doesn't exist.

        This method should be called during application startup to ensure
        the database schema is properly initialized.

        Schema:
            - event_id: UUID primary key
            - aggregate_id: UUID (indexed for fast queries)
            - sequence_number: INT (for ordering)
            - event_type: VARCHAR (for deserialization)
            - payload: JSONB (event data)
            - occurred_at: TIMESTAMP (for audit trail)
            - UNIQUE(aggregate_id, sequence_number) for OCC

        Raises:
            EventStoreError: If schema creation fails.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            # Load SQL schema from file
            create_table_sql = self._load_sql("create_events_table.sql")

            async with self.pool.acquire() as conn:
                await conn.execute(create_table_sql)
        except Exception as e:
            raise EventStoreError(
                "Failed to initialize event store schema", original_error=e
            ) from e

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append a domain event with Optimistic Concurrency Control.

        This method ensures that events are only appended if the aggregate's
        current version matches the expected_version. This prevents lost updates
        in concurrent scenarios.

        OCC Implementation Strategy:
            - Use UNIQUE(aggregate_id, sequence_number) constraint
            - Try to INSERT with sequence_number = expected_version + 1
            - If UniqueViolationError → ConcurrencyError
            - This leverages database atomicity for correctness

        Args:
            event: The domain event to append.
            expected_version: The expected current version of the aggregate.

        Raises:
            ConcurrencyError: When expected_version doesn't match actual version.
            EventStoreError: On database failures.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            # Convert Pydantic model to dict for JSONB storage
            # The custom orjson codec registered in connect() handles serialization
            event_dict = event.model_dump(mode="json")

            # Extract event type for deserialization
            event_type = event.__class__.__name__

            # Attempt to insert with specific sequence_number
            # If another process already inserted this sequence_number,
            # the UNIQUE constraint will raise UniqueViolationError
            async with self.pool.acquire() as conn:
                # Pass dicts directly - the orjson codec handles serialization
                await conn.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        aggregate_id,
                        sequence_number,
                        event_type,
                        payload,
                        occurred_at,
                        metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    event.event_id,
                    event.aggregate_id,
                    event.sequence_number,
                    event_type,
                    event_dict,  # Dict - codec handles serialization
                    event.occurred_at,
                    event.metadata,  # Dict - codec handles serialization
                )

        except asyncpg.UniqueViolationError as e:
            # Concurrency conflict detected by database constraint
            # Query actual version to provide helpful error message
            try:
                async with self.pool.acquire() as conn:
                    actual_version = await conn.fetchval(
                        """
                        SELECT COALESCE(MAX(sequence_number), 0)
                        FROM events
                        WHERE aggregate_id = $1
                        """,
                        event.aggregate_id,
                    )
            except Exception:
                actual_version = -1  # Unknown

            raise ConcurrencyError(
                aggregate_id=str(event.aggregate_id),
                expected_version=expected_version,
                actual_version=actual_version,
            ) from e

        except Exception as e:
            raise EventStoreError(
                f"Failed to append event for aggregate {event.aggregate_id}",
                original_error=e,
            ) from e

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        """Retrieve all events for an aggregate, ordered by sequence number.

        This method queries the database for all events belonging to the
        specified aggregate and deserializes them back into their original
        Pydantic event class instances.

        Args:
            aggregate_id: The UUID of the aggregate (AgentSession).

        Returns:
            List of domain events in sequence order.
            Returns empty list if aggregate doesn't exist.

        Raises:
            EventStoreError: On database or deserialization failures.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT event_type, payload
                    FROM events
                    WHERE aggregate_id = $1
                    ORDER BY sequence_number ASC
                    """,
                    aggregate_id,
                )

            # Deserialize events from database rows
            events: list[DomainEvent] = []
            for row in rows:
                event_type = row["event_type"]
                payload_json = row["payload"]

                # Look up event class from registry
                event_class = EVENT_TYPE_REGISTRY.get(event_type)
                if not event_class:
                    raise EventStoreError(
                        f"Unknown event type '{event_type}' for aggregate {aggregate_id}. "
                        f"Event type not registered in EVENT_TYPE_REGISTRY."
                    )

                # Deserialize JSON to Pydantic model
                try:
                    # Handle both dict (correct) and string (double-encoded legacy data)
                    if isinstance(payload_json, str):
                        # Double-encoded: JSONB stored a JSON string, parse it
                        payload_dict = orjson.loads(payload_json)
                    else:
                        # Normal: JSONB returned a dict directly
                        payload_dict = payload_json

                    event = event_class(**payload_dict)
                    events.append(event)
                except Exception as e:
                    raise EventStoreError(
                        f"Failed to deserialize event {event_type} for aggregate {aggregate_id}",
                        original_error=e,
                    ) from e

            return events

        except EventStoreError:
            # Re-raise our own errors
            raise

        except Exception as e:
            raise EventStoreError(
                f"Failed to retrieve events for aggregate {aggregate_id}",
                original_error=e,
            ) from e

    async def get_all_aggregate_ids(self) -> list[UUID]:
        """Retrieve all unique aggregate IDs from the event store.

        This method queries the database for all distinct aggregate_ids
        that have events. Used by the orchestration layer to discover
        all agents in the system.

        Returns:
            List of UUIDs for all aggregates with events.
            Returns empty list if no aggregates exist.

        Raises:
            EventStoreError: On database failures.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT aggregate_id
                    FROM events
                    ORDER BY aggregate_id
                    """
                )

            return [row["aggregate_id"] for row in rows]

        except Exception as e:
            raise EventStoreError(
                "Failed to retrieve all aggregate IDs",
                original_error=e,
            ) from e
