"""Hierarchy tree builder for agent hierarchies.

Builds a complete tree structure from projected agent data.
"""

from dataclasses import dataclass
from uuid import UUID

from core.query.projections.models import AgentListItem


@dataclass
class AgentNode:
    """Node in the agent hierarchy tree."""

    id: UUID
    role: str
    status: str
    task_description: str
    parent_id: UUID | None
    children: list["AgentNode"]


@dataclass
class HierarchyResult:
    """Result of hierarchy building."""

    root: AgentNode
    total_agents: int
    depth: int


class HierarchyBuilder:
    """Builds agent hierarchy trees from projected data.

    Takes pre-projected agent data (AgentListItem) and builds
    a complete tree structure via BFS traversal.

    This separates the tree-building logic from the API route,
    making it testable and reusable.
    """

    def __init__(self, agents_by_id: dict[UUID, AgentListItem]) -> None:
        """Initialize with projected agent data.

        Args:
            agents_by_id: Dict mapping agent IDs to their projected data.
        """
        self._agents_by_id = agents_by_id

    def build(self, root_id: UUID) -> HierarchyResult:
        """Build complete hierarchy tree from root agent.

        Args:
            root_id: ID of the root agent (typically BOSS).

        Returns:
            HierarchyResult with root node, total count, and depth.

        Raises:
            KeyError: If root_id is not in agents_by_id.
        """
        if root_id not in self._agents_by_id:
            raise KeyError(f"Agent {root_id} not found")

        # BFS to find all agents in hierarchy and calculate depth
        hierarchy_agents, max_depth = self._traverse_hierarchy(root_id)

        # Build tree recursively
        root_node = self._build_node(root_id)

        return HierarchyResult(
            root=root_node,
            total_agents=len(hierarchy_agents),
            depth=max_depth,
        )

    def _traverse_hierarchy(self, root_id: UUID) -> tuple[set[UUID], int]:
        """BFS traversal to find all agents and max depth.

        Args:
            root_id: Starting agent ID.

        Returns:
            Tuple of (set of all agent IDs, max depth).
        """
        hierarchy_agents: set[UUID] = set()
        queue: list[UUID] = [root_id]
        max_depth = 0
        depth_map: dict[UUID, int] = {root_id: 0}

        while queue:
            current_id = queue.pop(0)

            if current_id in hierarchy_agents:
                continue
            hierarchy_agents.add(current_id)

            if current_id in self._agents_by_id:
                agent = self._agents_by_id[current_id]
                current_depth = depth_map.get(current_id, 0)
                max_depth = max(max_depth, current_depth)

                for child_id in agent.child_ids:
                    if child_id not in hierarchy_agents and child_id in self._agents_by_id:
                        queue.append(child_id)
                        depth_map[child_id] = current_depth + 1

        return hierarchy_agents, max_depth

    def _build_node(self, agent_id: UUID) -> AgentNode:
        """Recursively build tree node.

        Args:
            agent_id: ID of agent to build node for.

        Returns:
            AgentNode with all descendants.
        """
        agent = self._agents_by_id[agent_id]
        children = [
            self._build_node(child_id)
            for child_id in agent.child_ids
            if child_id in self._agents_by_id
        ]

        return AgentNode(
            id=agent_id,
            role=agent.role,
            status=agent.status,
            task_description=agent.task_description or "",
            parent_id=agent.parent_id,
            children=children,
        )
