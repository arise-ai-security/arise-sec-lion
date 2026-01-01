"""Parent notification service for agent hierarchy.

Handles notifying parent agents when children complete their execution.
Extracted from ExecutionService to follow Single Responsibility Principle.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.events.events import DomainEvent
from core.domain.aggregates.agent_session import AgentSession, AgentStatus

from .agent_repository import AgentNotFoundError, AgentRepository


if TYPE_CHECKING:
    pass


type ProgressCallback = Callable[[DomainEvent, Any], None]


@dataclass
class BudgetRecollectionConfig:
    """Configuration for budget recollection (Design Choice 3)."""

    enabled: bool = False
    success_reward_ratio: float = 1.0
    failure_penalty_ratio: float = 0.0


class ParentNotificationService:
    """Handles notifying parent agents when children complete.

    Single Responsibility: Manage parent-child completion notifications.
    """

    def __init__(
        self,
        repository: AgentRepository,
        progress_callback: ProgressCallback | None = None,
        budget_recollection_config: BudgetRecollectionConfig | None = None,
    ) -> None:
        """Initialize the parent notification service.

        Args:
            repository: Agent repository for loading and persisting agents.
            progress_callback: Optional callback for progress notifications.
            budget_recollection_config: Config for budget recollection (Design Choice 3).
        """
        self._repository = repository
        self._progress_callback = progress_callback
        self._budget_config = budget_recollection_config or BudgetRecollectionConfig()

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set the progress callback for event notifications."""
        self._progress_callback = callback

    async def notify_if_complete(self, child: AgentSession) -> None:
        """Notify parent when child completes, recursively up the hierarchy.

        This method:
        1. Checks if child is completed/failed and has a parent
        2. Loads the parent agent
        3. Builds structured child result
        4. Updates parent with child completion (with budget recollection)
        5. Persists parent events
        6. Recursively notifies grandparent if parent also completed

        Args:
            child: The child agent that may have completed.
        """
        # Handle both successful completion and failure
        if child.status not in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            return
        if child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError:
            raise ValueError(f"Parent agent {child.parent_id} not found")

        parent_version = parent.version

        # Build structured task outcome
        task_outcome = child.build_task_outcome()

        # Determine success for budget recollection (Design Choice 3)
        child_succeeded = child.status == AgentStatus.COMPLETED

        parent.handle_child_update(
            child_id=child.agent_id,
            result=child.result or child.error_message or "",
            task_outcome=task_outcome,
            success=child_succeeded,
            budget_success_reward_ratio=self._budget_config.success_reward_ratio,
            budget_failure_penalty_ratio=self._budget_config.failure_penalty_ratio,
        )

        await self._repository.persist_events(
            parent,
            parent_version,
            self._progress_callback,
        )

        # Recursively notify grandparent if parent also completed
        # This handles the case where all children completing causes parent to complete
        if parent.status == AgentStatus.COMPLETED:
            await self.notify_if_complete(parent)
