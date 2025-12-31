"""Query service for agent hierarchy traversal and execution ordering.

This service implements the left-to-right worker execution guarantee through:
1. Hierarchical path computation - tuple paths from root to each agent
2. Left sibling blocking check - ensures no incomplete work to the left
3. Sequential worker filtering - only returns the leftmost eligible worker
"""

from collections import deque
from uuid import UUID

from core.domain.events import ChildSpawned, SubordinatesSpawned
from core.domain.model import AgentRole, AgentSession
from core.ports.event_store_port import EventStorePort


class QueryService:
    """Service for querying agent hierarchy with execution ordering guarantees.

    The system guarantees left-to-right worker execution through 4 key mechanisms:

    1. Sibling Indexing at Creation
       Each agent gets a sibling_index (0 = leftmost) when spawned.

    2. Hierarchical Path Computation
       Builds a tuple path from root to each agent:
       - Worker W1: (0, 0, 0) - root's first child's first child
       - Worker W2: (0, 0, 1) - root's first child's second child
       - Worker W3: (0, 1, 0) - root's second child's first child
       These paths sort lexicographically in left-to-right order.

    3. Left Sibling Blocking Check
       Before a worker can execute, verifies no incomplete work exists to its left.
       Walks up ancestor chain; for EACH ancestor, checks if left siblings
       (and their subtrees) are complete.

    4. Selective Worker Filtering
       With sequential_workers=True, only one worker executes at a time
       (the leftmost eligible), while managers decompose in parallel.

    Attributes:
        event_store: Port for loading domain events.
    """

    def __init__(self, event_store: EventStorePort) -> None:
        """Initialize query service with event store dependency.

        Args:
            event_store: Event store implementation for loading agent state.
        """
        self._event_store = event_store
        # Cache for agent data during a single query operation
        self._agent_cache: dict[UUID, AgentSession] = {}
        # Cache for parent-child relationships
        self._children_cache: dict[UUID, list[tuple[UUID, int]]] = {}  # parent_id -> [(child_id, sibling_index)]

    async def _load_agent(self, agent_id: UUID) -> AgentSession | None:
        """Load an agent from cache or event store.

        Args:
            agent_id: UUID of the agent to load.

        Returns:
            AgentSession if found, None otherwise.
        """
        if agent_id in self._agent_cache:
            return self._agent_cache[agent_id]

        events = await self._event_store.get_events(agent_id)
        if not events:
            return None

        agent = AgentSession.load_from_history(events)
        self._agent_cache[agent_id] = agent
        return agent

    async def _build_hierarchy_data(self, root_id: UUID) -> None:
        """Build hierarchy data structures via BFS traversal.

        Populates:
        - _agent_cache: agent_id -> AgentSession
        - _children_cache: parent_id -> [(child_id, sibling_index)]

        Args:
            root_id: Root agent ID to start traversal from.
        """
        self._agent_cache.clear()
        self._children_cache.clear()

        visited: set[UUID] = set()
        queue: deque[UUID] = deque([root_id])

        while queue:
            agent_id = queue.popleft()
            if agent_id in visited:
                continue
            visited.add(agent_id)

            events = await self._event_store.get_events(agent_id)
            if not events:
                continue

            agent = AgentSession.load_from_history(events)
            self._agent_cache[agent_id] = agent

            # Extract children with their sibling indices
            sibling_index = 0
            children: list[tuple[UUID, int]] = []

            for event in events:
                if isinstance(event, ChildSpawned):
                    children.append((event.child_id, sibling_index))
                    sibling_index += 1
                    if event.child_id not in visited:
                        queue.append(event.child_id)
                elif isinstance(event, SubordinatesSpawned):
                    for config in event.subordinate_configs:
                        child_id = config.get("child_id")
                        if child_id:
                            if isinstance(child_id, str):
                                child_id = UUID(child_id)
                            children.append((child_id, sibling_index))
                            sibling_index += 1
                            if child_id not in visited:
                                queue.append(child_id)

            if children:
                self._children_cache[agent_id] = children

    def _compute_hierarchical_path(self, agent_id: UUID) -> tuple[int, ...]:
        """Compute the hierarchical path (tuple of sibling indices) for an agent.

        The path represents the position in the tree:
        - Root: (0,)
        - Root's first child: (0, 0)
        - Root's second child: (0, 1)
        - First child's first child: (0, 0, 0)
        - First child's second child: (0, 0, 1)
        - Second child's first child: (0, 1, 0)

        These paths sort lexicographically in left-to-right DFS order.

        Args:
            agent_id: UUID of the agent.

        Returns:
            Tuple of sibling indices representing path from root.
        """
        agent = self._agent_cache.get(agent_id)
        if agent is None:
            return (0,)

        # Build path by walking up to root
        path_parts: list[int] = []
        current_id = agent_id

        while True:
            current = self._agent_cache.get(current_id)
            if current is None:
                break

            parent_id = current.parent_id
            if parent_id is None:
                # This is the root
                path_parts.append(0)
                break

            # Find this agent's sibling index in parent's children
            parent_children = self._children_cache.get(parent_id, [])
            sibling_index = 0
            for child_id, idx in parent_children:
                if child_id == current_id:
                    sibling_index = idx
                    break

            path_parts.append(sibling_index)
            current_id = parent_id

        # Reverse to get root-to-leaf path
        path_parts.reverse()
        return tuple(path_parts)

    async def _is_subtree_complete(self, agent_id: UUID) -> bool:
        """Check if an agent and its entire subtree are in terminal state.

        An agent's subtree is complete if:
        - The agent itself is in terminal state (COMPLETED or FAILED), AND
        - All of its children's subtrees are also complete

        Args:
            agent_id: UUID of the agent to check.

        Returns:
            True if the entire subtree is complete.
        """
        agent = self._agent_cache.get(agent_id)
        if agent is None:
            # Agent not found, consider it complete
            return True

        # If agent is not in terminal state, subtree is not complete
        if not agent.is_terminal():
            return False

        # Check all children recursively
        children = self._children_cache.get(agent_id, [])
        for child_id, _ in children:
            if not await self._is_subtree_complete(child_id):
                return False

        return True

    async def _has_incomplete_left_siblings(
        self,
        agent_id: UUID,
    ) -> bool:
        """Check if there are incomplete agents to the left of this agent.

        This is the core check for left-to-right execution. It walks up the
        ancestor chain and for EACH ancestor, checks if any left siblings
        (and their subtrees) are incomplete.

        The check ensures that:
        1. All siblings to the left of the current agent are complete
        2. All siblings to the left of the parent are complete
        3. All siblings to the left of the grandparent are complete
        4. ... and so on up to the root

        Args:
            agent_id: UUID of the agent to check.

        Returns:
            True if there are incomplete agents to the left, False otherwise.
        """
        current_id = agent_id

        while True:
            current = self._agent_cache.get(current_id)
            if current is None:
                break

            parent_id = current.parent_id
            if parent_id is None:
                # Reached the root, no more ancestors to check
                break

            # Get parent's children with their sibling indices
            parent_children = self._children_cache.get(parent_id, [])

            # Find current agent's sibling index
            current_sibling_index = -1
            for child_id, idx in parent_children:
                if child_id == current_id:
                    current_sibling_index = idx
                    break

            if current_sibling_index < 0:
                # Should not happen, but handle gracefully
                break

            # Check all siblings to the LEFT (lower sibling index)
            for child_id, idx in parent_children:
                if idx < current_sibling_index:
                    # This is a left sibling - check if its subtree is complete
                    if not await self._is_subtree_complete(child_id):
                        return True

            # Move up to parent and continue checking
            current_id = parent_id

        return False

    async def get_active_agent_ids(
        self,
        root_id: UUID,
        sequential_workers: bool = False,
    ) -> list[UUID]:
        """Return agent IDs not in terminal state, with optional sequential worker filtering.

        When sequential_workers=True, only returns one worker at a time - the leftmost
        eligible worker that has no incomplete work to its left. Non-workers (PENDING,
        BOSS, MANAGER) are always returned as they can execute concurrently.

        Args:
            root_id: Root agent ID to scope the query to.
            sequential_workers: If True, filter workers to only the leftmost eligible.

        Returns:
            List of active agent IDs that can execute.
        """
        # Build hierarchy data
        await self._build_hierarchy_data(root_id)

        # Collect all non-terminal agents
        active_ids: list[UUID] = []
        for agent_id, agent in self._agent_cache.items():
            if not agent.is_terminal():
                active_ids.append(agent_id)

        if not sequential_workers:
            return active_ids

        # Separate workers from non-workers
        workers: list[tuple[UUID, tuple[int, ...]]] = []
        non_workers: list[UUID] = []

        for agent_id in active_ids:
            agent = self._agent_cache.get(agent_id)
            if agent is None:
                continue

            if agent.role == AgentRole.WORKER:
                path = self._compute_hierarchical_path(agent_id)
                workers.append((agent_id, path))
            else:
                non_workers.append(agent_id)

        # Non-workers can always execute (they don't modify workspace)
        result = list(non_workers)

        # Check if any non-worker is still in ANALYZING status (decomposition in progress)
        # Workers should NOT execute until ALL decomposition is complete
        decomposition_in_progress = False
        for agent_id in non_workers:
            agent = self._agent_cache.get(agent_id)
            if agent and agent.status.value == "analyzing":
                decomposition_in_progress = True
                break

        # If decomposition is still happening, don't return any workers yet
        if decomposition_in_progress:
            return result

        # Sort workers by hierarchical path (lexicographic = left-to-right)
        workers.sort(key=lambda x: x[1])

        # Find the first worker that has no incomplete left siblings
        for worker_id, _ in workers:
            has_incomplete_left = await self._has_incomplete_left_siblings(worker_id)
            if not has_incomplete_left:
                # This worker is eligible to execute
                result.append(worker_id)
                break  # Only one worker at a time

        return result

    async def get_all_agent_ids(self, root_id: UUID) -> set[UUID]:
        """Return all agent IDs in the hierarchy.

        Args:
            root_id: Root agent ID to scope the query to.

        Returns:
            Set of all agent IDs in the hierarchy.
        """
        await self._build_hierarchy_data(root_id)
        return set(self._agent_cache.keys())

    async def get_agents_status(self, root_id: UUID) -> list[dict]:
        """Return status information for all agents in the hierarchy.

        Args:
            root_id: Root agent ID to scope the query to.

        Returns:
            List of dicts with agent status information.
        """
        await self._build_hierarchy_data(root_id)

        agents_status = []
        for agent_id, agent in self._agent_cache.items():
            path = self._compute_hierarchical_path(agent_id)
            agents_status.append({
                "agent_id": str(agent_id)[:8],
                "role": agent.role.value,
                "status": agent.status.value,
                "budget": agent.current_budget,
                "path": path,
                "task": (agent.task_description[:40] + "...")
                if agent.task_description and len(agent.task_description) > 40
                else agent.task_description,
                "queue_size": len(agent.task_queue),
            })

        # Sort by hierarchical path for consistent ordering
        agents_status.sort(key=lambda x: x["path"])
        return agents_status
