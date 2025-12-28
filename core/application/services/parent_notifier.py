"""Parent notification service for agent hierarchy.

Handles notifying parent agents when children complete their execution.
Extracted from ExecutionService to follow Single Responsibility Principle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.domain.events import DomainEvent
from core.domain.model import AgentSession, AgentStatus

from .agent_repository import AgentNotFoundError, AgentRepository


if TYPE_CHECKING:
    pass


ProgressCallback = Callable[[DomainEvent, Any], None]


class ParentNotificationService:
    """Handles notifying parent agents when children complete.

    Single Responsibility: Manage parent-child completion notifications.
    """

    def __init__(
        self,
        repository: AgentRepository,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Initialize the parent notification service.

        Args:
            repository: Agent repository for loading and persisting agents.
            progress_callback: Optional callback for progress notifications.
        """
        self._repository = repository
        self._progress_callback = progress_callback

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set the progress callback for event notifications."""
        self._progress_callback = callback

    async def notify_if_complete(self, child: AgentSession) -> None:
        """Notify parent when child completes.

        This method:
        1. Checks if child is completed and has a parent
        2. Loads the parent agent
        3. Builds structured child result
        4. Updates parent with child completion
        5. Persists parent events

        Args:
            child: The child agent that may have completed.
        """
        if child.status != AgentStatus.COMPLETED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError:
            raise ValueError(f"Parent agent {child.parent_id} not found")

        parent_version = parent.version

        # Build structured child result
        child_result = child.build_child_result()

        parent.handle_child_update(
            child_id=child.agent_id,
            result=child.result or "",
            child_result=child_result,
        )

        await self._repository.persist_events(
            parent,
            parent_version,
            self._progress_callback,
        )
