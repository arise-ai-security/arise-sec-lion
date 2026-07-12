"""Tests for successful hard-dependency readiness across every agent role."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.application.services import AgentReadinessService
from core.domain.events.events import AgentCreated, StatusChanged, TaskAssigned, WorkFailed


def _events(
    agent_id: UUID,
    *,
    role: str,
    parent_id: UUID | None,
    sibling_index: int,
    depends_on: list[int] | None = None,
    dependency_failure_policy: str = "block",
    status: str = "analyzing",
):
    events = [
        AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role=role,
            parent_id=parent_id,
            config={},
            sibling_index=sibling_index,
            depends_on=depends_on or [],
            dependency_failure_policy=dependency_failure_policy,
        ),
        TaskAssigned(
            aggregate_id=agent_id,
            sequence_number=2,
            task_description="task",
        ),
    ]
    if status == "failed":
        events.append(
            WorkFailed(
                aggregate_id=agent_id,
                sequence_number=3,
                reason="failed",
            )
        )
    elif status != "analyzing":
        events.append(
            StatusChanged(
                aggregate_id=agent_id,
                sequence_number=3,
                old_status="analyzing",
                new_status=status,
            )
        )
    return events


@pytest.mark.parametrize("consumer_role", ["pending", "manager", "worker"])
@pytest.mark.parametrize("sequential_workers", [False, True])
async def test_failed_hard_dependency_blocks_every_consumer_role(
    consumer_role: str,
    sequential_workers: bool,
) -> None:
    # Given: A consumer whose required predecessor failed
    parent_id = uuid4()
    producer_id = uuid4()
    consumer_id = uuid4()
    repository = AsyncMock()
    repository.get_all_events_grouped.return_value = {
        parent_id: _events(
            parent_id,
            role="boss",
            parent_id=None,
            sibling_index=0,
            status="waiting",
        ),
        producer_id: _events(
            producer_id,
            role="worker",
            parent_id=parent_id,
            sibling_index=0,
            status="failed",
        ),
        consumer_id: _events(
            consumer_id,
            role=consumer_role,
            parent_id=parent_id,
            sibling_index=1,
            depends_on=[0],
        ),
    }

    # When: Readiness is computed
    active = await AgentReadinessService(repository).get_active_agent_ids(
        sequential_workers=sequential_workers
    )

    # Then: The consumer remains blocked
    assert consumer_id not in active


async def test_continue_policy_allows_optional_failed_predecessor() -> None:
    # Given: A consumer explicitly allowed to continue after predecessor failure
    parent_id = uuid4()
    producer_id = uuid4()
    consumer_id = uuid4()
    repository = AsyncMock()
    repository.get_all_events_grouped.return_value = {
        parent_id: _events(
            parent_id,
            role="boss",
            parent_id=None,
            sibling_index=0,
            status="waiting",
        ),
        producer_id: _events(
            producer_id,
            role="worker",
            parent_id=parent_id,
            sibling_index=0,
            status="failed",
        ),
        consumer_id: _events(
            consumer_id,
            role="worker",
            parent_id=parent_id,
            sibling_index=1,
            depends_on=[0],
            dependency_failure_policy="continue",
        ),
    }

    # When: Readiness is computed
    active = await AgentReadinessService(repository).get_active_agent_ids()

    # Then: The explicitly optional dependency does not block the consumer
    assert consumer_id in active
