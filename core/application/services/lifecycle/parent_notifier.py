"""Parent notification service for agent hierarchy.

Handles notifying parent agents when children complete or fail.
Extracted from ExecutionService to follow Single Responsibility Principle.
"""

import re

from core.application.types import ProgressCallback
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus

from .agent_repository import AgentNotFoundError, AgentRepository


class ParentNotificationService:
    """Handles notifying parent agents when children complete or fail.

    Single Responsibility: Manage parent-child completion/failure notifications.
    """

    def __init__(
        self,
        repository: AgentRepository,
        max_redecompositions: int = 2,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Initialize the parent notification service.

        Args:
            repository: Agent repository for loading and persisting agents.
            max_redecompositions: Max infeasibility-driven redecompositions per parent.
            progress_callback: Optional callback for progress notifications.
        """
        self._repository = repository
        self._max_redecompositions = max_redecompositions
        self._progress_callback = progress_callback

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    async def notify_if_complete(self, child: AgentSession) -> None:
        """Notify parent when child completes, recursively up the hierarchy.

        Branches on parent status so that an OCC retry whose sibling has
        already driven the parent to terminal does not drop the upward
        propagation chain:

        - WAITING: normal path (emit, persist, recurse upward).
        - COMPLETED: walk further upward toward the grandparent. The
          aggregate's idempotence guard (handle_child_update) makes the
          grandparent re-emission safe.
        - FAILED: no-op; failure already propagated via the failure path.
        - Other: no-op.
        """
        if child.status != AgentStatus.COMPLETED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError as e:
            raise ValueError(f"Parent agent {child.parent_id} not found") from e

        if parent.status == AgentStatus.WAITING:
            report = child.build_report()

            parent.handle_child_update(
                child_id=child.agent_id,
                result=child.result or "",
                report=report,
                max_redecompositions=self._redecomposition_limit(parent),
            )
            self._record_terminal_phase_gate(parent, child)

            await self._repository.persist_events(
                parent,
                self._progress_callback,
            )

            # Recursively notify grandparent if parent also completed
            if parent.status == AgentStatus.COMPLETED:
                await self.notify_if_complete(parent)
            return

        if parent.status == AgentStatus.COMPLETED:
            # Sibling already drove parent terminal; walk upward so the
            # grandparent sees the completion chain. Aggregate idempotence
            # protects against duplicate ChildCompleted emissions.
            await self.notify_if_complete(parent)
            return

        # FAILED or any other non-WAITING status: drop the upward
        # propagation here — failure has its own propagation path.
        return

    async def notify_if_failed(self, child: AgentSession) -> None:
        """Notify parent when child fails, recursively up the hierarchy.

        Branches on parent status so a sibling that already drove the
        parent terminal does not silently drop the failure chain:

        - WAITING: normal path (decide infeasible re-decomposition or
          propagate failure).
        - FAILED: walk further upward toward the grandparent.
        - COMPLETED: partial-success case — walk upward via the
          completion path so the grandparent sees the completion chain.
        - ANALYZING (re-decompose in flight) or other: no-op.

        If child failure is infeasible (DecisionInfeasible), a Manager may
        re-decompose instead of propagating failure. Otherwise the failure, its
        task, and its digest are recorded on the parent. When every required
        Manager child has failed and budget remains, that Manager re-decomposes
        informed by those records; non-Manager parents fail upward.
        """
        if child.status != AgentStatus.FAILED or child.parent_id is None:
            return

        try:
            parent = await self._repository.load(child.parent_id)
        except AgentNotFoundError as e:
            raise ValueError(f"Parent agent {child.parent_id} not found") from e

        if parent.status == AgentStatus.FAILED:
            # Sibling already drove parent terminal; walk upward.
            await self.notify_if_failed(parent)
            return

        if parent.status == AgentStatus.COMPLETED:
            # Partial success: sibling completed the parent already.
            # Walk upward via the completion path.
            await self.notify_if_complete(parent)
            return

        if parent.status != AgentStatus.WAITING:
            # ANALYZING (re-decompose in flight) or unexpected state.
            return

        redecomposition_limit = self._redecomposition_limit(parent)

        # Check if failure is infeasible → trigger re-decomposition
        error = child.error_message or ""
        if error.startswith("Infeasible:"):
            if parent.redecomposition_count < redecomposition_limit:
                parent.handle_child_failure(
                    child_id=child.agent_id,
                    reason=error,
                    child_task=child.task_description,
                    digest=child.failure_digest,
                    max_redecompositions=redecomposition_limit,
                )
                if parent.status == AgentStatus.WAITING:
                    parent.trigger_redecomposition(
                        trigger_child_id=child.agent_id,
                        reason=error,
                    )
                await self._repository.persist_events(
                    parent, self._progress_callback
                )
                # Parent is now ANALYZING — system loop will re-decompose it
                return

            parent.handle_child_failure(
                child_id=child.agent_id,
                reason=(
                    "Redecomposition limit reached "
                    f"({redecomposition_limit}): {error}"
                ),
                child_task=child.task_description,
                digest=child.failure_digest,
                max_redecompositions=redecomposition_limit,
            )
            await self._repository.persist_events(
                parent, self._progress_callback
            )
            if parent.status == AgentStatus.FAILED:
                await self.notify_if_failed(parent)
            elif parent.status == AgentStatus.COMPLETED:
                await self.notify_if_complete(parent)
            return

        parent.handle_child_failure(
            child_id=child.agent_id,
            reason=error,
            child_task=child.task_description,
            digest=child.failure_digest,
            max_redecompositions=redecomposition_limit,
        )
        self._record_terminal_phase_gate(parent, child)

        await self._repository.persist_events(
            parent,
            self._progress_callback,
        )

        if parent.status == AgentStatus.FAILED:
            await self.notify_if_failed(parent)
        elif parent.status == AgentStatus.COMPLETED:
            # Partial success: some children succeeded, parent completed with
            # aggregated results. Notify grandparent of completion.
            await self.notify_if_complete(parent)

    def _redecomposition_limit(self, parent: AgentSession) -> int:
        """Only Managers may semantically re-plan after child failure."""
        if parent.role == AgentRole.MANAGER:
            return self._max_redecompositions
        return 0

    @staticmethod
    def _record_terminal_phase_gate(parent: AgentSession, child: AgentSession) -> None:
        # Only a terminal phase outcome gates. A parent that is still WAITING or
        # re-decomposing (ANALYZING) has no settled verdict yet, so recording a
        # gate there would emit a spurious passed=False on every replan.
        if not parent.is_terminal():
            return
        match = re.match(r"^\s*\[([^\]]+)\]", parent.task_description)
        if match is None:
            return
        parent.record_phase_gate(
            phase=match.group(1),
            passed=parent.status == AgentStatus.COMPLETED,
            evidence_references=[f"agent:{child.agent_id}"],
            reason=child.error_message or child.result or "",
        )
