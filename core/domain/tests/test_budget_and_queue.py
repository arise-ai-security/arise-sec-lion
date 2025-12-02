"""Test cases for AgentSession budget tracking and task queue management.

Tests follow Test-Driven Development principles with Given-When-Then structure.
"""

from uuid import uuid4

import pytest

from core.domain.events import BudgetAdjusted, BudgetAllocated, TaskDequeued, TaskEnqueued
from core.domain.model import AgentRole, AgentSession
from core.domain.subtask import Subtask


# Budget Tracking Tests


def test_budget_allocation_creates_event() -> None:
    """Test that budget allocation creates proper event and updates state."""

    # Given: Create a BOSS agent
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )

    # When: Allocate budget to the agent
    agent.allocate_budget(amount=100.0, source="initial")

    # Then: Verify BudgetAllocated event was created
    budget_events = [e for e in agent.events if isinstance(e, BudgetAllocated)]
    assert len(budget_events) == 1, "Should have exactly 1 BudgetAllocated event"

    budget_event = budget_events[0]
    assert budget_event.amount == 100.0, "Event amount must match input"
    assert budget_event.source == "initial", "Event source must match input"
    assert budget_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"

    # And: Verify agent budget was updated
    assert agent.current_budget == 100.0, "Agent budget must be updated to 100.0"


def test_budget_allocation_negative_amount_raises_error() -> None:
    """Test that allocating negative budget raises ValueError."""

    # Given: Create a BOSS agent
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )

    # When/Then: Allocating negative budget raises ValueError
    with pytest.raises(ValueError, match="Budget amount must be non-negative"):
        agent.allocate_budget(amount=-50.0)


def test_budget_adjustment_positive() -> None:
    """Test that positive budget adjustment increases balance."""

    # Given: Create an agent with initial budget
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )
    agent.allocate_budget(amount=100.0, source="initial")

    # When: Adjust budget positively (reward for successful child)
    agent.adjust_budget(adjustment=25.0, reason="Child agent succeeded")

    # Then: Verify BudgetAdjusted event was created
    adjustment_events = [e for e in agent.events if isinstance(e, BudgetAdjusted)]
    assert len(adjustment_events) == 1, "Should have exactly 1 BudgetAdjusted event"

    adjustment_event = adjustment_events[0]
    assert adjustment_event.adjustment == 25.0, "Event adjustment must match input"
    assert adjustment_event.reason == "Child agent succeeded", "Event reason must match input"
    assert adjustment_event.new_balance == 125.0, "Event new_balance must be 125.0"
    assert adjustment_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"

    # And: Verify agent budget was updated
    assert agent.current_budget == 125.0, "Agent budget must be updated to 125.0"


def test_budget_adjustment_negative() -> None:
    """Test that negative budget adjustment decreases balance."""

    # Given: Create an agent with initial budget
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )
    agent.allocate_budget(amount=100.0, source="initial")

    # When: Adjust budget negatively (penalty for failed child)
    agent.adjust_budget(adjustment=-30.0, reason="Child agent failed")

    # Then: Verify BudgetAdjusted event was created
    adjustment_events = [e for e in agent.events if isinstance(e, BudgetAdjusted)]
    assert len(adjustment_events) == 1, "Should have exactly 1 BudgetAdjusted event"

    adjustment_event = adjustment_events[0]
    assert adjustment_event.adjustment == -30.0, "Event adjustment must match input"
    assert adjustment_event.reason == "Child agent failed", "Event reason must match input"
    assert adjustment_event.new_balance == 70.0, "Event new_balance must be 70.0"

    # And: Verify agent budget was updated
    assert agent.current_budget == 70.0, "Agent budget must be updated to 70.0"


def test_budget_adjustment_can_go_negative() -> None:
    """Test that budget can go negative (debt scenario)."""

    # Given: Create an agent with small initial budget
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )
    agent.allocate_budget(amount=10.0, source="initial")

    # When: Apply large negative adjustment
    agent.adjust_budget(adjustment=-50.0, reason="Multiple failures")

    # Then: Verify budget went negative
    assert agent.current_budget == -40.0, "Budget can go negative to track debt"


def test_multiple_budget_adjustments() -> None:
    """Test that multiple budget adjustments compound correctly."""

    # Given: Create an agent with initial budget
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )
    agent.allocate_budget(amount=100.0, source="initial")

    # When: Apply multiple adjustments
    agent.adjust_budget(adjustment=25.0, reason="Success 1")
    agent.adjust_budget(adjustment=-10.0, reason="Failure 1")
    agent.adjust_budget(adjustment=15.0, reason="Success 2")

    # Then: Verify final budget is correct
    assert agent.current_budget == 130.0, "Budget must be 100 + 25 - 10 + 15 = 130"

    # And: Verify all adjustment events were recorded
    adjustment_events = [e for e in agent.events if isinstance(e, BudgetAdjusted)]
    assert len(adjustment_events) == 3, "Should have 3 BudgetAdjusted events"


# Task Queue Tests


def test_enqueue_task_creates_event() -> None:
    """Test that enqueuing a task creates proper event and updates queue."""

    # Given: Create a MANAGER agent
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    subtask_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }
    subtask = Subtask(description="Implement authentication", config=subtask_config)

    # When: Enqueue the subtask
    agent.enqueue_task(subtask)

    # Then: Verify TaskEnqueued event was created
    enqueue_events = [e for e in agent.events if isinstance(e, TaskEnqueued)]
    assert len(enqueue_events) == 1, "Should have exactly 1 TaskEnqueued event"

    enqueue_event = enqueue_events[0]
    assert enqueue_event.subtask == subtask, "Event subtask must match input"
    assert enqueue_event.aggregate_id == agent_id, "Event aggregate_id must match agent id"

    # And: Verify task was added to queue
    assert len(agent.task_queue) == 1, "Queue should have 1 task"
    assert agent.task_queue[0] == subtask, "Queue task must match input"


def test_dequeue_task_removes_first_task() -> None:
    """Test that dequeuing removes and returns the first task (FIFO)."""

    # Given: Create an agent with multiple tasks in queue
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    subtask_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }

    task1 = Subtask(description="Task 1", config=subtask_config)
    task2 = Subtask(description="Task 2", config=subtask_config)
    task3 = Subtask(description="Task 3", config=subtask_config)

    agent.enqueue_task(task1)
    agent.enqueue_task(task2)
    agent.enqueue_task(task3)

    # When: Dequeue a task
    dequeued = agent.dequeue_task()

    # Then: Verify first task was returned
    assert dequeued == task1, "Should return first task (FIFO)"

    # And: Verify TaskDequeued event was created
    dequeue_events = [e for e in agent.events if isinstance(e, TaskDequeued)]
    assert len(dequeue_events) == 1, "Should have exactly 1 TaskDequeued event"

    dequeue_event = dequeue_events[0]
    assert dequeue_event.subtask == task1, "Event subtask must match dequeued task"

    # And: Verify task was removed from queue
    assert len(agent.task_queue) == 2, "Queue should have 2 tasks remaining"
    assert agent.task_queue[0] == task2, "First task in queue should now be task2"
    assert agent.task_queue[1] == task3, "Second task in queue should be task3"


def test_dequeue_empty_queue_returns_none() -> None:
    """Test that dequeuing from empty queue returns None."""

    # Given: Create an agent with empty queue
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    # When: Dequeue from empty queue
    dequeued = agent.dequeue_task()

    # Then: Verify None was returned
    assert dequeued is None, "Dequeuing empty queue should return None"

    # And: Verify no TaskDequeued event was created
    dequeue_events = [e for e in agent.events if isinstance(e, TaskDequeued)]
    assert len(dequeue_events) == 0, "Should have no TaskDequeued events"


def test_peek_next_task_does_not_remove() -> None:
    """Test that peeking at next task does not remove it from queue."""

    # Given: Create an agent with tasks in queue
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    subtask_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }

    task1 = Subtask(description="Task 1", config=subtask_config)
    task2 = Subtask(description="Task 2", config=subtask_config)

    agent.enqueue_task(task1)
    agent.enqueue_task(task2)

    # When: Peek at next task
    peeked = agent.peek_next_task()

    # Then: Verify first task was returned
    assert peeked == task1, "Should return first task"

    # And: Verify queue was not modified
    assert len(agent.task_queue) == 2, "Queue should still have 2 tasks"
    assert agent.task_queue[0] == task1, "First task should still be task1"

    # And: Verify no TaskDequeued event was created
    dequeue_events = [e for e in agent.events if isinstance(e, TaskDequeued)]
    assert len(dequeue_events) == 0, "Should have no TaskDequeued events"


def test_peek_empty_queue_returns_none() -> None:
    """Test that peeking at empty queue returns None."""

    # Given: Create an agent with empty queue
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    # When: Peek at empty queue
    peeked = agent.peek_next_task()

    # Then: Verify None was returned
    assert peeked is None, "Peeking empty queue should return None"


def test_has_pending_tasks_returns_correct_state() -> None:
    """Test that has_pending_tasks reflects queue state correctly."""

    # Given: Create an agent with empty queue
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    # When/Then: Empty queue returns False
    assert agent.has_pending_tasks() is False, "Empty queue should return False"

    # When: Add a task to queue
    subtask_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }
    task = Subtask(description="Task 1", config=subtask_config)
    agent.enqueue_task(task)

    # Then: Queue with tasks returns True
    assert agent.has_pending_tasks() is True, "Queue with tasks should return True"

    # When: Dequeue the task
    agent.dequeue_task()

    # Then: Empty queue returns False again
    assert agent.has_pending_tasks() is False, "Empty queue should return False"


def test_event_sourcing_budget_reconstruction() -> None:
    """Test that budget state is correctly reconstructed from events."""

    # Given: Create an agent and perform budget operations
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.BOSS, config=config, parent_id=None
    )
    agent.allocate_budget(amount=100.0, source="initial")
    agent.adjust_budget(adjustment=25.0, reason="Success")
    agent.adjust_budget(adjustment=-15.0, reason="Failure")

    # When: Reconstruct agent from event history
    events = agent.events
    reconstructed = AgentSession.load_from_history(events)

    # Then: Verify budget state matches original
    assert reconstructed.current_budget == 110.0, "Reconstructed budget must match original"
    assert reconstructed.session_id == agent_id, "Reconstructed session_id must match"


def test_event_sourcing_task_queue_reconstruction() -> None:
    """Test that task queue state is correctly reconstructed from events."""

    # Given: Create an agent and perform queue operations
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    subtask_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }

    task1 = Subtask(description="Task 1", config=subtask_config)
    task2 = Subtask(description="Task 2", config=subtask_config)
    task3 = Subtask(description="Task 3", config=subtask_config)

    agent.enqueue_task(task1)
    agent.enqueue_task(task2)
    agent.enqueue_task(task3)
    agent.dequeue_task()  # Remove task1

    # When: Reconstruct agent from event history
    events = agent.events
    reconstructed = AgentSession.load_from_history(events)

    # Then: Verify queue state matches original
    assert len(reconstructed.task_queue) == 2, "Reconstructed queue should have 2 tasks"
    assert reconstructed.task_queue[0] == task2, "First task should be task2"
    assert reconstructed.task_queue[1] == task3, "Second task should be task3"
