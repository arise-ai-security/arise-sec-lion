"""PostgreSQL event store with asyncpg and OCC."""

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
    """PostgreSQL event store with append-only log and OCC via UNIQUE constraint."""

    SQL_DIR = Path(__file__).parent.parent / "sql"

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.pool: asyncpg.Pool | None = None

    @classmethod
    def _load_sql(cls, filename: str) -> str:
        sql_path = cls.SQL_DIR / filename
        return sql_path.read_text(encoding="utf-8")

    async def connect(self) -> None:
        async def init_connection(conn: asyncpg.Connection) -> None:
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
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            event_dict = event.model_dump(mode="json")
            event_type = event.__class__.__name__

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
                    event_type,
                    event_dict,
                    event.occurred_at,
                    event.metadata,
                )

        except asyncpg.UniqueViolationError as e:
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

            events: list[DomainEvent] = []
            for row in rows:
                event_type = row["event_type"]
                payload_json = row["payload"]

                event_class = EVENT_TYPE_REGISTRY.get(event_type)
                if not event_class:
                    raise EventStoreError(
                        f"Unknown event type '{event_type}' for aggregate {aggregate_id}"
                    )

                try:
                    if isinstance(payload_json, str):
                        payload_dict = orjson.loads(payload_json)
                    else:
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
