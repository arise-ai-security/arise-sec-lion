"""PostgreSQL adapter for task deduplication with CQRS projection table.

Uses a dedicated projection table (task_registry) for O(1) lookups,
with PostgreSQL advisory locks for atomic registration.

This is a CQRS read model - separate from the event-sourced SharedContext.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.services import RegisteredTask
from core.ports.task_registry_port import TaskRegistryPort

if TYPE_CHECKING:
    from infrastructure.adapters.postgres_event_store import PostgresEventStore

logger = logging.getLogger(__name__)


class PostgresTaskRegistryAdapter(TaskRegistryPort):
    """CQRS projection table for task deduplication with advisory locking.

    Uses PostgreSQL advisory locks (pg_advisory_xact_lock) for atomic
    registration - no retry storms when multiple agents try to register
    the same task concurrently.

    The projection table provides O(1) lookups via primary key, much faster
    than replaying all SharedContext events.
    """

    SQL_DIR = Path(__file__).parent.parent / "sql"

    def __init__(self, event_store: PostgresEventStore) -> None:
        """Initialize with event store (shares its connection pool).

        Args:
            event_store: PostgreSQL event store adapter (provides pool access)
        """
        self._event_store = event_store

    @property
    def _pool(self):
        """Get connection pool from event store."""
        if self._event_store.pool is None:
            raise RuntimeError("Event store not connected - call connect() first")
        return self._event_store.pool

    @classmethod
    def _load_sql(cls, filename: str) -> str:
        """Load SQL from file."""
        sql_path = cls.SQL_DIR / filename
        return sql_path.read_text(encoding="utf-8")

    async def ensure_table_exists(self) -> None:
        """Create task_registry table if it doesn't exist.

        Called during application startup.
        """
        sql = self._load_sql("create_task_registry_table.sql")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def register_if_not_exists(
        self,
        root_id: UUID,
        task_key: str,
        task_description: str,
        registered_by: UUID,
        parent_id: UUID | None,
    ) -> bool:
        """Atomically register a task if not already registered.

        Uses PostgreSQL advisory lock for atomicity - other processes
        trying to register the same task_key will wait (not fail/retry).

        Args:
            root_id: Root agent ID (execution run identifier).
            task_key: Normalized hash of task description (16 chars).
            task_description: Original task description (truncated to 200 chars).
            registered_by: Agent ID that is registering this task.
            parent_id: Parent agent ID (for hierarchy tracking).

        Returns:
            True if task was registered (new task).
            False if task already exists (duplicate).
        """
        # Generate lock ID from task_key (deterministic, fits in bigint)
        # Using first 15 hex chars = 60 bits, fits in signed bigint
        lock_id = int(hashlib.sha256(task_key.encode()).hexdigest()[:15], 16)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Acquire advisory lock (transaction-scoped, auto-released on commit)
                # This serializes concurrent registrations for the same task_key
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_id)

                # Check if task already exists (O(1) via primary key)
                exists = await conn.fetchval(
                    "SELECT 1 FROM task_registry WHERE task_key = $1",
                    task_key,
                )
                if exists:
                    logger.debug(
                        "Task already registered: key=%s, root=%s", task_key, root_id
                    )
                    return False  # Duplicate

                # Insert new task (truncate description to 200 chars)
                await conn.execute(
                    """INSERT INTO task_registry
                       (task_key, root_id, task_description, registered_by, parent_id)
                       VALUES ($1, $2, $3, $4, $5)""",
                    task_key,
                    root_id,
                    task_description[:200],
                    registered_by,
                    parent_id,
                )
                logger.debug(
                    "Task registered: key=%s, root=%s, by=%s",
                    task_key,
                    root_id,
                    registered_by,
                )
                return True  # Successfully registered
        # Advisory lock auto-released when transaction commits

    async def get_all_for_root(self, root_id: UUID) -> list[RegisteredTask]:
        """Get all registered tasks for a root_id.

        Used to build prompt context so LLM can avoid creating duplicates.

        Args:
            root_id: Root agent ID (execution run identifier).

        Returns:
            List of RegisteredTask value objects.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT task_key, task_description, registered_by, parent_id
                   FROM task_registry
                   WHERE root_id = $1
                   ORDER BY created_at""",
                root_id,
            )
            return [
                RegisteredTask(
                    task_key=row["task_key"],
                    task_description=row["task_description"],
                    registered_by=row["registered_by"],
                    parent_id=row["parent_id"],
                )
                for row in rows
            ]

    async def cleanup_old_runs(self, before_days: int = 7) -> int:
        """Delete task registrations older than specified days.

        Maintenance operation to prevent table bloat.

        Args:
            before_days: Delete registrations older than this many days.

        Returns:
            Number of rows deleted.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """DELETE FROM task_registry
                   WHERE created_at < NOW() - INTERVAL '$1 days'""",
                before_days,
            )
            # Result is like "DELETE 42"
            deleted = int(result.split()[-1]) if result else 0
            logger.info("Cleaned up %d old task registrations", deleted)
            return deleted

    # TODO: Add cleanup_failed_tree(root_id: UUID) -> int method
    # When a task tree fails, all tasks registered under that root_id should be
    # removed from the registry. This allows retry attempts to re-register the
    # same tasks without being blocked by stale entries from the failed run.
    #
    # async def cleanup_failed_tree(self, root_id: UUID) -> int:
    #     async with self._pool.acquire() as conn:
    #         result = await conn.execute(
    #             "DELETE FROM task_registry WHERE root_id = $1",
    #             root_id,
    #         )
    #         deleted = int(result.split()[-1]) if result else 0
    #         logger.info("Cleaned up %d tasks for failed tree: root=%s", deleted, root_id)
    #         return deleted
