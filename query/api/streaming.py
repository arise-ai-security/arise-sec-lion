"""Incremental hierarchy tracking for SSE event streaming.

Stateful batch-incremental event fetching with child discovery, used by the
events SSE endpoints to reduce database load from O(total_events) to
O(new_events) per poll.
"""

from uuid import UUID

from core.domain.events.events import ChildSpawned, DomainEvent
from core.ports.event_store_port import EventStoreReadPort


class IncrementalHierarchyTracker:
    """Tracks hierarchy state for incremental event fetching.

    Uses optimized batch fetching on first load, then batch incremental updates.
    This reduces database load from O(total_events) to O(new_events) per poll.

    Optimized: Uses single batch query for incremental updates instead of N queries.
    """

    def __init__(self, event_store: EventStoreReadPort, root_id: UUID) -> None:
        self._event_store = event_store
        self._root_id = root_id
        # Track last seen sequence per agent
        self._last_sequence: dict[UUID, int] = {}
        # Agents we know about
        self._known_agents: set[UUID] = set()
        # All events accumulated (for summary projection)
        self._all_events: list[DomainEvent] = []
        # Whether initial load has been done
        self._initialized = False
        # New children discovered that need initial fetch
        self._pending_children: set[UUID] = set()

    async def fetch_new_events(self) -> list[DomainEvent]:
        """Fetch only new events since last poll.

        First call uses optimized single-query approach for entire hierarchy.
        Subsequent calls use batch incremental fetch (1 query instead of N).
        """
        new_events: list[DomainEvent] = []

        # First call: use optimized single-query approach
        if not self._initialized:
            self._initialized = True
            grouped = await self._event_store.get_hierarchy_events_grouped(self._root_id)

            for agent_id, events in grouped.items():
                self._known_agents.add(agent_id)
                if events:
                    self._last_sequence[agent_id] = events[-1].sequence_number
                    new_events.extend(events)

            self._all_events.extend(new_events)
            return sorted(new_events, key=lambda e: (e.occurred_at, e.sequence_number))

        # Subsequent calls: batch incremental fetch (1 query instead of N)
        # Build dict of agent_id -> last_sequence for batch query
        agent_sequences: dict[UUID, int | None] = {}
        for agent_id in self._known_agents:
            agent_sequences[agent_id] = self._last_sequence.get(agent_id)

        # Include pending children (new agents discovered via ChildSpawned)
        for child_id in self._pending_children:
            agent_sequences[child_id] = None  # Fetch all events for new agents
        self._pending_children.clear()

        # Single batch query for all agents
        if agent_sequences:
            grouped = await self._event_store.get_events_batch_incremental(agent_sequences)

            for agent_id, events in grouped.items():
                if events:
                    self._last_sequence[agent_id] = events[-1].sequence_number
                    new_events.extend(events)

                    # Discover new children
                    for event in events:
                        if isinstance(event, ChildSpawned):
                            child_id = event.child_id
                            if child_id not in self._known_agents:
                                self._known_agents.add(child_id)
                                self._pending_children.add(child_id)

        self._all_events.extend(new_events)
        return sorted(new_events, key=lambda e: (e.occurred_at, e.sequence_number))

    def get_all_events(self) -> list[DomainEvent]:
        return sorted(self._all_events, key=lambda e: (e.occurred_at, e.sequence_number))

    def has_pending_children(self) -> bool:
        return len(self._pending_children) > 0
