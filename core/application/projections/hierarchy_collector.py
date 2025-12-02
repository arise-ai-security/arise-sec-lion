"""Hierarchy collector for recursive event collection.

This module provides functionality to collect all events from an agent
hierarchy (BOSS + all descendants) by traversing the tree via ChildSpawned
events.
"""

from collections import deque
from uuid import UUID

from core.domain.events import ChildSpawned, DomainEvent
from core.ports.event_store_port import EventStorePort


class HierarchyCollector:
    """Collects all events from a BOSS agent and all its descendants.

    This collector traverses the agent hierarchy by looking for ChildSpawned
    events, which contain the child_id of spawned agents. It performs a
    breadth-first traversal to collect events from all agents in the tree.

    The collector:
    1. Starts with the root agent (typically BOSS)
    2. Fetches all events for that agent
    3. Finds ChildSpawned events to discover child agents
    4. Recursively collects events from all children
    5. Returns all events sorted by occurred_at timestamp

    Usage:
        collector = HierarchyCollector(event_store)
        events = await collector.collect(boss_agent_id)
    """

    def __init__(self, event_store: EventStorePort) -> None:
        """Initialize with an event store.

        Args:
            event_store: The event store to read events from.
        """
        self._event_store = event_store

    async def collect(self, root_agent_id: UUID) -> list[DomainEvent]:
        """Recursively collect events from entire agent hierarchy.

        Performs a breadth-first traversal of the agent tree, collecting
        events from each agent and discovering children via ChildSpawned events.

        Args:
            root_agent_id: UUID of the root agent (typically BOSS).

        Returns:
            List of all events from the hierarchy, sorted by occurred_at.
        """
        all_events: list[DomainEvent] = []
        visited: set[UUID] = set()
        queue: deque[UUID] = deque([root_agent_id])

        while queue:
            agent_id = queue.popleft()

            # Skip if already visited (shouldn't happen in a tree, but safe)
            if agent_id in visited:
                continue
            visited.add(agent_id)

            # Fetch events for this agent
            events = await self._event_store.get_events(agent_id)
            all_events.extend(events)

            # Find child agents from ChildSpawned events
            for event in events:
                if isinstance(event, ChildSpawned) and event.child_id not in visited:
                    queue.append(event.child_id)

        # Sort by occurred_at for chronological order
        return sorted(all_events, key=lambda e: (e.occurred_at, e.sequence_number))

    async def collect_agent_ids(self, root_agent_id: UUID) -> set[UUID]:
        """Collect just the agent IDs in the hierarchy.

        Useful for discovering the scope of a run without fetching
        all event data.

        Args:
            root_agent_id: UUID of the root agent (typically BOSS).

        Returns:
            Set of all agent UUIDs in the hierarchy.
        """
        visited: set[UUID] = set()
        queue: deque[UUID] = deque([root_agent_id])

        while queue:
            agent_id = queue.popleft()

            if agent_id in visited:
                continue
            visited.add(agent_id)

            # Fetch events to discover children
            events = await self._event_store.get_events(agent_id)
            for event in events:
                if isinstance(event, ChildSpawned) and event.child_id not in visited:
                    queue.append(event.child_id)

        return visited

    async def get_hierarchy_depth(self, root_agent_id: UUID) -> int:
        """Calculate the depth of the agent hierarchy.

        Depth 0 = just the root agent
        Depth 1 = root + direct children
        etc.

        Args:
            root_agent_id: UUID of the root agent.

        Returns:
            Maximum depth of the hierarchy tree.
        """
        max_depth = 0
        visited: set[UUID] = set()
        # Queue entries: (agent_id, depth)
        queue: deque[tuple[UUID, int]] = deque([(root_agent_id, 0)])

        while queue:
            agent_id, depth = queue.popleft()

            if agent_id in visited:
                continue
            visited.add(agent_id)
            max_depth = max(max_depth, depth)

            events = await self._event_store.get_events(agent_id)
            for event in events:
                if isinstance(event, ChildSpawned) and event.child_id not in visited:
                    queue.append((event.child_id, depth + 1))

        return max_depth
