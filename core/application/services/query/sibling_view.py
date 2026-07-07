"""Sibling coordination view: builds the Handoff a worker receives (SiblingViewPort)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.services.query.read_models import AgentSummaryReadModel
from core.domain.values.node_message import (
    WORKER_CONCLUSION_MARKER,
    Handoff,
    PeerStatus,
    SharedDecision,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.ports.runtime_ports import SharedContextPort


def _head_tail(text: str, limit: int) -> str:
    """Keep first 2/3 + last 1/3 of text, showing omission count."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n[...{omitted} chars omitted...]\n\n{text[-tail:]}"


def _sibling_result_summary(result: str, limit: int = 2000) -> str:
    """Sibling-facing summary of a worker result.

    The full result is a judge-oriented command log; its transcript tail is
    noise for siblings (failed tool calls, truncated output). Prefer the
    worker's own conclusion when the adapter recorded one.
    """
    marker_pos = result.rfind(WORKER_CONCLUSION_MARKER)
    if marker_pos != -1:
        conclusion = result[marker_pos + len(WORKER_CONCLUSION_MARKER) :].strip()
        if conclusion:
            return _head_tail(conclusion, limit)
    return _head_tail(result, limit)


class SiblingViewService:
    """Builds the sibling-coordination Handoff for an agent (SiblingViewPort impl)."""

    def __init__(
        self,
        repository: AgentRepository,
        shared_context_port: SharedContextPort | None = None,
    ) -> None:
        self._repository = repository
        self._shared_context_port = shared_context_port

    async def build_view(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> Handoff:
        parent_task = await self._get_parent_task(parent_id)
        current_sibling_index = await self._get_current_sibling_index(agent_id)
        sibling_statuses = await self._get_sibling_statuses(agent_id, parent_id)
        shared_decisions = await self._get_shared_decisions(root_id)

        return Handoff(
            parent_task=parent_task,
            current_sibling_index=current_sibling_index,
            siblings=tuple(sibling_statuses),
            shared_decisions=tuple(shared_decisions),
        )

    async def _get_parent_task(self, parent_id: UUID | None) -> str | None:
        if parent_id is None:
            return None
        parent = await self._repository.load_if_exists(parent_id)
        return parent.task_description if parent else None

    async def _get_current_sibling_index(self, agent_id: UUID) -> int | None:
        agent = await self._repository.load_if_exists(agent_id)
        if agent is None:
            return None
        return agent.sibling_index

    async def _get_sibling_statuses(
        self, agent_id: UUID, parent_id: UUID | None
    ) -> list[PeerStatus]:
        if parent_id is None:
            return []

        children_events = await self._repository.get_children_events_grouped(parent_id)

        siblings: list[PeerStatus] = []
        for agg_id, events in children_events.items():
            if agg_id == agent_id:
                continue

            summary = AgentSummaryReadModel.from_events(events)
            if summary is None:
                continue

            result_summary = None
            if summary.status == "completed":
                agent = await self._repository.load_if_exists(agg_id)
                if agent and agent.result:
                    result_summary = _sibling_result_summary(agent.result)

            siblings.append(
                PeerStatus(
                    agent_id=str(agg_id),
                    sibling_index=summary.sibling_index,
                    status=summary.status,
                    task_summary=summary.task_summary[:200],
                    result_summary=result_summary,
                )
            )

        siblings.sort(key=lambda s: s.sibling_index)
        return siblings

    async def _get_shared_decisions(self, root_id: UUID) -> list[SharedDecision]:
        if self._shared_context_port is None:
            return []

        context = await self._shared_context_port.get(root_id)
        if context is None:
            return []

        decisions: list[SharedDecision] = []
        for key in context.list_decisions():
            decision = context.get_decision(key)
            if decision:
                decisions.append(
                    SharedDecision(
                        key=decision.key,
                        value=decision.value,
                        rationale=decision.rationale,
                        decided_by=str(decision.decided_by),
                    )
                )
        return decisions
