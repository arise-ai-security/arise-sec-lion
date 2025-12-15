"""BFS traversal to collect events from agent hierarchy."""

from collections import deque
from collections.abc import AsyncIterator
from uuid import UUID

from core.domain.events import ChildSpawned, DomainEvent, SubordinatesSpawned
from core.ports.event_store_port import EventStorePort


class HierarchyCollector:
    """Collects events from BOSS and all descendants via ChildSpawned/SubordinatesSpawned traversal."""

    def __init__(self, event_store: EventStorePort) -> None:
        self._event_store = event_store

    async def _traverse(
        self, root_agent_id: UUID
    ) -> AsyncIterator[tuple[UUID, list[DomainEvent], int]]:
        """BFS yielding (agent_id, events, depth) for each agent."""
        visited: set[UUID] = set()
        queue: deque[tuple[UUID, int]] = deque([(root_agent_id, 0)])

        while queue:
            agent_id, depth = queue.popleft()
            if agent_id in visited:
                continue
            visited.add(agent_id)

            events = await self._event_store.get_events(agent_id)
            yield agent_id, events, depth

            for event in events:
                # Handle ChildSpawned events (single child)
                if isinstance(event, ChildSpawned) and event.child_id not in visited:
                    queue.append((event.child_id, depth + 1))
                # Handle SubordinatesSpawned events (multiple children with same task)
                elif isinstance(event, SubordinatesSpawned):
                    for config in event.subordinate_configs:
                        child_id = config.get("child_id")
                        if child_id:
                            if isinstance(child_id, str):
                                child_id = UUID(child_id)
                            if child_id not in visited:
                                queue.append((child_id, depth + 1))

    async def collect(self, root_agent_id: UUID) -> list[DomainEvent]:
        """Collect all events from hierarchy, sorted by occurred_at."""
        all_events: list[DomainEvent] = []
        async for _agent_id, events, _depth in self._traverse(root_agent_id):
            all_events.extend(events)
        return sorted(all_events, key=lambda e: (e.occurred_at, e.sequence_number))

    async def collect_agent_ids(self, root_agent_id: UUID) -> set[UUID]:
        """Collect just the agent IDs in the hierarchy."""
        agent_ids: set[UUID] = set()
        async for agent_id, _events, _depth in self._traverse(root_agent_id):
            agent_ids.add(agent_id)
        return agent_ids

    async def get_hierarchy_depth(self, root_agent_id: UUID) -> int:
        """Return maximum depth of hierarchy (0 = root only)."""
        max_depth = 0
        async for _agent_id, _events, depth in self._traverse(root_agent_id):
            max_depth = max(max_depth, depth)
        return max_depth
