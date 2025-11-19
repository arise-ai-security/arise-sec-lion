"""Test cases for AgentSession aggregate (TDD approach).

These tests are written before the implementation exists, following
Test-Driven Development principles.
"""

from uuid import uuid4

from core.domain.events import AgentCreated, TaskAssigned
from core.domain.model import AgentRole, AgentSession, AgentStatus


def test_boss_initialization_flow() -> None:
    """Test that a BOSS agent is properly initialized with correct events.

    Arrangement:
        - Define UUID and configuration
    Action:
        - Create a new AgentSession
    Assertion:
        - Verify uncommitted events contain exactly 1 AgentCreated event
        - Verify agent status is PENDING
    """
    # Arrangement
    agent_id = uuid4()
    config = {"model": "gpt-4", "temperature": 0.7}

    # Action
    agent = AgentSession(id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None)

    # Assertion (Event Sourcing Check)
    assert len(agent.events) == 1, "Agent should have exactly 1 uncommitted event"

    first_event = agent.events[0]
    assert isinstance(first_event, AgentCreated), "First event must be AgentCreated"
    assert first_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"
    assert first_event.role == AgentRole.BOSS.value, "Event role must be BOSS"
    assert first_event.parent_id is None, "BOSS has no parent"
    assert first_event.config == config, "Event config must match input"

    assert agent.status == AgentStatus.PENDING, "New agent status must be PENDING"


def test_task_assignment() -> None:
    """Test that task assignment creates proper events and transitions state.

    Arrangement:
        - Create a BOSS agent
    Action:
        - Assign a task to the agent
    Assertion:
        - Verify 2 events exist (AgentCreated + TaskAssigned)
        - Verify last event is TaskAssigned
        - Verify status transitioned to ANALYZING
        - Verify task_description matches input
    """
    # Arrangement
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession(id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None)

    # Action
    task_description = "Build a snake game"
    agent.assign_task(task_description)

    # Assertion
    assert len(agent.events) == 2, "Agent should have 2 uncommitted events (Created + Assigned)"

    last_event = agent.events[-1]
    assert isinstance(last_event, TaskAssigned), "Last event must be TaskAssigned"
    assert last_event.task_description == task_description, "Event task must match input"
    assert last_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"

    assert agent.status == AgentStatus.ANALYZING, (
        "Status must transition to ANALYZING after task assignment"
    )
    assert agent.task_description == task_description, "Agent task_description must match input"
