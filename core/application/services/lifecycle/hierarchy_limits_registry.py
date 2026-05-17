"""Registry for hierarchy limits across an execution tree."""

from __future__ import annotations

from uuid import UUID

from core.domain.values.limits import HierarchyLimits


class HierarchyLimitsRegistry:
    """Track and propagate hierarchy limits from parent to child agents."""

    def __init__(self) -> None:
        self._limits: dict[UUID, HierarchyLimits] = {}
        self._used_roles: dict[UUID, set[str]] = {}  # root_id → role prefixes
        self._parent_children: dict[UUID, set[UUID]] = {}  # parent_id → child_ids
        self._agent_role_prefix: dict[UUID, str] = {}  # agent_id → role prefix

    def reset(self) -> None:
        """Clear all limits for a new run."""
        self._limits.clear()
        self._used_roles.clear()
        self._parent_children.clear()
        self._agent_role_prefix.clear()

    def create_root(
        self,
        root_id: UUID,
        max_depth: int,
        max_children_per_node: int,
        max_retries: int,
        max_total_agents: int = -1,
        domain_context: object | None = None,
    ) -> HierarchyLimits:
        limits = HierarchyLimits.create_root(
            root_id=root_id,
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_retries=max_retries,
            max_total_agents=max_total_agents,
            domain_context=domain_context,
        )
        self._limits[root_id] = limits
        return limits

    def get(self, agent_id: UUID) -> HierarchyLimits | None:
        return self._limits.get(agent_id)

    def set(self, agent_id: UUID, limits: HierarchyLimits) -> None:
        self._limits[agent_id] = limits

    def propagate_to_child(self, parent_id: UUID, child_id: UUID) -> HierarchyLimits | None:
        parent_limits = self._limits.get(parent_id)
        if parent_limits is None:
            return None
        child_limits = parent_limits.for_child()
        self._limits[child_id] = child_limits
        return child_limits

    def get_root_id(self, agent_id: UUID) -> UUID:
        limits = self._limits.get(agent_id)
        if limits is None:
            raise KeyError(f"No hierarchy limits for agent {agent_id}")
        return limits.root_id

    # ------------------------------------------------------------------
    # Cross-tree role dedup tracking
    # ------------------------------------------------------------------

    def register_role(self, agent_id: UUID, role_prefix: str, parent_id: UUID | None = None) -> None:
        limits = self._limits.get(agent_id)
        root_id = limits.root_id if limits else agent_id
        self._used_roles.setdefault(root_id, set()).add(role_prefix)
        self._agent_role_prefix[agent_id] = role_prefix
        if parent_id is not None:
            self._parent_children.setdefault(parent_id, set()).add(agent_id)

    def get_sibling_role_prefixes(self, agent_id: UUID, parent_id: UUID) -> set[str]:
        """Get [Role-Name] prefixes of an agent's siblings (same parent)."""
        children = self._parent_children.get(parent_id, set())
        return {
            self._agent_role_prefix[cid]
            for cid in children
            if cid != agent_id and cid in self._agent_role_prefix
        }

    def get_tree_role_prefixes(self, agent_id: UUID) -> set[str]:
        limits = self._limits.get(agent_id)
        root_id = limits.root_id if limits else agent_id
        return set(self._used_roles.get(root_id, set()))
