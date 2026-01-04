"""Parent notification service for agent hierarchy.

Handles notifying parent agents when children complete or fail.
Extracted from ExecutionService to follow Single Responsibility Principle.

Design Choice 5: Also handles knowledge publishing when worker children complete.
The parent thinker reviews the worker's report and publishes curated knowledge
to the SharedExecutionContext for other workers to learn from.

Uses ThinkerReviewAndPublish step for the review/publish logic.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.events.events import DomainEvent
from core.domain.aggregates.agent_session import AgentSession, AgentStatus
from core.domain.values.enums import AgentRole
from core.application.pipeline.steps.knowledge import ThinkerReviewAndPublish

from .agent_repository import AgentNotFoundError, AgentRepository


if TYPE_CHECKING:
    from core.ports.shared_context_port import SharedContextPort


type ProgressCallback = Callable[[DomainEvent, Any], None]


@dataclass
class BudgetRecollectionConfig:
    """Configuration for budget recollection (Design Choice 3)."""

    enabled: bool = False
    success_reward_ratio: float = 1.0
    failure_penalty_ratio: float = 0.0


class ParentNotificationService:
    """Handles notifying parent agents when children complete or fail.

    Single Responsibility: Manage parent-child completion notifications./failure notifications

    Design Choice 5: When a worker child completes successfully, the parent
    thinker reviews and publishes curated knowledge to SharedExecutionContext
    using the ThinkerReviewAndPublish pipeline step.
    """

    def __init__(
        self,
        repository: AgentRepository,
        progress_callback: ProgressCallback | None = None,
        budget_recollection_config: BudgetRecollectionConfig | None = None,
        shared_context_port: "SharedContextPort | None" = None,
        publish_worker_knowledge: bool = True,
    ) -> None:
        """Initialize the parent notification service.

        Args:
            repository: Agent repository for loading and persisting agents.
            progress_callback: Optional callback for progress notifications.
            budget_recollection_config: Config for budget recollection (Design Choice 3).
            shared_context_port: Port for shared context (Design Choice 5).
            publish_worker_knowledge: Whether to publish worker knowledge (Design Choice 5).
        """
        self._repository = repository
        self._progress_callback = progress_callback
        self._budget_config = budget_recollection_config or BudgetRecollectionConfig()
        self._publish_worker_knowledge = publish_worker_knowledge

        # Design Choice 5: Create thinker review step if shared context is available
        self._thinker_review: ThinkerReviewAndPublish | None = None
        if shared_context_port is not None:
            self._thinker_review = ThinkerReviewAndPublish(shared_context_port)

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
        6. Design Choice 5: Publishes worker knowledge to shared context
        7. Recursively notifies grandparent if parent also completed

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

        # Skip if parent is not in WAITING status (already completed or failed)
        # This handles race conditions where:
        # 1. Another child already triggered parent completion
        # 2. A sibling failed and propagated failure to parent
        if parent.status != AgentStatus.WAITING:
            return

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

        # Design Choice 5: Parent thinker publishes worker knowledge to shared context
        # Only publish if child was a successful worker (not manager/boss)
        if (
            self._publish_worker_knowledge
            and child_succeeded
            and child.role == AgentRole.WORKER
            and self._thinker_review is not None
        ):
            root_id = child.hierarchy_limits.root_id if child.hierarchy_limits else None
            if root_id is not None:
                await self._thinker_review.execute(
                    child_agent_id=str(child.agent_id),
                    child_task=child.task_description or "",
                    child_result=child.result or "",
                    child_report=child.pending_worker_report,
                    parent_agent_id=str(parent.agent_id),
                    root_id=str(root_id),
                )

        # Recursively notify grandparent if parent also completed
        # This handles the case where all children completing causes parent to complete
        if parent.status == AgentStatus.COMPLETED:
            await self.notify_if_complete(parent)

    async def notify_if_failed(self, child: AgentSession) -> None:
        """Notify parent when child fails, recursively up the hierarchy.

        This method:
        1. Checks if child is failed and has a parent
        2. Loads the parent agent
        3. Updates parent with child failure (parent also fails)
        4. Persists parent events
        5. Recursively notifies grandparent (failure bubbles up to BOSS)

        Args:
            child: The child agent that has failed.
        """
        if child.status != AgentStatus.FAILED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError:
            raise ValueError(f"Parent agent {child.parent_id} not found")

        # Skip if parent is not in WAITING status (already completed or failed)
        if parent.status != AgentStatus.WAITING:
            return

        parent_version = parent.version

        parent.handle_child_failure(
            child_id=child.agent_id,
            reason=child.error_message or "Unknown error",
        )

        await self._repository.persist_events(
            parent,
            parent_version,
            self._progress_callback,
        )

        # Recursively notify grandparent - failure bubbles up
        if parent.status == AgentStatus.FAILED:
            await self.notify_if_failed(parent)
