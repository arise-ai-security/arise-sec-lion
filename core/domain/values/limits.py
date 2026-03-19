"""Hierarchy limits passed down the agent hierarchy.

This immutable value object carries limits and state that propagate from
parent to child agents, enabling depth tracking and limit enforcement.

Limit values of -1 indicate "unlimited" (no limit enforced).

Budget tracking is handled via SharedStore (keyed by root_id).
CVE instance is optionally attached for SEC-bench benchmark runs.
"""

from typing import Self
from uuid import UUID

from pydantic import BaseModel

from core.domain.values.cve_instance import CVEInstance


class HierarchyLimits(BaseModel):
    """Immutable limits passed down agent hierarchy.

    Tracks current position in the hierarchy and enforces limits.
    Each child receives a new context with incremented depth.

    Limit values:
        -1 = unlimited (no enforcement)
        >0 = enforced limit

    The root_id references the SharedStore for this hierarchy.
    CVE instance is optionally attached for SEC-bench benchmark runs.
    """

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    current_depth: int
    max_depth: int  # -1 = unlimited
    max_children_per_node: int  # -1 = unlimited
    max_retries: int
    root_id: UUID  # Reference to SharedStore
    max_total_agents: int = -1  # -1 = unlimited, global limit across hierarchy
    current_total_agents: int = 0  # Snapshot of total agents created so far
    cve_instance: CVEInstance | None = None  # SEC-bench CVE context

    def for_child(self) -> Self:
        """Create limits for child agent (increments depth)."""
        return self.model_copy(update={"current_depth": self.current_depth + 1})

    def is_depth_limited(self) -> bool:
        """Check if depth limit is enabled."""
        return self.max_depth > 0

    def is_children_limited(self) -> bool:
        """Check if children-per-node limit is enabled."""
        return self.max_children_per_node > 0

    def is_total_agents_limited(self) -> bool:
        """Check if total agents limit is enabled."""
        return self.max_total_agents > 0

    def agents_remaining(self) -> int:
        """Return how many more agents can be created.

        Returns -1 if total agents is unlimited.
        """
        if not self.is_total_agents_limited():
            return -1
        return max(0, self.max_total_agents - self.current_total_agents)

    def with_agent_counts(self, current_total: int, max_total: int) -> Self:
        """Create new limits with updated agent counts.

        Used to refresh agent count snapshot before each agent step.
        """
        return self.model_copy(
            update={
                "current_total_agents": current_total,
                "max_total_agents": max_total,
            }
        )

    def can_spawn_child(self) -> bool:
        """Check if current depth allows spawning children.

        Returns True if:
        - Depth limit is disabled (-1), OR
        - Current depth is below max_depth
        """
        if not self.is_depth_limited():
            return True
        return self.current_depth < self.max_depth

    def depth_remaining(self) -> int:
        """Return how many more levels can be spawned.

        Returns -1 if depth is unlimited.
        """
        if not self.is_depth_limited():
            return -1
        return max(0, self.max_depth - self.current_depth)

    def has_cve_context(self) -> bool:
        """Check if this is a CVE-based security benchmark task."""
        return self.cve_instance is not None

    def with_cve_instance(self, cve: CVEInstance) -> Self:
        """Create new limits with CVE instance attached."""
        return self.model_copy(update={"cve_instance": cve})

    @classmethod
    def create_root(
        cls,
        root_id: UUID,
        max_depth: int,
        max_children_per_node: int,
        max_retries: int,
        max_total_agents: int = -1,
        cve_instance: CVEInstance | None = None,
    ) -> Self:
        """Create limits for root (BOSS) agent.

        Args:
            root_id: Root agent ID (references SharedStore)
            max_depth: Maximum hierarchy depth (-1 = unlimited)
            max_children_per_node: Max children per parent (-1 = unlimited)
            max_retries: Max retry attempts
            max_total_agents: Max total agents in hierarchy (-1 = unlimited)
            cve_instance: Optional SEC-bench CVE instance for benchmark runs
        """
        return cls(
            current_depth=0,
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_retries=max_retries,
            root_id=root_id,
            max_total_agents=max_total_agents,
            current_total_agents=1,  # Root agent counts as 1
            cve_instance=cve_instance,
        )
