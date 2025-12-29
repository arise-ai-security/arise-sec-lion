"""Execution context passed down the agent hierarchy.

This immutable value object carries limits and state that propagate from
parent to child agents, enabling depth tracking and limit enforcement.

Limit values of -1 indicate "unlimited" (no limit enforced).

Budget tracking is handled via SharedExecutionContext (keyed by root_id).
CVE instance is optionally attached for SEC-bench benchmark runs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.cve_instance import CVEInstance


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Immutable context passed down agent hierarchy.

    Tracks current position in the hierarchy and enforces limits.
    Each child receives a new context with incremented depth.

    Limit values:
        -1 = unlimited (no enforcement)
        >0 = enforced limit

    The root_id references the SharedExecutionContext for this hierarchy.
    CVE instance is optionally attached for SEC-bench benchmark runs.
    """

    current_depth: int
    max_depth: int  # -1 = unlimited
    max_children_per_node: int  # -1 = unlimited
    max_retries: int
    root_id: UUID  # Reference to SharedExecutionContext
    cve_instance: CVEInstance | None = None  # SEC-bench CVE context

    def for_child(self) -> ExecutionContext:
        """Create context for child agent (increments depth)."""
        return replace(self, current_depth=self.current_depth + 1)

    def is_depth_limited(self) -> bool:
        """Check if depth limit is enabled."""
        return self.max_depth > 0

    def is_children_limited(self) -> bool:
        """Check if children-per-node limit is enabled."""
        return self.max_children_per_node > 0

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

    def with_cve_instance(self, cve: CVEInstance) -> ExecutionContext:
        """Create new context with CVE instance attached."""
        return replace(self, cve_instance=cve)

    @classmethod
    def create_root(
        cls,
        root_id: UUID,
        max_depth: int,
        max_children_per_node: int,
        max_retries: int,
        cve_instance: CVEInstance | None = None,
    ) -> ExecutionContext:
        """Create context for root (BOSS) agent.

        Args:
            root_id: Root agent ID (references SharedExecutionContext)
            max_depth: Maximum hierarchy depth (-1 = unlimited)
            max_children_per_node: Max children per parent (-1 = unlimited)
            max_retries: Max retry attempts
            cve_instance: Optional SEC-bench CVE instance for benchmark runs
        """
        return cls(
            current_depth=0,
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_retries=max_retries,
            root_id=root_id,
            cve_instance=cve_instance,
        )
