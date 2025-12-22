"""Child agent factory for creating agents from ChildSpawned events.

Handles child agent creation with execution context propagation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.context import ParentContext
from core.domain.events import ChildSpawned
from core.domain.model import AgentRole, AgentSession


if TYPE_CHECKING:
    from core.application.services.agent_repository import AgentRepository
    from core.application.services.context_registry import ExecutionContextRegistry


@dataclass
class ChildCreationResult:
    """Result of child agent creation."""

    agent: AgentSession
    created: bool  # False if limit was reached


class ChildAgentFactory:
    """Factory for creating child agents from ChildSpawned events.

    Single Responsibility: Create child agents with proper context propagation.

    Enforces:
    - max_total_agents limit
    - Execution context propagation to children
    """

    def __init__(
        self,
        repository: AgentRepository,
        context_registry: ExecutionContextRegistry,
        max_total_agents: int = -1,  # -1 means unlimited
    ) -> None:
        self._repository = repository
        self._context_registry = context_registry
        self._max_total_agents = max_total_agents
        self._total_created: int = 0

    def reset(self, initial_count: int = 0) -> None:
        """Reset factory state for a new run."""
        self._total_created = initial_count

    @property
    def total_created(self) -> int:
        """Get total number of agents created."""
        return self._total_created

    def is_at_limit(self) -> bool:
        """Check if we've reached the agent creation limit."""
        if self._max_total_agents < 0:
            return False
        return self._total_created >= self._max_total_agents

    async def create_from_event(
        self,
        event: ChildSpawned,
        parent_id: UUID,
    ) -> ChildCreationResult | None:
        """Create a child agent from a ChildSpawned event.

        Returns None if at limit, otherwise returns the created agent.
        Propagates execution context and parent context to child.
        """
        if self.is_at_limit():
            return None

        child = AgentSession.create(
            session_id=event.child_id,
            role=AgentRole(event.child_role),
            config=event.child_config,
            parent_id=parent_id,
        )
        child.assign_task(event.subtask.description)

        # Propagate execution context
        self._context_registry.propagate_to_child(parent_id, event.child_id)

        # Set parent context if available in event
        if event.parent_context:
            parent_context = ParentContext.from_dict(event.parent_context)
            child.set_parent_context(parent_context)

        # Persist the new agent
        await self._repository.save_new_agent(child)
        self._total_created += 1

        return ChildCreationResult(agent=child, created=True)

    async def create_children_from_events(
        self,
        events: list[ChildSpawned],
        parent_id: UUID,
    ) -> list[ChildCreationResult]:
        """Create multiple child agents from ChildSpawned events.

        Returns list of creation results. Some may be None if limit reached.
        """
        results = []
        for event in events:
            result = await self.create_from_event(event, parent_id)
            if result:
                results.append(result)
        return results
