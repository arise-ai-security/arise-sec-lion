"""Tests for durable terminal post-step events."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import PostStepCompleted, PostStepRequested, WorkFailed


def test_terminal_outcome_requests_and_completes_post_step_idempotently() -> None:
    """Terminal outcomes append a correlated request that replay can reconcile once."""

    # Given: a committed worker session.
    agent = AgentSession.create(
        agent_id=uuid4(),
        role=AgentRole.WORKER,
        config={
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.0, "max_tokens": 1},
            "tool": "claude_code",
        },
    )
    history = list(agent.events)
    agent.mark_changes_as_committed()

    # When: the worker reaches a terminal failure.
    agent.fail_with_reason("tool crashed")

    # Then: the failure and its request are one ordered uncommitted batch.
    assert [type(event) for event in agent.events] == [WorkFailed, PostStepRequested]
    failed, requested = agent.events
    assert requested.terminal_event_id == failed.event_id
    assert requested.sequence_number == failed.sequence_number + 1

    # When: the request is completed and replayed twice.
    agent.complete_post_step(failed.event_id)
    agent.complete_post_step(failed.event_id)
    history.extend(agent.events)
    replayed = AgentSession.load_from_history(history)

    # Then: only one completion exists and replay has no pending post-step work.
    assert len([event for event in agent.events if isinstance(event, PostStepCompleted)]) == 1
    assert replayed.pending_post_step_terminal_event_id is None
