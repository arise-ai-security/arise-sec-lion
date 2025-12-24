"""PostgreSQL context dashboard with asyncpg."""

from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import orjson

from core.domain.context_entry import ContextEntry
from core.domain.subtask import WorkerReport
from core.ports.context_dashboard_port import ContextDashboardPort


class ContextDashboardError(Exception):
    """Error in context dashboard operations."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.message = message
        self.original_error = original_error
        super().__init__(message)


class PostgresContextDashboard(ContextDashboardPort):
    """PostgreSQL implementation of the context dashboard for cross-session learning."""

    SQL_DIR = Path(__file__).parent.parent / "sql"

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.pool: asyncpg.Pool | None = None

    @classmethod
    def _load_sql(cls, filename: str) -> str:
        sql_path = cls.SQL_DIR / filename
        return sql_path.read_text(encoding="utf-8")

    async def connect(self) -> None:
        """Initialize connection pool with JSONB codec."""

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
        """Close connection pool."""
        if self.pool:
            await self.pool.close()

    async def initialize_schema(self) -> None:
        """Create context dashboard table."""
        if not self.pool:
            raise ContextDashboardError(
                "Connection pool not initialized. Call connect() first."
            )

        try:
            create_sql = self._load_sql("create_context_dashboard_table.sql")
            async with self.pool.acquire() as conn:
                await conn.execute(create_sql)
        except Exception as e:
            raise ContextDashboardError(
                "Failed to initialize context dashboard schema", original_error=e
            ) from e

    async def publish(self, entry: ContextEntry) -> UUID:
        """Publish a context entry to the dashboard."""
        if not self.pool:
            raise ContextDashboardError(
                "Connection pool not initialized. Call connect() first."
            )

        entry_id = uuid4()

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO context_dashboard (
                        entry_id, work_title, objective, justification,
                        work_analysis, worker_report, session_id, worker_id,
                        created_at, tags
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    entry_id,
                    entry.work_title,
                    entry.objective,
                    entry.justification,
                    entry.work_analysis,
                    entry.worker_report.model_dump(mode="json"),
                    entry.session_id,
                    entry.worker_id,
                    entry.created_at,
                    entry.tags,
                )
        except Exception as e:
            raise ContextDashboardError(
                "Failed to publish context entry", original_error=e
            ) from e

        return entry_id

    async def get_all_titles(self) -> list[tuple[UUID, str]]:
        """Get all work titles for relevance matching, ordered by most recent first."""
        if not self.pool:
            raise ContextDashboardError(
                "Connection pool not initialized. Call connect() first."
            )

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT entry_id, work_title
                    FROM context_dashboard
                    ORDER BY created_at DESC
                    """
                )
            return [(row["entry_id"], row["work_title"]) for row in rows]
        except Exception as e:
            raise ContextDashboardError(
                "Failed to get context titles", original_error=e
            ) from e

    async def get_entries_by_ids(self, entry_ids: list[UUID]) -> list[ContextEntry]:
        """Retrieve full context entries by their IDs."""
        if not self.pool:
            raise ContextDashboardError(
                "Connection pool not initialized. Call connect() first."
            )

        if not entry_ids:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT * FROM context_dashboard
                    WHERE entry_id = ANY($1)
                    """,
                    entry_ids,
                )
            return [self._row_to_entry(row) for row in rows]
        except Exception as e:
            raise ContextDashboardError(
                "Failed to get context entries by IDs", original_error=e
            ) from e

    async def get_entry(self, entry_id: UUID) -> ContextEntry | None:
        """Retrieve a single context entry."""
        if not self.pool:
            raise ContextDashboardError(
                "Connection pool not initialized. Call connect() first."
            )

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM context_dashboard WHERE entry_id = $1",
                    entry_id,
                )
            return self._row_to_entry(row) if row else None
        except Exception as e:
            raise ContextDashboardError(
                "Failed to get context entry", original_error=e
            ) from e

    def _row_to_entry(self, row: asyncpg.Record) -> ContextEntry:
        """Convert database row to ContextEntry."""
        report_data = row["worker_report"]
        if isinstance(report_data, str):
            report_data = orjson.loads(report_data)

        return ContextEntry(
            entry_id=row["entry_id"],
            work_title=row["work_title"],
            objective=row["objective"],
            justification=row["justification"],
            work_analysis=row["work_analysis"],
            worker_report=WorkerReport(**report_data),
            session_id=row["session_id"],
            worker_id=row["worker_id"],
            created_at=row["created_at"],
            tags=list(row["tags"]) if row["tags"] else [],
        )
