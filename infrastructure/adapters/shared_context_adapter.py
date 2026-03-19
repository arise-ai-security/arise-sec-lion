"""PostgreSQL adapter for SharedStore persistence.

Uses the same event store as AgentSession for consistency.
Events are keyed by a derived aggregate_id (from root_id) to avoid PK collision.
"""



from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.shared_context import (
    Artifact,
    Decision,
    SharedStore,
    shared_context_aggregate_id,
)
from core.ports.runtime_ports import SharedContextPort


if TYPE_CHECKING:
    from infrastructure.adapters.postgres_event_store import PostgresEventStore


class PostgresSharedContextAdapter(SharedContextPort):
    """Shared context backed by PostgreSQL event store.

    Uses the same events table as agent events, with a derived aggregate_id
    (from root_id) to avoid PK collision with AgentSession events.
    This ensures OCC via the existing UNIQUE constraint.
    """

    def __init__(self, event_store: "PostgresEventStore") -> None:
        """Initialize with existing event store.

        Args:
            event_store: PostgreSQL event store adapter
        """
        self._event_store = event_store

    async def get_or_create(
        self,
        root_id: UUID,
        initial_budget_usd: float = 0.0,
        config: dict | None = None,
    ) -> SharedStore:
        """Get existing context or create new one.

        Args:
            root_id: Root agent ID
            initial_budget_usd: Budget limit for new context (0 = unlimited)
            config: Configuration for new context

        Returns:
            Existing or newly created SharedStore
        """
        aggregate_id = shared_context_aggregate_id(root_id)
        events = await self._event_store.get_events(aggregate_id)
        if events:
            return SharedStore.load_from_history(events)
        return SharedStore.create(
            root_id=root_id,
            initial_budget_usd=initial_budget_usd,
            config=config,
        )

    async def get(self, root_id: UUID) -> SharedStore | None:
        """Get shared context by root ID.

        Args:
            root_id: Root agent ID

        Returns:
            SharedStore if exists, None otherwise
        """
        aggregate_id = shared_context_aggregate_id(root_id)
        events = await self._event_store.get_events(aggregate_id)
        if not events:
            return None
        return SharedStore.load_from_history(events)

    async def save(
        self,
        context: SharedStore,
        expected_version: int,
    ) -> None:
        """Save shared context with OCC.

        Persists all uncommitted events from the context.

        Args:
            context: The context to save
            expected_version: Expected version for OCC

        Raises:
            ConcurrencyError: If version mismatch
        """
        current_version = expected_version
        for event in context.events:
            await self._event_store.append(event, expected_version=current_version)
            current_version += 1
        context.mark_changes_as_committed()

    async def exists(self, root_id: UUID) -> bool:
        """Check if shared context exists.

        Args:
            root_id: Root agent ID

        Returns:
            True if context exists
        """
        aggregate_id = shared_context_aggregate_id(root_id)
        events = await self._event_store.get_events(aggregate_id)
        return len(events) > 0

    async def get_artifact(self, root_id: UUID, key: str) -> Artifact | None:
        """Get artifact from shared context.

        Args:
            root_id: Root agent ID
            key: Artifact key

        Returns:
            Artifact if exists, None otherwise
        """
        context = await self.get(root_id)
        if context is None:
            return None
        return context.get_artifact(key)

    async def get_decision(self, root_id: UUID, key: str) -> Decision | None:
        """Get decision from shared context.

        Args:
            root_id: Root agent ID
            key: Decision key

        Returns:
            Decision if exists, None otherwise
        """
        context = await self.get(root_id)
        if context is None:
            return None
        return context.get_decision(key)
