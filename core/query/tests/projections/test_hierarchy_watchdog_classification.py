"""Tests for hierarchy watchdog classification in the API route layer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from core.domain.events.events import AgentCreated, ChildSpawned, TaskAssigned, WorkCompleted
from core.domain.values.subtask import Subtask
from core.query.projections.hierarchy_builder import AgentNode
from query.api.routes.agents import _compute_watchdog, _watchdog_defaults


def _mk_agent_created(*, aid, role: str, parent_id=None):
    return AgentCreated(
        aggregate_id=aid,
        sequence_number=1,
        role=role,
        parent_id=parent_id,
        config={},
    )


def _mk_task_assigned(*, aid, seq: int, desc: str) -> TaskAssigned:
    return TaskAssigned(
        aggregate_id=aid,
        sequence_number=seq,
        task_description=desc,
    )


def _mk_child_spawned(
    *,
    parent_id,
    seq: int,
    child_id,
    sibling_index: int,
    depends_on: list[int],
) -> ChildSpawned:
    return ChildSpawned(
        aggregate_id=parent_id,
        sequence_number=seq,
        child_id=child_id,
        child_role="worker",
        sibling_index=sibling_index,
        subtask=Subtask(
            description=f"child-{sibling_index}",
            config={},
            depends_on=depends_on,
        ),
        child_config={},
    )


def test_compute_watchdog_marks_blocked_dependencies_when_exec_not_started() -> None:
    now = datetime.now(UTC)
    watchdog_cfg = _watchdog_defaults()

    parent_id = uuid4()
    dep_child_id = uuid4()
    blocked_child_id = uuid4()

    parent_events = [
        _mk_agent_created(aid=parent_id, role="manager"),
        _mk_child_spawned(
            parent_id=parent_id,
            seq=2,
            child_id=dep_child_id,
            sibling_index=0,
            depends_on=[],
        ),
        _mk_child_spawned(
            parent_id=parent_id,
            seq=3,
            child_id=blocked_child_id,
            sibling_index=1,
            depends_on=[0],
        ),
    ]

    # Dependency child has not completed yet.
    dep_events = [
        _mk_agent_created(aid=dep_child_id, role="worker", parent_id=parent_id),
        _mk_task_assigned(aid=dep_child_id, seq=2, desc="dep child"),
    ]
    blocked_events = [
        _mk_agent_created(aid=blocked_child_id, role="worker", parent_id=parent_id),
        _mk_task_assigned(aid=blocked_child_id, seq=2, desc="blocked child"),
    ]

    hierarchy_events = {
        parent_id: parent_events,
        dep_child_id: dep_events,
        blocked_child_id: blocked_events,
    }
    node = AgentNode(
        id=blocked_child_id,
        role="worker",
        status="analyzing",
        task_description="blocked child",
        parent_id=parent_id,
    )

    phase, timeout_s, elapsed_s, overdue, next_action = _compute_watchdog(
        node=node,
        events=blocked_events,
        hierarchy_events=hierarchy_events,
        now_utc=now,
        idle_seconds=1200,
        watchdog_cfg=watchdog_cfg,
    )

    assert phase == "blocked_dependencies"
    assert timeout_s == watchdog_cfg["worker_silence_timeout_seconds"]
    assert elapsed_s == 1200
    assert overdue is False
    assert next_action == "wait_for_dependencies_or_parent_recovery"


def test_compute_watchdog_falls_back_to_silent_when_dependencies_met() -> None:
    now = datetime.now(UTC)
    watchdog_cfg = _watchdog_defaults()

    parent_id = uuid4()
    dep_child_id = uuid4()
    child_id = uuid4()

    parent_events = [
        _mk_agent_created(aid=parent_id, role="manager"),
        _mk_child_spawned(
            parent_id=parent_id,
            seq=2,
            child_id=dep_child_id,
            sibling_index=0,
            depends_on=[],
        ),
        _mk_child_spawned(
            parent_id=parent_id,
            seq=3,
            child_id=child_id,
            sibling_index=1,
            depends_on=[0],
        ),
    ]
    dep_events = [
        _mk_agent_created(aid=dep_child_id, role="worker", parent_id=parent_id),
        _mk_task_assigned(aid=dep_child_id, seq=2, desc="dep child"),
        WorkCompleted(
            aggregate_id=dep_child_id,
            sequence_number=3,
            result="done",
        ),
    ]
    child_events = [
        _mk_agent_created(aid=child_id, role="worker", parent_id=parent_id),
        _mk_task_assigned(aid=child_id, seq=2, desc="child"),
    ]

    hierarchy_events = {
        parent_id: parent_events,
        dep_child_id: dep_events,
        child_id: child_events,
    }
    node = AgentNode(
        id=child_id,
        role="worker",
        status="analyzing",
        task_description="child",
        parent_id=parent_id,
    )

    phase, timeout_s, elapsed_s, overdue, next_action = _compute_watchdog(
        node=node,
        events=child_events,
        hierarchy_events=hierarchy_events,
        now_utc=now,
        idle_seconds=1200,
        watchdog_cfg=watchdog_cfg,
    )

    assert phase == "silent_worker"
    assert timeout_s == watchdog_cfg["worker_silence_timeout_seconds"]
    assert elapsed_s == 1200
    assert overdue is True
    assert next_action == "retry_or_fail"
