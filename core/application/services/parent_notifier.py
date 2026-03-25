"""Parent notification service for agent hierarchy.

Handles notifying parent agents when children complete or fail.
Extracted from ExecutionService to follow Single Responsibility Principle.
"""

from core.application.types import ProgressCallback
from core.domain.aggregates.agent_session import AgentSession, AgentStatus

from .agent_repository import AgentNotFoundError, AgentRepository


class ParentNotificationService:
    """Handles notifying parent agents when children complete or fail.

    Single Responsibility: Manage parent-child completion/failure notifications.
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
        """Notify parent when child completes, recursively up the hierarchy.

        This method:
        1. Checks if child is completed and has a parent
        2. Loads the parent agent
        3. Builds structured child result
        4. Updates parent with child completion
        5. Persists parent events
        6. Recursively notifies grandparent if parent also completed

        Args:
            child: The child agent that may have completed.
        """
        if child.status != AgentStatus.COMPLETED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError as e:
            raise ValueError(f"Parent agent {child.parent_id} not found") from e

        # Skip if parent is not in WAITING status (already completed or failed)
        # This handles race conditions where:
        # 1. Another child already triggered parent completion
        # 2. A sibling failed and propagated failure to parent
        if parent.status != AgentStatus.WAITING:
            return

        parent_version = parent.version

        # Build structured report
        report = child.build_report()

        parent.handle_child_update(
            child_id=child.agent_id,
            result=child.result or "",
            report=report,
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

    async def notify_if_failed(self, child: AgentSession) -> None:
        """Notify parent when child fails, recursively up the hierarchy.

        If child failure is infeasible (DecisionInfeasible), triggers parent
        re-decomposition instead of propagating failure.
        Otherwise, parent fails and failure bubbles up to BOSS.
        """
        if child.status != AgentStatus.FAILED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError as e:
            raise ValueError(f"Parent agent {child.parent_id} not found") from e

        if parent.status != AgentStatus.WAITING:
            return

        parent_version = parent.version

        # Check if failure is infeasible → trigger re-decomposition
        error = child.error_message or ""
        if error.startswith("Infeasible:"):
            parent.trigger_redecomposition(
                trigger_child_id=child.agent_id,
                reason=error,
            )
            await self._repository.persist_events(
                parent, parent_version, self._progress_callback
            )
            # Parent is now ANALYZING — system loop will re-decompose it
            return

        parent.handle_child_failure(
            child_id=child.agent_id,
            reason=error,
        )

        await self._repository.persist_events(
            parent,
            parent_version,
            self._progress_callback,
        )

        if parent.status == AgentStatus.FAILED:
            await self.notify_if_failed(parent)
