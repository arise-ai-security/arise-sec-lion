"""Port for task deduplication operations.

Follows Hexagonal Architecture:
- Port defines the contract (abstraction)
- Implementation in infrastructure/adapters/task_registry_adapter.py
- Uses CQRS projection table for O(1) reads

This port follows Interface Segregation Principle:
- Dedicated to task deduplication (single responsibility)
- Separate from SharedContextPort (which is event-sourced)
"""



from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.services import RegisteredTask


class TaskRegistryPort(Protocol):
    """Port for task deduplication operations (CQRS read model).

    Provides atomic task registration with advisory locking to prevent
    duplicate task creation during concurrent decomposition.
    """

    async def ensure_table_exists(self) -> None:
        """Create task_registry table if it doesn't exist.

        Called during application startup.
        """
        ...

    async def register_if_not_exists(
        self,
        root_id: UUID,
        task_key: str,
        task_description: str,
        registered_by: UUID,
        parent_id: UUID | None,
    ) -> bool:
        """Atomically register a task if not already registered.

        Uses PostgreSQL advisory lock for atomicity - no retry storms.

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
        ...

    async def get_all_for_root(self, root_id: UUID) -> list[RegisteredTask]:
        """Get all registered tasks for a root_id.

        Used to build prompt context so LLM can avoid creating duplicates.

        Args:
            root_id: Root agent ID (execution run identifier).

        Returns:
            List of RegisteredTask value objects.
        """
        ...

    # TODO: Add cleanup_failed_tree(root_id: UUID) method
    # When a task tree fails, all tasks registered under that root_id should be
    # removed from the registry. This allows retry attempts to re-register the
    # same tasks without being blocked by stale entries from the failed run.
    # Implementation: DELETE FROM task_registry WHERE root_id = $1
