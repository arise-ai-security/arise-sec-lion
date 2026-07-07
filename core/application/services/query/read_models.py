"""Lightweight read models built from events (CQRS read side)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.domain.events.events import (
    AgentCreated,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DomainEvent,
    RedecompositionTriggered,
    RetryScheduled,
    StatusChanged,
    TaskAssigned,
    VerificationFailed,
    WorkCompleted,
    WorkFailed,
)


if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True)
class AgentSummaryReadModel:
    """Lightweight read model for agent queries.

    Avoids full aggregate reconstruction by extracting only needed fields
    directly from events. Much faster than AgentSession.load_from_history().
    """

    agent_id: UUID
    role: str
    status: str
    parent_id: UUID | None
    task_summary: str
    is_terminal: bool
    sibling_index: int  # Position among siblings for ordering
    depends_on: tuple[int, ...]  # Sibling indices this agent depends on (DAG scheduling)

    @classmethod
    def from_events(cls, events: list[DomainEvent]) -> AgentSummaryReadModel | None:
        """Build read model from events without full aggregate reconstruction.

        Only processes the events needed to extract summary fields:
        - AgentCreated: initial role, parent_id, sibling_index
        - TaskAssigned: task_description
        - ComplexityEvaluated: updated role (PENDING -> WORKER/MANAGER)
        - StatusChanged/CodeGenerationStarted/WorkCompleted/WorkFailed: current status

        Args:
            events: List of domain events for an aggregate.

        Returns:
            AgentSummaryReadModel or None if events are invalid.
        """
        if not events:
            return None

        first_event = events[0]
        if not isinstance(first_event, AgentCreated):
            return None

        # Extract from AgentCreated
        agent_id = first_event.aggregate_id
        role = first_event.role
        parent_id = first_event.parent_id
        sibling_index = first_event.sibling_index
        depends_on = tuple(first_event.depends_on)

        # Default values
        task_summary = ""
        status = "pending"  # Default after creation

        # Process events to extract status, task, and role changes
        for event in events:
            if isinstance(event, TaskAssigned):
                task_summary = event.task_description[:100]  # Truncate for summary
                status = "analyzing"  # TaskAssigned transitions to ANALYZING
            elif isinstance(event, ComplexityEvaluated):
                # CRITICAL: Update role when complexity evaluation determines WORKER/MANAGER
                # Without this, PENDING agents that become WORKER are not detected as workers
                # and bypass the sequential execution check!
                role = event.determined_role
            elif isinstance(event, StatusChanged):
                status = event.new_status
            elif isinstance(event, CodeGenerationStarted):
                status = "in_progress"
            elif isinstance(event, WorkCompleted):
                status = "completed"
            elif isinstance(event, (WorkFailed, VerificationFailed, DecisionInfeasible)):
                status = "failed"
            elif isinstance(event, RetryScheduled):
                # FAILED → ANALYZING: agent is retrying, no longer terminal
                status = "analyzing"
            elif isinstance(event, RedecompositionTriggered):
                # WAITING → ANALYZING: parent re-decomposes after child infeasible
                status = "analyzing"

        is_terminal = status in ("completed", "failed")

        return cls(
            agent_id=agent_id,
            role=role,
            status=status,
            parent_id=parent_id,
            task_summary=task_summary,
            is_terminal=is_terminal,
            sibling_index=sibling_index,
            depends_on=depends_on,
        )
