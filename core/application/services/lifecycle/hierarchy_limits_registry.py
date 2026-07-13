"""Registry for hierarchy limits across an execution tree."""

from __future__ import annotations

from uuid import UUID

from core.domain.values.limits import HierarchyLimits


class HierarchyLimitsRegistry:
    """Track and propagate hierarchy limits from parent to child agents."""

    def __init__(self) -> None:
        self._limits: dict[UUID, HierarchyLimits] = {}
        self._completed_roles: dict[UUID, set[str]] = {}  # root_id → completed roles
        self._failed_roles: dict[UUID, set[str]] = {}  # root_id → failed roles
        self._parent_children: dict[UUID, set[UUID]] = {}  # parent_id → child_ids
        self._agent_role_prefix: dict[UUID, str] = {}  # agent_id → role prefix
        self._current_child_dags: dict[
            UUID, dict[UUID, tuple[UUID, ...]]
        ] = {}  # parent_id → current child_id → sibling predecessor ids

    def reset(self) -> None:
        """Clear all limits for a new run."""
        self._limits.clear()
        self._completed_roles.clear()
        self._failed_roles.clear()
        self._parent_children.clear()
        self._agent_role_prefix.clear()
        self._current_child_dags.clear()

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
    # Current child-generation dependency tracking
    # ------------------------------------------------------------------

    def replace_child_generation(
        self,
        parent_id: UUID,
        child_dependencies: dict[UUID, tuple[UUID, ...]],
    ) -> None:
        """Replace a parent's active child DAG after an initial plan or re-plan."""
        self._current_child_dags[parent_id] = dict(child_dependencies)
        self._parent_children[parent_id] = set(child_dependencies)

    def expand_to_terminal_sinks(
        self, predecessor_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Resolve grouping predecessors to terminal sinks of their current child DAGs."""
        expanded: list[UUID] = []
        for predecessor_id in predecessor_ids:
            for sink_id in self._terminal_sinks(predecessor_id):
                if sink_id not in expanded:
                    expanded.append(sink_id)
        return tuple(expanded)

    def _terminal_sinks(self, agent_id: UUID) -> tuple[UUID, ...]:
        child_dag = self._current_child_dags.get(agent_id)
        if not child_dag:
            return (agent_id,)
        depended_on = {
            dependency_id
            for dependencies in child_dag.values()
            for dependency_id in dependencies
            if dependency_id in child_dag
        }
        sinks = tuple(child_id for child_id in child_dag if child_id not in depended_on)
        return tuple(
            terminal_id
            for sink_id in sinks
            for terminal_id in self._terminal_sinks(sink_id)
        )

    # ------------------------------------------------------------------
    # Cross-tree role dedup tracking (outcome-aware)
    # ------------------------------------------------------------------

    def register_role(
        self, agent_id: UUID, role_prefix: str, parent_id: UUID | None = None
    ) -> None:
        self._agent_role_prefix[agent_id] = role_prefix
        if parent_id is not None:
            self._parent_children.setdefault(parent_id, set()).add(agent_id)

    def mark_role_completed(self, agent_id: UUID) -> None:
        """Record that this agent's role finished successfully (must not respawn)."""
        role = self._agent_role_prefix.get(agent_id)
        if role is None:
            return
        root_id = self._root_for(agent_id)
        self._completed_roles.setdefault(root_id, set()).add(role)
        failed = self._failed_roles.get(root_id)
        if failed is not None:
            failed.discard(role)

    def mark_role_failed(self, agent_id: UUID) -> None:
        """Record that this agent's role failed (may be reissued on re-decomposition)."""
        role = self._agent_role_prefix.get(agent_id)
        if role is None:
            return
        root_id = self._root_for(agent_id)
        # A later completion of a reissued role already cleared failure; only mark
        # failed when not already completed.
        if role in self._completed_roles.get(root_id, set()):
            return
        self._failed_roles.setdefault(root_id, set()).add(role)

    def get_sibling_role_prefixes(self, agent_id: UUID, parent_id: UUID) -> set[str]:
        """Get [Role-Name] prefixes of an agent's siblings (same parent)."""
        children = self._parent_children.get(parent_id, set())
        return {
            self._agent_role_prefix[cid]
            for cid in children
            if cid != agent_id and cid in self._agent_role_prefix
        }

    def get_tree_role_prefixes(self, agent_id: UUID) -> set[str]:
        """Get role prefixes used elsewhere in the same execution tree."""
        root_id = self._root_for(agent_id)
        return {
            role_prefix
            for other_id, role_prefix in self._agent_role_prefix.items()
            if other_id != agent_id and self._root_for(other_id) == root_id
        }

    def get_completed_role_prefixes(self, agent_id: UUID) -> set[str]:
        root_id = self._root_for(agent_id)
        return set(self._completed_roles.get(root_id, set()))

    def get_failed_role_prefixes(self, agent_id: UUID) -> set[str]:
        root_id = self._root_for(agent_id)
        return set(self._failed_roles.get(root_id, set()))

    def _root_for(self, agent_id: UUID) -> UUID:
        limits = self._limits.get(agent_id)
        return limits.root_id if limits else agent_id
