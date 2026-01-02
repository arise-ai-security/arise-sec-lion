"""PostgreSQL event store with asyncpg and OCC."""

from pathlib import Path
from uuid import UUID

import asyncpg
import orjson

from core.domain.events.events import (
    AgentCreated,
    ArtifactStored,
    BudgetConsumed,
    BudgetExceeded,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    ConfigOverrideSet,
    DecisionRecorded,
    DomainEvent,
    LimitEnforced,
    ProgressUpdated,
    PromptSent,
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
    "ChildFailed": ChildFailed,
    "ComplexityEvaluated": ComplexityEvaluated,
    # Observability events
    "PromptSent": PromptSent,
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

    async def append_batch(
        self,
        events: list[DomainEvent],
        expected_version: int,
    ) -> None:
        """Append multiple events atomically in a single transaction.

        This is significantly faster than individual appends for workers
        that produce many events (thoughts, tool uses, etc.).

        Args:
            events: List of events to persist.
            expected_version: Expected version before first event.
        """
        if not events:
            return

        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(
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
                        [
                            (
                                event.event_id,
                                event.aggregate_id,
                                event.sequence_number,
                                event.__class__.__name__,
                                event.model_dump(mode="json"),
                                event.occurred_at,
                                event.metadata,
                            )
                            for event in events
                        ],
                    )

        except asyncpg.UniqueViolationError as e:
            raise ConcurrencyError(
                aggregate_id=str(events[0].aggregate_id),
                expected_version=expected_version,
            ) from e

        except Exception as e:
            raise EventStoreError(
                f"Failed to append batch for aggregate {events[0].aggregate_id}",
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
                    # No pagination - fetch all events
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

    async def get_boss_agents_grouped(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for BOSS agents only using optimized single-query approach.

        Uses a subquery to filter BOSS agents at the database level,
        avoiding the expensive 2-query approach and Python-side filtering.

        Performance: ~188ms (single round trip) vs ~1079ms (2 round trips).
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                # Single query: filter BOSS agents + fetch all their events
                query = """
                    SELECT e.aggregate_id, e.event_type, e.payload
                    FROM events e
                    WHERE e.aggregate_id IN (
                        SELECT aggregate_id
                        FROM events
                        WHERE event_type = 'AgentCreated'
                          AND payload->>'role' = 'boss'
                        ORDER BY occurred_at DESC
                        LIMIT $1 OFFSET $2
                    )
                    ORDER BY e.aggregate_id, e.sequence_number ASC
                """
                # Use a large default limit when None
                effective_limit = limit if limit is not None else 10000
                rows = await conn.fetch(query, effective_limit, offset)

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
                "Failed to retrieve BOSS agents events",
                original_error=e,
            ) from e

    async def get_hierarchy_events_grouped(
        self,
        root_id: UUID,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for an agent hierarchy using recursive CTE.

        Uses PostgreSQL recursive CTE to traverse ChildSpawned events
        and fetch all events for the hierarchy in a single query.

        Performance: Single round trip vs N+1 queries for N agents.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                # Recursive CTE to find all agents in hierarchy
                query = """
                    WITH RECURSIVE hierarchy AS (
                        -- Base case: root agent
                        SELECT $1::uuid AS agent_id

                        UNION

                        -- Recursive case: children via ChildSpawned events
                        SELECT (e.payload->>'child_id')::uuid AS agent_id
                        FROM events e
                        INNER JOIN hierarchy h ON e.aggregate_id = h.agent_id
                        WHERE e.event_type = 'ChildSpawned'
                    )
                    SELECT e.aggregate_id, e.event_type, e.payload
                    FROM events e
                    INNER JOIN hierarchy h ON e.aggregate_id = h.agent_id
                    ORDER BY e.aggregate_id, e.sequence_number ASC
                """
                rows = await conn.fetch(query, root_id)

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
                f"Failed to retrieve hierarchy events for {root_id}",
                original_error=e,
            ) from e

    async def get_children_events_grouped(
        self,
        parent_id: UUID,
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for all children of a parent agent.

        Filters at database level using parent_id from AgentCreated payload.

        Performance: Single query vs fetching all events + Python filter.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                # Single query: find children by parent_id in AgentCreated payload
                query = """
                    SELECT e.aggregate_id, e.event_type, e.payload
                    FROM events e
                    WHERE e.aggregate_id IN (
                        SELECT aggregate_id
                        FROM events
                        WHERE event_type = 'AgentCreated'
                          AND (payload->>'parent_id')::uuid = $1
                    )
                    ORDER BY e.aggregate_id, e.sequence_number ASC
                """
                rows = await conn.fetch(query, parent_id)

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
                f"Failed to retrieve children events for parent {parent_id}",
                original_error=e,
            ) from e

    async def get_boss_agent_summaries(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get BOSS agent summaries directly from SQL.

        Uses SQL-level projection to extract summary data without
        fetching/deserializing full event streams.

        Performance: ~0.3ms vs ~22ms for event-based approach.
        """
        if not self.pool:
            raise EventStoreError("Connection pool not initialized. Call connect() first.")

        try:
            async with self.pool.acquire() as conn:
                # Use CTEs with batch lookups instead of correlated subqueries
                # This reduces ~201 queries (1 + 100 + 100) to a single query
                query = """
                    WITH boss_agents AS (
                        SELECT
                            aggregate_id as agent_id,
                            payload->>'role' as role,
                            occurred_at as created_at
                        FROM events
                        WHERE event_type = 'AgentCreated'
                          AND payload->>'role' = 'boss'
                        ORDER BY occurred_at DESC
                        LIMIT $1 OFFSET $2
                    ),
                    task_descriptions AS (
                        SELECT DISTINCT ON (e.aggregate_id)
                            e.aggregate_id,
                            e.payload->>'task_description' as task_description
                        FROM events e
                        INNER JOIN boss_agents b ON e.aggregate_id = b.agent_id
                        WHERE e.event_type = 'TaskAssigned'
                        ORDER BY e.aggregate_id
                    ),
                    latest_status AS (
                        SELECT DISTINCT ON (e.aggregate_id)
                            e.aggregate_id,
                            CASE e.event_type
                                WHEN 'WorkCompleted' THEN 'completed'
                                WHEN 'WorkFailed' THEN 'failed'
                                ELSE e.payload->>'new_status'
                            END as status
                        FROM events e
                        INNER JOIN boss_agents b ON e.aggregate_id = b.agent_id
                        WHERE e.event_type IN ('StatusChanged', 'WorkCompleted', 'WorkFailed')
                        ORDER BY e.aggregate_id, e.occurred_at DESC
                    )
                    SELECT
                        b.agent_id,
                        b.role,
                        b.created_at,
                        t.task_description,
                        COALESCE(s.status, 'analyzing') as status
                    FROM boss_agents b
                    LEFT JOIN task_descriptions t ON t.aggregate_id = b.agent_id
                    LEFT JOIN latest_status s ON s.aggregate_id = b.agent_id
                    ORDER BY b.created_at DESC
                """
                rows = await conn.fetch(query, limit, offset)

            return [
                {
                    "agent_id": row["agent_id"],
                    "role": row["role"],
                    "status": row["status"],
                    "task_description": row["task_description"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        except EventStoreError:
            raise

        except Exception as e:
            raise EventStoreError(
                "Failed to retrieve BOSS agent summaries",
                original_error=e,
            ) from e
