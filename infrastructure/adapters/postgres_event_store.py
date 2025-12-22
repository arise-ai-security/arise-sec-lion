"""PostgreSQL event store with asyncpg and OCC."""

from pathlib import Path
from uuid import UUID

import asyncpg
import orjson

from core.domain.events import (
    AgentCreated,
    ArtifactStored,
    BudgetConsumed,
    BudgetExceeded,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    ConfigOverrideSet,
    DecisionRecorded,
    DomainEvent,
    LimitEnforced,
    ProgressUpdated,
    SharedContextCreated,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.exceptions import ConcurrencyError, EventStoreError
from core.ports.event_store_port import EventStorePort


EVENT_TYPE_REGISTRY: dict[str, type[DomainEvent]] = {
    # Agent lifecycle events
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
    # Cost tracking events
    "TokensConsumed": TokensConsumed,
    "WorkerCostRecorded": WorkerCostRecorded,
    # Limit enforcement events
    "LimitEnforced": LimitEnforced,
    # Shared context events
    "SharedContextCreated": SharedContextCreated,
    "ArtifactStored": ArtifactStored,
    "DecisionRecorded": DecisionRecorded,
    "ProgressUpdated": ProgressUpdated,
    "ConfigOverrideSet": ConfigOverrideSet,
    # Budget events (part of shared context)
    "BudgetConsumed": BudgetConsumed,
    "BudgetExceeded": BudgetExceeded,
}


class PostgresEventStore(EventStorePort):
    """PostgreSQL event store with append-only log and OCC via UNIQUE constraint."""

    SQL_DIR = Path(__file__).parent.parent / "sql"

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.pool: asyncpg.Pool | None = None

    @classmethod
    def _load_sql(cls, filename: str) -> str:
        sql_path = cls.SQL_DIR / filename
        return sql_path.read_text(encoding="utf-8")

    @staticmethod
    def _deserialize_event(event_type: str, payload: str | dict) -> DomainEvent:
        """Deserialize event from stored type and payload."""
        event_class = EVENT_TYPE_REGISTRY.get(event_type)
        if not event_class:
            raise EventStoreError(f"Unknown event type: {event_type}")

        payload_dict = orjson.loads(payload) if isinstance(payload, str) else payload
        return event_class(**payload_dict)

    async def connect(self) -> None:
        async def init_connection(conn: asyncpg.Connection) -> None:
            await conn.set_type_codec(
                "jsonb",
                encoder=lambda v: orjson.dumps(v).decode("utf-8"),
                decoder=orjson.loads,
                schema="pg_catalog",
            )

        # Use statement_cache_size=0 for compatibility with pgbouncer/Supabase pooler
        self.pool = await asyncpg.create_pool(
            self.connection_string,
            init=init_connection,
            statement_cache_size=0,
        )

    async def disconnect(self) -> None:
        if self.pool:
            await self.pool.close()

    async def initialize_schema(self) -> None:
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            create_table_sql = self._load_sql("create_events_table.sql")
            async with self.pool.acquire() as conn:
                await conn.execute(create_table_sql)
        except Exception as e:
            raise EventStoreError(
                "Failed to initialize event store schema", original_error=e
            ) from e

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append event with OCC via UNIQUE constraint.

        The UNIQUE(aggregate_id, sequence_number) constraint guarantees
        only one writer succeeds. On conflict, caller reloads and retries.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
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
                    event.__class__.__name__,
                    event.model_dump(mode="json"),
                    event.occurred_at,
                    event.metadata,
                )

        except asyncpg.UniqueViolationError as e:
            raise ConcurrencyError(
                aggregate_id=str(event.aggregate_id),
                expected_version=expected_version,
            ) from e

        except Exception as e:
            raise EventStoreError(
                f"Failed to append event for aggregate {event.aggregate_id}",
                original_error=e,
            ) from e

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            # Build query with optional pagination
            query_parts = [
                "SELECT event_type, payload",
                "FROM events",
                "WHERE aggregate_id = $1",
            ]
            params: list[UUID | int] = [aggregate_id]
            param_idx = 2

            if after_sequence is not None:
                query_parts.append(f"AND sequence_number > ${param_idx}")
                params.append(after_sequence)
                param_idx += 1

            query_parts.append("ORDER BY sequence_number ASC")

            if limit is not None:
                query_parts.append(f"LIMIT ${param_idx}")
                params.append(limit)

            query = "\n".join(query_parts)

            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, *params)

            return [
                self._deserialize_event(row["event_type"], row["payload"])
                for row in rows
            ]

        except EventStoreError:
            raise

        except Exception as e:
            raise EventStoreError(
                f"Failed to retrieve events for aggregate {aggregate_id}",
                original_error=e,
            ) from e

    async def get_all_aggregate_ids(self) -> list[UUID]:
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
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                # First get paginated aggregate IDs
                if limit is not None:
                    aggregate_ids_query = """
                        SELECT DISTINCT aggregate_id
                        FROM events
                        ORDER BY aggregate_id
                        LIMIT $1 OFFSET $2
                    """
                    aggregate_rows = await conn.fetch(aggregate_ids_query, limit, offset)
                    aggregate_ids = [row["aggregate_id"] for row in aggregate_rows]

                    if not aggregate_ids:
                        return {}

                    # Then fetch events only for those aggregates
                    rows = await conn.fetch(
                        """
                        SELECT aggregate_id, event_type, payload
                        FROM events
                        WHERE aggregate_id = ANY($1)
                        ORDER BY aggregate_id, sequence_number ASC
                        """,
                        aggregate_ids,
                    )
                else:
                    # No pagination - fetch all (backward compatible)
                    rows = await conn.fetch(
                        """
                        SELECT aggregate_id, event_type, payload
                        FROM events
                        ORDER BY aggregate_id, sequence_number ASC
                        """
                    )

            grouped: dict[UUID, list[DomainEvent]] = {}
            for row in rows:
                aggregate_id = row["aggregate_id"]
                event = self._deserialize_event(row["event_type"], row["payload"])

                if aggregate_id not in grouped:
                    grouped[aggregate_id] = []
                grouped[aggregate_id].append(event)

            return grouped

        except EventStoreError:
            raise

        except Exception as e:
            raise EventStoreError(
                "Failed to retrieve all events grouped",
                original_error=e,
            ) from e
