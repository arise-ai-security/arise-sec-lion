"""Optimized hierarchy event collection using single-query approach."""

from collections import deque
from uuid import UUID

from core.domain.events.events import ChildSpawned, DomainEvent
from core.ports.event_store_port import EventStoreReadPort


class HierarchyCollector:
    """Collects events from BOSS and all descendants using optimized single query.

    Uses get_hierarchy_events_grouped() which fetches all hierarchy events
    in a single database round-trip using PostgreSQL recursive CTE.
    """

    def __init__(self, event_store: EventStoreReadPort) -> None:
        self._event_store = event_store

    async def collect(self, root_agent_id: UUID) -> list[DomainEvent]:
        """Collect all events from hierarchy, sorted by occurred_at.

        Uses single recursive CTE query instead of N+1 individual queries.
        Performance: 1 query vs N queries for N agents in hierarchy.
        """
        # Single query fetches all events for entire hierarchy
        grouped_events = await self._event_store.get_hierarchy_events_grouped(root_agent_id)

        # Flatten and sort all events
        all_events: list[DomainEvent] = []
        for events in grouped_events.values():
            all_events.extend(events)

        return sorted(all_events, key=lambda e: (e.occurred_at, e.sequence_number))

    async def collect_grouped(self, root_agent_id: UUID) -> dict[UUID, list[DomainEvent]]:
        """Collect events grouped by agent ID.

        Returns the raw grouped result from the optimized query.
        Useful when caller needs per-agent access without re-grouping.
        """
        return await self._event_store.get_hierarchy_events_grouped(root_agent_id)

    async def collect_agent_ids(self, root_agent_id: UUID) -> set[UUID]:
        """Collect just the agent IDs in the hierarchy."""
        grouped_events = await self._event_store.get_hierarchy_events_grouped(root_agent_id)
        return set(grouped_events.keys())

    async def get_hierarchy_depth(self, root_agent_id: UUID) -> int:
        """Return maximum depth of hierarchy (0 = root only).

        Calculates depth by traversing ChildSpawned relationships in-memory
        after fetching all events in single query.
        """
        grouped_events = await self._event_store.get_hierarchy_events_grouped(root_agent_id)

        if not grouped_events:
            return 0

        # Build parent->children map from ChildSpawned events
        children_map: dict[UUID, list[UUID]] = {}
        for agent_id, events in grouped_events.items():
            for event in events:
                if isinstance(event, ChildSpawned):
                    if agent_id not in children_map:
                        children_map[agent_id] = []
                    children_map[agent_id].append(event.child_id)

        # BFS to find max depth
        max_depth = 0
        queue: deque[tuple[UUID, int]] = deque([(root_agent_id, 0)])
        visited: set[UUID] = set()

        while queue:
            agent_id, depth = queue.popleft()
            if agent_id in visited:
                continue
            visited.add(agent_id)
            max_depth = max(max_depth, depth)

            for child_id in children_map.get(agent_id, []):
                if child_id not in visited:
                    queue.append((child_id, depth + 1))

        return max_depth
