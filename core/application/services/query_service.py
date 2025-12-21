"""Agent query service for read operations (CQRS read side).

Handles statistics, results, and active agent queries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.model import AgentStatus


if TYPE_CHECKING:
    from core.application.services.agent_repository import AgentRepository


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
        """Get system-wide statistics about all agents."""
        all_ids = await self._repository.get_all_agent_ids()

        completed = 0
        failed = 0
        active = 0

        for agent_id in all_ids:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                continue

            if agent.status == AgentStatus.COMPLETED:
                completed += 1
            elif agent.status == AgentStatus.FAILED:
                failed += 1
            else:
                active += 1

        return SystemStatisticsDTO(
            total_agents=len(all_ids),
            completed=completed,
            failed=failed,
            active=active,
        )

    async def get_active_agent_ids(self) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED)."""
        all_ids = await self._repository.get_all_agent_ids()
        active_ids = []

        for agent_id in all_ids:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is not None and not agent.is_terminal():
                active_ids.append(agent_id)

        return active_ids

    async def is_hierarchy_complete(self, root_id: UUID) -> bool:
        """Check if all agents in hierarchy have reached terminal state."""
        active_ids = await self.get_active_agent_ids()
        return len(active_ids) == 0
