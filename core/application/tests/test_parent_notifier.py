"""Tests for ParentNotificationService failure handling policies."""

from uuid import uuid4

import pytest

from bootstrap.tests.test_characterization import InMemoryEventStore
from core.application.services.agent_repository import AgentRepository
from core.application.services.parent_notifier import ParentNotificationService
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.values.node_message import Briefing
from core.domain.values.subtask import Subtask


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


async def _create_waiting_parent(
    repository: AgentRepository,
) -> tuple[AgentSession, AgentSession]:
    """Persist a parent in WAITING plus an infeasible child in FAILED."""
    parent = AgentSession.create(
        agent_id=uuid4(),
        role=AgentRole.BOSS,
        config=_config(),
    )
    parent.assign_task("Main task")
    spawned = parent.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="Subtask", config=_config())],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Main task"),
    )
    child_id, _ = spawned[0]
    await repository.save_new_agent(parent)

    child = AgentSession.create(
        agent_id=child_id,
        role=AgentRole.PENDING,
        config=_config(),
        parent_id=parent.agent_id,
    )
    child.assign_task("Subtask")
    child.mark_infeasible("No viable plan")
    await repository.save_new_agent(child)
    return parent, child


@pytest.mark.asyncio
async def test_infeasible_child_triggers_redecomposition_within_limit() -> None:
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(
        repository=repository,
        max_redecompositions=1,
    )

    parent, child = await _create_waiting_parent(repository)

    await notifier.notify_if_failed(child)

    reloaded_parent = await repository.load(parent.agent_id)
    assert reloaded_parent.status == AgentStatus.ANALYZING
    assert reloaded_parent.redecomposition_count == 1
    assert reloaded_parent.child_ids == []


@pytest.mark.asyncio
async def test_infeasible_child_propagates_failure_after_limit_exhausted() -> None:
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(
        repository=repository,
        max_redecompositions=1,
    )

    parent, child = await _create_waiting_parent(repository)
    await notifier.notify_if_failed(child)

    reloaded_parent = await repository.load(parent.agent_id)
    current_version = reloaded_parent.version
    spawned = reloaded_parent.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="Retry subtask", config=_config())],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Main task"),
    )
    retry_child_id, _ = spawned[0]
    await repository.persist_events(reloaded_parent, current_version)

    retry_child = AgentSession.create(
        agent_id=retry_child_id,
        role=AgentRole.PENDING,
        config=_config(),
        parent_id=parent.agent_id,
    )
    retry_child.assign_task("Retry subtask")
    retry_child.mark_infeasible("Still impossible")
    await repository.save_new_agent(retry_child)

    await notifier.notify_if_failed(retry_child)

    failed_parent = await repository.load(parent.agent_id)
    assert failed_parent.status == AgentStatus.FAILED
    assert failed_parent.redecomposition_count == 1
    assert "Redecomposition limit reached (1)" in (failed_parent.error_message or "")
