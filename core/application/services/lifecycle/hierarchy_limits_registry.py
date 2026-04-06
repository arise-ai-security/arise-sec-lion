"""Registry for hierarchy limits across an execution tree."""

from uuid import UUID

from core.domain.values.limits import HierarchyLimits


class HierarchyLimitsRegistry:
    """Track and propagate hierarchy limits from parent to child agents."""

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
        max_total_agents: int = -1,
        domain_context: object | None = None,
    ) -> HierarchyLimits:
        """Create and register root hierarchy limits."""
        limits = HierarchyLimits.create_root(
            root_id=root_id,
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_total_agents=max_total_agents,
            domain_context=domain_context,
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
        """Propagate hierarchy limits from parent to child with incremented depth."""
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
        """Get root ID for an agent's hierarchy limits."""
        limits = self._limits.get(agent_id)
        if limits is None:
            raise KeyError(f"No hierarchy limits for agent {agent_id}")
        return limits.root_id
