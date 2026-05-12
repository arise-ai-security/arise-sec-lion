"""Idempotence tests for AgentSession child-report handling (D.1)."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import ChildCompleted, ChildFailed
from core.domain.values.node_message import Briefing
from core.domain.values.subtask import Subtask


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def _build_waiting_manager_with_children() -> tuple[AgentSession, list]:
    """Manager in WAITING with two spawned children A, B."""
    manager = AgentSession.create(
        agent_id=uuid4(),
        role=AgentRole.BOSS,
        config=_config(),
    )
    manager.assign_task("Top task")
    spawned = manager.apply_subtasks_and_spawn_children(
        subtasks=[
            Subtask(description="A", config=_config()),
            Subtask(description="B", config=_config()),
        ],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Top task"),
    )
    return manager, [child_id for child_id, _ in spawned]


def test_handle_child_update_is_idempotent() -> None:
    """A repeated child completion notification must not emit a second event."""

    # Given: a manager in WAITING with two children A, B
    manager, child_ids = _build_waiting_manager_with_children()
    child_a = child_ids[0]

    # When: handle_child_update for child A fires twice
    manager.handle_child_update(child_id=child_a, result="A done")
    events_after_first = list(manager.events)
    manager.handle_child_update(child_id=child_a, result="A done again")

    # Then: exactly one ChildCompleted(A) is recorded
    child_completed_for_a = [
        e for e in manager.events
        if isinstance(e, ChildCompleted) and e.child_id == child_a
    ]
    assert len(child_completed_for_a) == 1

    # And: no new events were appended on the second call
    assert len(manager.events) == len(events_after_first)


def test_handle_child_failure_is_idempotent() -> None:
    """A repeated child failure notification must not emit a second event."""

    # Given: a manager in WAITING with two children A, B
    manager, child_ids = _build_waiting_manager_with_children()
    child_a = child_ids[0]

    # When: handle_child_failure for child A fires twice
    manager.handle_child_failure(child_id=child_a, reason="boom")
    events_after_first = list(manager.events)
    manager.handle_child_failure(child_id=child_a, reason="boom again")

    # Then: exactly one ChildFailed(A) is recorded
    child_failed_for_a = [
        e for e in manager.events
        if isinstance(e, ChildFailed) and e.child_id == child_a
    ]
    assert len(child_failed_for_a) == 1

    # And: no new events were appended on the second call
    assert len(manager.events) == len(events_after_first)


def test_handle_child_update_idempotent_against_prior_failure() -> None:
    """A completion for an already-failed child must no-op."""

    # Given: a manager in WAITING with two children A, B; A failed.
    manager, child_ids = _build_waiting_manager_with_children()
    child_a = child_ids[0]
    manager.handle_child_failure(child_id=child_a, reason="boom")
    events_before = list(manager.events)

    # When: a late completion notification arrives for A
    manager.handle_child_update(child_id=child_a, result="A done")

    # Then: no new event is recorded
    assert len(manager.events) == len(events_before)
