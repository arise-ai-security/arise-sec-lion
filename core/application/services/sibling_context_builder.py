"""Sibling context builder for worker execution."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from core.application.services.query_service import AgentSummaryReadModel
from core.domain.sibling_context import (
    DecisionInfo,
    SiblingTaskInfo,
    WorkerSiblingContext,
)

if TYPE_CHECKING:
    from core.application.services.agent_repository import AgentRepository
    from core.ports.shared_context_port import SharedContextPort


class SiblingContextBuilder:
    """Builds sibling context for worker execution."""

    def __init__(
        self,
        repository: AgentRepository,
        shared_context_port: SharedContextPort | None = None,
    ) -> None:
        self._repository = repository
        self._shared_context_port = shared_context_port

    async def build_context(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> WorkerSiblingContext:
        """Build complete sibling context for a worker."""
        parent_task = await self._get_parent_task(parent_id)
        sibling_tasks = await self._get_sibling_tasks(agent_id, parent_id)
        shared_decisions = await self._get_shared_decisions(root_id)

        return WorkerSiblingContext(
            current_agent_id=str(agent_id),
            parent_task=parent_task,
            sibling_tasks=tuple(sibling_tasks),
            shared_decisions=tuple(shared_decisions),
        )

    async def _get_parent_task(self, parent_id: UUID | None) -> str | None:
        if parent_id is None:
            return None
        parent = await self._repository.load_if_exists(parent_id)
        return parent.task_description if parent else None

    async def _get_sibling_tasks(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
    ) -> list[SiblingTaskInfo]:
        if parent_id is None:
            return []

        all_events = await self._repository.get_all_events_grouped()

        siblings: list[SiblingTaskInfo] = []
        for agg_id, events in all_events.items():
            if agg_id == agent_id:
                continue

            summary = AgentSummaryReadModel.from_events(events)
            if summary is None or summary.parent_id != parent_id:
                continue

            result_summary = None
            if summary.status == "completed":
                agent = await self._repository.load_if_exists(agg_id)
                if agent and agent.result:
                    result_summary = agent.result[:500]

            siblings.append(
                SiblingTaskInfo(
                    agent_id=str(agg_id),
                    sibling_index=summary.sibling_index,
                    status=summary.status,
                    task_summary=summary.task_summary[:200],
                    result_summary=result_summary,
                )
            )

        siblings.sort(key=lambda s: s.sibling_index)
        return siblings

    async def _get_shared_decisions(self, root_id: UUID) -> list[DecisionInfo]:
        if self._shared_context_port is None:
            return []

        context = await self._shared_context_port.get(root_id)
        if context is None:
            return []

        decisions: list[DecisionInfo] = []
        for key in context.list_decisions():
            decision = context.get_decision(key)
            if decision:
                decisions.append(
                    DecisionInfo(
                        key=decision.key,
                        value=decision.value,
                        rationale=decision.rationale,
                        decided_by=str(decision.decided_by),
                    )
                )
        return decisions
