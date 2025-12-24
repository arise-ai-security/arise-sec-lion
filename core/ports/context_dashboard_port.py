"""Context Dashboard port: global key-value store for cross-session learning."""

from typing import Protocol
from uuid import UUID

from core.domain.context_entry import ContextEntry


class ContextDashboardPort(Protocol):
    """Port for global context dashboard (cross-session knowledge store).

    Stores context entries published by supervisors when workers complete tasks.
    Other workers can query this store to find relevant context from previous
    work and learn from past experiences.
    """

    async def connect(self) -> None:
        """Connect to backend."""
        ...

    async def disconnect(self) -> None:
        """Close connection."""
        ...

    async def initialize_schema(self) -> None:
        """Create tables/schema."""
        ...

    async def publish(self, entry: ContextEntry) -> UUID:
        """Publish a context entry to the dashboard.

        Args:
            entry: The ContextEntry to store.

        Returns:
            The UUID assigned to this entry.
        """
        ...

    async def get_all_titles(self) -> list[tuple[UUID, str]]:
        """Get all work titles for relevance matching.

        Returns:
            List of (entry_id, work_title) tuples, ordered by created_at DESC.
        """
        ...

    async def get_entries_by_ids(self, entry_ids: list[UUID]) -> list[ContextEntry]:
        """Retrieve full context entries by their IDs.

        Args:
            entry_ids: List of entry UUIDs to fetch.

        Returns:
            List of ContextEntry objects (order not guaranteed).
        """
        ...

    async def get_entry(self, entry_id: UUID) -> ContextEntry | None:
        """Retrieve a single context entry.

        Args:
            entry_id: The entry UUID.

        Returns:
            The ContextEntry or None if not found.
        """
        ...
