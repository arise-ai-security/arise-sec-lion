"""Hierarchy limits registry for managing agent execution limits.

Tracks hierarchy limits for each agent in the hierarchy.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.values.context import HierarchyLimits

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance


class HierarchyLimitsRegistry:
    """Registry for managing hierarchy limits in agent hierarchy.

    Single Responsibility: Track and propagate hierarchy limits.

    Provides:
    - Root limits creation with system limits
    - Limits propagation from parent to child
    - Limits lookup by agent ID
    """

    def __init__(self) -> None:
        self._limits: dict[UUID, HierarchyLimits] = {}

    def reset(self) -> None:
        """Clear all limits for a new run."""
        self._limits.clear()

    def create_root(
        self,
        root_id: UUID,
        max_depth: int,
        max_children_per_node: int,
        max_retries: int,
        max_total_agents: int = -1,
        cve_instance: "CVEInstance | None" = None,
    ) -> HierarchyLimits:
        """Create and register root hierarchy limits.

        Args:
            root_id: The root agent ID (also used for SharedExecutionContext reference)
            max_depth: Maximum depth limit (-1 for unlimited)
            max_children_per_node: Maximum children per node (-1 for unlimited)
            max_retries: Maximum retry attempts
            max_total_agents: Maximum total agents in hierarchy (-1 for unlimited)
            cve_instance: Optional SEC-bench CVE instance for benchmark runs
        """
        limits = HierarchyLimits.create_root(
            root_id=root_id,
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_retries=max_retries,
            max_total_agents=max_total_agents,
            cve_instance=cve_instance,
        )
        self._limits[root_id] = limits
        return limits

    def get(self, agent_id: UUID) -> HierarchyLimits | None:
        """Get hierarchy limits for an agent."""
        return self._limits.get(agent_id)

    def set(self, agent_id: UUID, limits: HierarchyLimits) -> None:
        """Set hierarchy limits for an agent."""
        self._limits[agent_id] = limits

    def propagate_to_child(self, parent_id: UUID, child_id: UUID) -> HierarchyLimits | None:
        """Propagate hierarchy limits from parent to child.

        Creates child limits with incremented depth.
        Returns the child limits, or None if parent has no limits.
        """
        parent_limits = self._limits.get(parent_id)
        if parent_limits is None:
            return None

        child_limits = parent_limits.for_child()
        self._limits[child_id] = child_limits
        return child_limits

    def has_limits(self, agent_id: UUID) -> bool:
        """Check if agent has registered hierarchy limits."""
        return agent_id in self._limits

    def get_root_id(self, agent_id: UUID) -> UUID:
        """Get root ID for an agent's hierarchy limits.

        Args:
            agent_id: The agent to get root ID for.

        Returns:
            The root_id from the agent's HierarchyLimits.

        Raises:
            KeyError: If agent has no registered limits.
        """
        limits = self._limits.get(agent_id)
        if limits is None:
            raise KeyError(f"No hierarchy limits for agent {agent_id}")
        return limits.root_id

    def __len__(self) -> int:
        """Return number of registered limits."""
        return len(self._limits)
