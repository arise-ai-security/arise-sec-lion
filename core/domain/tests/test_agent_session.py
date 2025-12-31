"""Test cases for AgentSession aggregate (TDD approach).

These tests are written before the implementation exists, following
Test-Driven Development principles.
"""

from uuid import uuid4

from core.domain.events.events import AgentCreated, TaskAssigned
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus


def test_boss_initialization_flow() -> None:
    """Test that a BOSS agent is properly initialized with correct events."""

    # Given: Define UUID and configuration
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }

    # When: Create a new AgentSession
    agent = AgentSession.create(
        agent_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )

    # Then: Verify uncommitted events contain exactly 1 AgentCreated event
    assert len(agent.events) == 1, "Agent should have exactly 1 uncommitted event"

    first_event = agent.events[0]
    assert isinstance(first_event, AgentCreated), "First event must be AgentCreated"
    assert first_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"
    assert first_event.role == AgentRole.BOSS.value, "Event role must be BOSS"
    assert first_event.parent_id is None, "BOSS has no parent"
    assert first_event.config == config, "Event config must match input"

    # And: Verify agent status is PENDING
    assert agent.status == AgentStatus.PENDING, "New agent status must be PENDING"


def test_task_assignment() -> None:
    """Test that task assignment creates proper events and transitions state."""

    # Given: Create a BOSS agent
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }

    agent = AgentSession.create(
        agent_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )

    # When: Assign a task to the agent
    task_description = "Build a snake game"
    agent.assign_task(task_description)

    # Then: Verify 2 events exist (AgentCreated + TaskAssigned)
    assert len(agent.events) == 2, "Agent should have 2 uncommitted events (Created + Assigned)"

    last_event = agent.events[-1]
    assert isinstance(last_event, TaskAssigned), "Last event must be TaskAssigned"
    assert last_event.task_description == task_description, "Event task must match input"
    assert last_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"

    # And: Verify status transitioned to ANALYZING
    assert agent.status == AgentStatus.ANALYZING, (
        "Status must transition to ANALYZING after task assignment"
    )

    # And: Verify task_description matches input
    assert agent.task_description == task_description, "Agent task_description must match input"


def test_fail_with_reason() -> None:
    """Test that fail_with_reason marks agent as FAILED with error message."""

    # Given: Create a BOSS agent with a task
    boss_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }

    boss = AgentSession.create(agent_id=boss_id, role=AgentRole.BOSS, config=config)
    boss.assign_task("Some task")

    # When: Mark agent as failed with a reason
    boss.fail_with_reason("LLM authentication failed after 3 retries")

    # Then: Agent status transitions to FAILED
    assert boss.status == AgentStatus.FAILED

    # And: error_message is set
    assert boss.error_message == "LLM authentication failed after 3 retries"

    # And: WorkFailed event is recorded
    from core.domain.events.events import WorkFailed

    failed_events = [e for e in boss.events if isinstance(e, WorkFailed)]
    assert len(failed_events) == 1
    assert failed_events[0].reason == "LLM authentication failed after 3 retries"

    # And: Agent is terminal
    assert boss.is_terminal() is True
