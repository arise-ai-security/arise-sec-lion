"""Agent query service for read operations (CQRS read side).

Handles statistics, results, and active agent queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events import (
    AgentCreated,
    DomainEvent,
    StatusChanged,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)
from core.domain.model import AgentSession


if TYPE_CHECKING:
    from core.application.services.agent_repository import AgentRepository


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

    @classmethod
    def from_events(cls, events: list[DomainEvent]) -> "AgentSummaryReadModel | None":
        """Build read model from events without full aggregate reconstruction.

        Only processes the events needed to extract summary fields:
        - AgentCreated: role, parent_id
        - TaskAssigned: task_description
        - StatusChanged/WorkCompleted/WorkFailed: current status

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

        # Default values
        task_summary = ""
        status = "pending"  # Default after creation

        # Process events to extract status and task (only relevant event types)
        for event in events:
            if isinstance(event, TaskAssigned):
                task_summary = event.task_description[:100]  # Truncate for summary
                status = "analyzing"  # TaskAssigned transitions to ANALYZING
            elif isinstance(event, StatusChanged):
                status = event.new_status
            elif isinstance(event, WorkCompleted):
                status = "completed"
            elif isinstance(event, WorkFailed):
                status = "failed"

        is_terminal = status in ("completed", "failed")

        return cls(
            agent_id=agent_id,
            role=role,
            status=status,
            parent_id=parent_id,
            task_summary=task_summary,
            is_terminal=is_terminal,
        )


class AgentQueryService:
    """Query service for agent read operations.

    Single Responsibility: Handle read-only queries about agents.
    CQRS pattern: Separates read operations from write operations.
    """

    def __init__(self, repository: AgentRepository) -> None:
        self._repository = repository

    async def get_result(self, agent_id: UUID) -> AgentResultDTO:
        """Get agent result as DTO.

        Raises:
            AgentNotFoundError: If agent doesn't exist.
        """
        agent = await self._repository.load(agent_id)
        return AgentResultDTO(
            agent_id=str(agent.session_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_statistics(self) -> SystemStatisticsDTO:
        """Get system-wide statistics about all agents.

        Uses get_all_events_grouped() for single-query efficiency (avoids N+1).
        Uses lightweight read model instead of full aggregate reconstruction.
        """
        all_events = await self._repository.get_all_events_grouped()

        total = 0
        completed = 0
        failed = 0
        active = 0

        for agent_id, events in all_events.items():
            summary = AgentSummaryReadModel.from_events(events)
            if summary is None:
                continue

            total += 1
            if summary.status == "completed":
                completed += 1
            elif summary.status == "failed":
                failed += 1
            else:
                active += 1

        return SystemStatisticsDTO(
            total_agents=total,
            completed=completed,
            failed=failed,
            active=active,
        )

    async def get_active_agent_ids(self) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED).

        Uses get_all_events_grouped() for single-query efficiency (avoids N+1).
        Uses lightweight read model instead of full aggregate reconstruction.
        """
        all_events = await self._repository.get_all_events_grouped()
        active_ids = []

        for agent_id, events in all_events.items():
            summary = AgentSummaryReadModel.from_events(events)
            if summary is not None and not summary.is_terminal:
                active_ids.append(agent_id)

        return active_ids

    async def is_hierarchy_complete(self, root_id: UUID) -> bool:
        """Check if all agents in hierarchy have reached terminal state."""
        active_ids = await self.get_active_agent_ids()
        return len(active_ids) == 0
