"""Execution context registry for managing agent execution contexts.

Tracks execution context for each agent in the hierarchy.
"""

from __future__ import annotations

from uuid import UUID

from core.domain.execution_context import ExecutionContext


class ExecutionContextRegistry:
    """Registry for managing execution contexts in agent hierarchy.

    Single Responsibility: Track and propagate execution contexts.

    Provides:
    - Root context creation with system limits
    - Context propagation from parent to child
    - Context lookup by agent ID
    """

    def __init__(self) -> None:
        self._contexts: dict[UUID, ExecutionContext] = {}

    def reset(self) -> None:
        """Clear all contexts for a new run."""
        self._contexts.clear()

    def create_root(
        self,
        root_id: UUID,
        max_depth: int,
        max_children_per_node: int,
        max_retries: int,
    ) -> ExecutionContext:
        """Create and register root execution context.

        Args:
            root_id: The root agent ID
            max_depth: Maximum depth limit (-1 for unlimited)
            max_children_per_node: Maximum children per node (-1 for unlimited)
            max_retries: Maximum retry attempts
        """
        context = ExecutionContext.create_root(
            max_depth=max_depth,
            max_children_per_node=max_children_per_node,
            max_retries=max_retries,
        )
        self._contexts[root_id] = context
        return context

    def get(self, agent_id: UUID) -> ExecutionContext | None:
        """Get execution context for an agent."""
        return self._contexts.get(agent_id)

    def set(self, agent_id: UUID, context: ExecutionContext) -> None:
        """Set execution context for an agent."""
        self._contexts[agent_id] = context

    def propagate_to_child(self, parent_id: UUID, child_id: UUID) -> ExecutionContext | None:
        """Propagate execution context from parent to child.

        Creates child context with incremented depth.
        Returns the child context, or None if parent has no context.
        """
        parent_context = self._contexts.get(parent_id)
        if parent_context is None:
            return None

        child_context = parent_context.for_child()
        self._contexts[child_id] = child_context
        return child_context

    def has_context(self, agent_id: UUID) -> bool:
        """Check if agent has a registered execution context."""
        return agent_id in self._contexts

    def __len__(self) -> int:
        """Return number of registered contexts."""
        return len(self._contexts)
