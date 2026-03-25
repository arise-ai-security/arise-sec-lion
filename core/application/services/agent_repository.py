"""Agent repository for loading and persisting agent state.

Encapsulates event store operations with OCC retry logic.
"""



import logging
from typing import TYPE_CHECKING
from uuid import UUID

from core.application.types import ProgressCallback
from core.domain.aggregates.agent_session import AgentSession
from core.domain.events.events import AgentCreated, DomainEvent
from core.domain.exceptions import ConcurrencyError


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.ports.event_store_port import EventStorePort

class AgentNotFoundError(Exception):
    """Raised when an agent cannot be found in the event store."""

    def __init__(self, agent_id: UUID) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent {agent_id} not found in event store")


class AgentRepository:
    """Repository for loading and persisting agent state via event sourcing.

    Single Responsibility: Handle event store operations for agents.
    """

    def __init__(
        self,
        event_store: "EventStorePort",
        max_retries: int = 3,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self._event_store = event_store
        self._max_retries = max_retries
        self._progress_callback = progress_callback

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set the progress callback for event notifications."""
        self._progress_callback = callback

    async def load(self, agent_id: UUID) -> AgentSession:
        """Load agent from event store.

        Raises:
            AgentNotFoundError: If agent doesn't exist.
        """
        events = await self._event_store.get_events(agent_id)
        if not events:
            raise AgentNotFoundError(agent_id)
        return AgentSession.load_from_history(events)

    async def load_if_exists(self, agent_id: UUID) -> AgentSession | None:
        """Load agent if it exists, return None otherwise.

        Returns None if:
        - No events exist for this aggregate_id
        - Events exist but don't represent an AgentSession (e.g., SharedStore)
        """
        events = await self._event_store.get_events(agent_id)
        if not events:
            return None
        # Check if this is actually an AgentSession aggregate
        if not isinstance(events[0], AgentCreated):
            return None
        return AgentSession.load_from_history(events)

    async def save(self, agent: AgentSession) -> list[DomainEvent]:
        """Persist uncommitted events and return them.

        Notifies progress callback for each persisted event.
        """
        current_version = agent.version - len(agent.events)
        uncommitted = list(agent.events)

        for event in uncommitted:
            await self._event_store.append(event, expected_version=current_version)
            current_version += 1
            self._notify_progress(event, agent)

        agent.mark_changes_as_committed()
        return uncommitted

    async def save_with_retry(self, agent: AgentSession) -> list[DomainEvent]:
        """Persist events with OCC retry logic.

        Raises:
            ConcurrencyError: If max retries exceeded.
        """
        retry_count = 0
        last_error: ConcurrencyError | None = None

        while retry_count < self._max_retries:
            try:
                return await self.save(agent)
            except ConcurrencyError as e:
                last_error = e
                retry_count += 1
                if retry_count >= self._max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent.agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e
                # Reload agent for retry
                agent = await self.load(agent.agent_id)

        # Should not reach here, but satisfy type checker
        if last_error:
            raise last_error
        return []

    async def save_new_agent(self, agent: AgentSession) -> None:
        """Save a newly created agent (no retry needed, version starts at 0)."""
        for idx, event in enumerate(agent.events):
            await self._event_store.append(event, expected_version=idx)
            self._notify_progress(event, agent)
        agent.mark_changes_as_committed()

    async def persist_events(
        self,
        agent: AgentSession,
        expected_version: int,
        progress_callback: ProgressCallback | None = None,
    ) -> list[DomainEvent]:
        """Persist uncommitted events with batch optimization.

        This method consolidates the event persistence pattern used across
        the application, eliminating DRY violations.

        Args:
            agent: The agent with uncommitted events.
            expected_version: The expected version for OCC.
            progress_callback: Optional callback for each persisted event.
                             If None, uses the repository's default callback.

        Returns:
            The list of persisted events for post-processing.
        """
        uncommitted = list(agent.events)
        if not uncommitted:
            return uncommitted

        # Batch for efficiency when multiple events
        if len(uncommitted) > 1:
            await self._event_store.append_batch(uncommitted, expected_version=expected_version)
        else:
            await self._event_store.append(uncommitted[0], expected_version=expected_version)

        # Notify progress (use provided callback or fall back to repository default)
        callback = progress_callback if progress_callback is not None else self._progress_callback
        if callback is not None:
            for event in uncommitted:
                try:
                    callback(event, agent)
                except Exception:
                    logger.warning(
                        "Progress callback failed for event %s on agent %s",
                        type(event).__name__,
                        agent.agent_id,
                        exc_info=True,
                    )

        agent.mark_changes_as_committed()
        return uncommitted

    async def get_all_agent_ids(self) -> list[UUID]:
        """Get all agent IDs from event store."""
        return await self._event_store.get_all_aggregate_ids()

    async def get_all_events_grouped(self) -> dict[UUID, list[DomainEvent]]:
        """Get all events grouped by aggregate ID.

        Returns:
            Dict mapping aggregate_id to list of events.
        """
        return await self._event_store.get_all_events_grouped()

    async def get_children_events_grouped(
        self, parent_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for all children of a parent agent.

        Optimized query that filters at database level.

        Args:
            parent_id: Parent agent UUID to find children for.

        Returns:
            Dict mapping aggregate_id to list of events for children only.
        """
        return await self._event_store.get_children_events_grouped(parent_id)

    async def get_hierarchy_events_grouped(
        self, root_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for entire hierarchy starting from root agent.

        Uses recursive CTE to efficiently traverse ChildSpawned events
        and fetch all descendant events in a single query.

        Args:
            root_id: Root agent UUID to start hierarchy traversal.

        Returns:
            Dict mapping aggregate_id to list of events for hierarchy.
        """
        return await self._event_store.get_hierarchy_events_grouped(root_id)

    def _notify_progress(self, event: DomainEvent, agent: AgentSession) -> None:
        """Notify progress callback if set."""
        if self._progress_callback is not None:
            try:
                self._progress_callback(event, agent)
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)
