"""Invariant tests for watchdog phase classification."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.domain.events.events import AgentCreated, AgentExecutionStarted, ChildSpawned, TaskAssigned
from core.domain.values.subtask import Subtask
from core.query.projections.hierarchy_builder import AgentNode
from query.api.routes.agents import _compute_watchdog, _watchdog_defaults


def _agent_created(*, aid, role: str, parent_id=None) -> AgentCreated:
    return AgentCreated(
        aggregate_id=aid,
        sequence_number=1,
        role=role,
        parent_id=parent_id,
        config={},
    )


def _task_assigned(*, aid, seq: int) -> TaskAssigned:
    return TaskAssigned(
        aggregate_id=aid,
        sequence_number=seq,
        task_description="task",
    )


def _child_spawned(*, parent_id, seq: int, child_id, idx: int, depends_on: list[int]) -> ChildSpawned:
    return ChildSpawned(
        aggregate_id=parent_id,
        sequence_number=seq,
        child_id=child_id,
        child_role="worker",
        sibling_index=idx,
        subtask=Subtask(description=f"child-{idx}", config={}, depends_on=depends_on),
        child_config={},
    )


@pytest.mark.parametrize("idle_seconds", [60, 300, 1800])
def test_blocked_dependency_workers_are_not_misclassified_as_retryable_hangs(
    idle_seconds: int,
) -> None:
    """Invariant: unmet dependencies => blocked_dependencies, not retry_or_fail."""
    now = datetime.now(UTC)
    cfg = _watchdog_defaults()

    parent_id = uuid4()
    dep_child = uuid4()
    blocked_child = uuid4()

    hierarchy_events = {
        parent_id: [
            _agent_created(aid=parent_id, role="manager"),
            _child_spawned(parent_id=parent_id, seq=2, child_id=dep_child, idx=0, depends_on=[]),
            _child_spawned(
                parent_id=parent_id,
                seq=3,
                child_id=blocked_child,
                idx=1,
                depends_on=[0],
            ),
        ],
        dep_child: [
            _agent_created(aid=dep_child, role="worker", parent_id=parent_id),
            _task_assigned(aid=dep_child, seq=2),
            # dependency intentionally never completes
        ],
        blocked_child: [
            _agent_created(aid=blocked_child, role="worker", parent_id=parent_id),
            _task_assigned(aid=blocked_child, seq=2),
        ],
    }

    phase, timeout_s, elapsed_s, overdue, next_action = _compute_watchdog(
        node=AgentNode(
            id=blocked_child,
            role="worker",
            status="analyzing",
            task_description="blocked child",
            parent_id=parent_id,
        ),
        events=hierarchy_events[blocked_child],
        hierarchy_events=hierarchy_events,
        now_utc=now,
        idle_seconds=idle_seconds,
        watchdog_cfg=cfg,
    )

    assert phase == "blocked_dependencies"
    assert timeout_s == cfg["worker_silence_timeout_seconds"]
    assert elapsed_s == idle_seconds
    assert overdue is False
    assert next_action == "wait_for_dependencies_or_parent_recovery"


def test_zero_thought_executing_worker_stays_retryable() -> None:
    """Invariant: real execution stall remains retryable no-progress path."""
    now = datetime.now(UTC)
    cfg = _watchdog_defaults()

    worker_id = uuid4()
    events = [
        _agent_created(aid=worker_id, role="worker"),
        _task_assigned(aid=worker_id, seq=2),
        AgentExecutionStarted(
            aggregate_id=worker_id,
            sequence_number=3,
            role="worker",
            depth=1,
            occurred_at=now.replace(microsecond=0),
        ),
    ]

    phase, _timeout_s, _elapsed_s, _overdue, next_action = _compute_watchdog(
        node=AgentNode(
            id=worker_id,
            role="worker",
            status="analyzing",
            task_description="worker",
            parent_id=None,
        ),
        events=events,
        hierarchy_events={worker_id: events},
        now_utc=now.replace(microsecond=0),
        idle_seconds=999,
        watchdog_cfg=cfg,
    )

    assert phase in {"no_progress_zero_thoughts", "step_timeout"}
    assert next_action == "provider_reconnect_then_retry_or_fail" or next_action == "retry_or_fail"
