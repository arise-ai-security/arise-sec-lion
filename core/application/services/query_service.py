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
    CodeGenerationStarted,
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
            elif isinstance(event, CodeGenerationStarted):
                status = "in_progress"
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
            agent_id=str(agent.agent_id),
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

    async def get_active_agent_ids(self, root_id: UUID | None = None) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED).

        Args:
            root_id: If provided, only return agents in this hierarchy.
                     Filters to agents where parent chain leads to root_id.

        Uses get_all_events_grouped() for single-query efficiency (avoids N+1).
        Uses lightweight read model instead of full aggregate reconstruction.
        """
        all_events = await self._repository.get_all_events_grouped()

        # Build summaries for all agents
        summaries: dict[UUID, AgentSummaryReadModel] = {}
        for agent_id, events in all_events.items():
            summary = AgentSummaryReadModel.from_events(events)
            if summary is not None:
                summaries[agent_id] = summary

        # If root_id specified, filter to only agents in that hierarchy
        if root_id is not None:
            hierarchy_ids = self._get_hierarchy_ids(root_id, summaries)
            summaries = {k: v for k, v in summaries.items() if k in hierarchy_ids}

        # Return non-terminal agents
        return [
            agent_id
            for agent_id, summary in summaries.items()
            if not summary.is_terminal
        ]

    def _get_hierarchy_ids(
        self,
        root_id: UUID,
        summaries: dict[UUID, "AgentSummaryReadModel"],
    ) -> set[UUID]:
        """Get all agent IDs that belong to a hierarchy rooted at root_id.

        Traverses the tree top-down from root to find all descendants.
        """
        if root_id not in summaries:
            return set()

        # Build parent->children map for efficient traversal
        children_map: dict[UUID | None, list[UUID]] = {}
        for agent_id, summary in summaries.items():
            parent = summary.parent_id
            if parent not in children_map:
                children_map[parent] = []
            children_map[parent].append(agent_id)

        # BFS from root to collect all descendants
        hierarchy: set[UUID] = {root_id}
        queue = [root_id]

        while queue:
            current = queue.pop(0)
            for child_id in children_map.get(current, []):
                if child_id not in hierarchy:
                    hierarchy.add(child_id)
                    queue.append(child_id)

        return hierarchy

    async def is_hierarchy_complete(self, root_id: UUID) -> bool:
        """Check if all agents in hierarchy have reached terminal state."""
        active_ids = await self.get_active_agent_ids(root_id=root_id)
        return len(active_ids) == 0
