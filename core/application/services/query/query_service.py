"""Agent query service for read operations (CQRS read side).

Handles statistics, results, and event-count queries; delegates the
readiness poll to AgentReadinessService.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.application.services.query.agent_readiness import AgentReadinessService
from core.application.services.query.read_models import AgentSummaryReadModel


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.ports.runtime_ports import SharedContextPort


class AgentQueryService:
    """Query service for agent read operations.

    Single Responsibility: Handle read-only queries about agents.
    CQRS pattern: Separates read operations from write operations.
    """

    def __init__(
        self,
        repository: AgentRepository,
        shared_context_port: SharedContextPort | None = None,
    ) -> None:
        self._repository = repository
        self._shared_context_port = shared_context_port
        self._readiness = AgentReadinessService(repository)

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

    async def get_statistics(
        self,
        root_id: UUID | None = None,
    ) -> SystemStatisticsDTO:
        """Get statistics about agents.

        Args:
            root_id: If provided, only count agents in this hierarchy.
                     If None, counts all agents in the database.

        Uses hierarchy-specific query when root_id is provided for accurate
        per-run statistics. Uses lightweight read model instead of full
        aggregate reconstruction.
        """
        if root_id is not None:
            all_events = await self._repository.get_hierarchy_events_grouped(root_id)
        else:
            all_events = await self._repository.get_all_events_grouped()

        total = 0
        completed = 0
        failed = 0
        active = 0

        for events in all_events.values():
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

    async def get_event_counts(self, agent_ids: list[UUID]) -> dict[UUID, int]:
        """Return number of persisted events per agent.

        Used by the system loop's staleness watchdog to detect agents that
        keep getting re-scheduled by ``get_active_agent_ids`` but never emit
        any new events (boss-in-judge-loop, manager re-decomposition cycle,
        etc.) — a pattern the per-task wall-clock watchdog cannot catch
        because each individual ``run_agent_step`` call completes quickly.
        """
        if not agent_ids:
            return {}
        return await self._repository.get_event_counts(agent_ids)

    async def get_subtree_event_counts(self, agent_ids: list[UUID]) -> dict[UUID, int]:
        """Return event counts across entire descendant subtree per agent.

        Uses a recursive CTE over ChildSpawned links so that a manager waiting
        for queued sub-workers is not mistakenly declared stale — the manager
        emits no own-events while waiting, but its subtree does.
        """
        if not agent_ids:
            return {}
        return await self._repository.get_subtree_event_counts(agent_ids)

    async def get_active_agent_ids(
        self,
        root_id: UUID | None = None,
        sequential_workers: bool = False,
    ) -> list[UUID]:
        """Return agent IDs eligible to take a step (see AgentReadinessService)."""
        return await self._readiness.get_active_agent_ids(
            root_id=root_id,
            sequential_workers=sequential_workers,
        )
