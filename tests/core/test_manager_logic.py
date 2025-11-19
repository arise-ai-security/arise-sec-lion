"""Test cases for Manager Agent decomposition logic (TDD approach).

This module tests the MANAGER agent's ability to decompose tasks into
subtasks and spawn child agents.
"""

import json
from typing import Any
from uuid import uuid4

import pytest

from core.domain.events import (
    ChildSpawned,
    StatusChanged,
    SubtasksDefined,
    WorkCompleted,
    WorkFailed,
)
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.subtask import Subtask
from core.ports.llm_port import LLMPort


class FakeLLM(LLMPort):
    """Fake LLM implementation for testing.

    Returns canned responses without calling real LLM APIs.
    """

    def __init__(self, canned_response: list[dict[str, Any]] | str) -> None:
        """Initialize with a canned response.

        Args:
            canned_response: Either a list of subtask dictionaries to return,
                           or a raw string response (for testing invalid JSON).
        """
        self.canned_response = canned_response

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Return canned response as string.

        Args:
            prompt: The prompt (ignored in fake).
            config_dict: Configuration (ignored in fake).

        Returns:
            String response (JSON if canned_response is list, raw string otherwise).
        """
        if isinstance(self.canned_response, str):
            return self.canned_response
        return json.dumps(self.canned_response)


@pytest.mark.asyncio
async def test_manager_decomposition() -> None:
    """Test that a MANAGER agent can decompose tasks into subtasks."""

    # Given: Create a MANAGER agent with ANALYZING status
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    # Create MANAGER agent
    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    # Assign a task (transitions to ANALYZING)
    agent.assign_task("Build a web scraper for news articles")

    # And: Prepare a FakeLLM that returns two subtasks
    fake_llm = FakeLLM(
        canned_response=[
            {"description": "Research BeautifulSoup and Scrapy libraries"},
            {"description": "Implement URL fetching and HTML parsing"},
        ]
    )

    # When: Call agent.evaluate_task(llm_port)
    await agent.evaluate_task(llm_port=fake_llm)

    # Then: Verify 6 events exist
    # (AgentCreated, TaskAssigned, SubtasksDefined, ChildSpawned x2, StatusChanged)
    assert len(agent.events) == 6, f"Expected 6 events, got {len(agent.events)}"

    # And: Find specific events
    subtasks_event = None
    child_spawned_events = []

    for event in agent.events:
        if isinstance(event, SubtasksDefined):
            subtasks_event = event
        elif isinstance(event, ChildSpawned):
            child_spawned_events.append(event)

    # And: Verify SubtasksDefined event is recorded
    assert subtasks_event is not None, "SubtasksDefined event must be present"
    assert len(subtasks_event.subtasks) == 2, "Should have 2 subtasks"

    # Verify subtasks are Subtask value objects (not dicts)
    assert all(isinstance(st, Subtask) for st in subtasks_event.subtasks)
    assert subtasks_event.subtasks[0].description == "Research BeautifulSoup and Scrapy libraries"
    assert subtasks_event.subtasks[1].description == "Implement URL fetching and HTML parsing"

    # And: Verify two ChildSpawned events are recorded (one per subtask)
    assert len(child_spawned_events) == 2, "Should have 2 ChildSpawned events"
    # Children now spawn with PENDING role (will evaluate complexity themselves)
    assert child_spawned_events[0].child_role == AgentRole.PENDING.value
    assert child_spawned_events[1].child_role == AgentRole.PENDING.value

    # And: Verify each ChildSpawned event contains Subtask value object
    assert isinstance(child_spawned_events[0].subtask, Subtask)
    assert isinstance(child_spawned_events[1].subtask, Subtask)
    assert (
        child_spawned_events[0].subtask.description == "Research BeautifulSoup and Scrapy libraries"
    )
    assert child_spawned_events[1].subtask.description == "Implement URL fetching and HTML parsing"

    # And: Verify agent status transitions to WAITING
    assert agent.status == AgentStatus.WAITING, (
        "Status should transition to WAITING after decomposition"
    )
    assert len(agent.child_ids) == 2, "Agent should track 2 child IDs"


@pytest.mark.asyncio
async def test_manager_llm_invalid_json_response() -> None:
    """Test that MANAGER handles LLM returning invalid JSON by publishing WorkFailed event."""

    # Given: Create a MANAGER agent with ANALYZING status
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )
    agent.assign_task("Build a web scraper for news articles")

    # And: Prepare a FakeLLM that returns invalid JSON
    fake_llm = FakeLLM(canned_response="This is not JSON at all!")

    # When: Call agent.evaluate_task(llm_port) with invalid JSON response
    await agent.evaluate_task(llm_port=fake_llm)

    # Then: Verify 3 events exist (AgentCreated, TaskAssigned, WorkFailed)
    assert len(agent.events) == 3, f"Expected 3 events, got {len(agent.events)}"

    # And: Find the WorkFailed event
    work_failed_event = None
    for event in agent.events:
        if isinstance(event, WorkFailed):
            work_failed_event = event

    # And: Verify WorkFailed event was recorded
    assert work_failed_event is not None, "WorkFailed event must be present"
    # Error message from SubtaskParser mentions "parse" or "json"
    reason_lower = work_failed_event.reason.lower()
    assert "parse" in reason_lower or "json" in reason_lower

    # And: Verify NO SubtasksDefined or ChildSpawned events were created
    subtasks_events = [e for e in agent.events if isinstance(e, SubtasksDefined)]
    child_spawned_events = [e for e in agent.events if isinstance(e, ChildSpawned)]
    assert len(subtasks_events) == 0, "Should have 0 SubtasksDefined events on failure"
    assert len(child_spawned_events) == 0, "Should have 0 ChildSpawned events on failure"

    # And: Verify agent status transitions to FAILED
    assert agent.status == AgentStatus.FAILED, "Status should transition to FAILED on LLM error"

    # And: Verify error_message is set
    assert agent.error_message is not None, "error_message should be set"
    assert len(agent.error_message) > 0, "error_message should not be empty"


@pytest.mark.asyncio
async def test_parent_completion_check() -> None:
    """Test that a MANAGER waits for all children before completing."""

    # Given: Create a MANAGER agent
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )
    agent.assign_task("Build a web scraper for news articles")

    # And: Manually simulate that it spawned 2 children
    child_a_id = uuid4()
    child_b_id = uuid4()

    # Create child A (spawned with PENDING role)
    child_a_event = ChildSpawned(
        aggregate_id=agent_id,
        sequence_number=3,
        child_id=child_a_id,
        child_role=AgentRole.PENDING.value,
        subtask=Subtask(description="Research BeautifulSoup library"),
    )
    agent._apply(child_a_event)
    agent._changes.append(child_a_event)

    # Create child B (spawned with PENDING role)
    child_b_event = ChildSpawned(
        aggregate_id=agent_id,
        sequence_number=4,
        child_id=child_b_id,
        child_role=AgentRole.PENDING.value,
        subtask=Subtask(description="Implement URL fetching"),
    )
    agent._apply(child_b_event)
    agent._changes.append(child_b_event)

    # And: Set status to WAITING
    status_event = StatusChanged(
        aggregate_id=agent_id,
        sequence_number=5,
        old_status=AgentStatus.ANALYZING.value,
        new_status=AgentStatus.WAITING.value,
        reason="Waiting for children to complete",
    )
    agent._apply(status_event)
    agent._changes.append(status_event)

    # Clear changes to focus on handle_child_update events
    initial_event_count = len(agent.events)

    # When: Child A finishes
    agent.handle_child_update(
        child_id=child_a_id,
        result="Successfully researched BeautifulSoup library",
    )

    # Then: Agent status is STILL WAITING (because Child B is not done)
    assert agent.status == AgentStatus.WAITING, (
        "Status should remain WAITING when not all children are complete"
    )

    # And: No WorkCompleted event yet
    work_completed_events = [
        e for e in agent.events[initial_event_count:] if isinstance(e, WorkCompleted)
    ]
    assert len(work_completed_events) == 0, "Should not complete until all children done"

    # When: Child B finishes
    agent.handle_child_update(
        child_id=child_b_id,
        result="Successfully implemented URL fetching",
    )

    # Then: Agent status transitions to COMPLETED
    assert agent.status == AgentStatus.COMPLETED, (
        "Status should transition to COMPLETED when all children are done"
    )

    # And: A WorkCompleted event is generated with aggregated results
    work_completed_events = [
        e for e in agent.events[initial_event_count:] if isinstance(e, WorkCompleted)
    ]
    assert len(work_completed_events) == 1, "Should have 1 WorkCompleted event"

    # And: The result should aggregate results from both children
    result = work_completed_events[0].result
    assert "BeautifulSoup" in result, "Result should include Child A's work"
    assert "URL fetching" in result, "Result should include Child B's work"

    # And: Agent result is set
    assert agent.result is not None
    assert "BeautifulSoup" in agent.result
    assert "URL fetching" in agent.result


def test_cannot_spawn_boss_child() -> None:
    """Test that system invariant prevents spawning BOSS children."""

    # Given: Create a MANAGER agent
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.MANAGER, config=config, parent_id=uuid4()
    )

    # When/Then: Attempting to spawn a BOSS child raises AssertionError
    # Try to create ChildSpawned event with BOSS role (invalid)
    invalid_event = ChildSpawned(
        aggregate_id=agent_id,
        sequence_number=3,
        child_id=uuid4(),
        child_role=AgentRole.BOSS.value,
        subtask=Subtask(description="Some task"),
    )
    # Applying this event should fail the invariant check
    with pytest.raises(AssertionError, match=r"(?i)(boss|invariant)"):
        agent._apply(invalid_event)


@pytest.mark.asyncio
async def test_child_evaluates_simple_complexity() -> None:
    """Test that child agent evaluates SIMPLE task and becomes WORKER."""

    # Given: Create a child agent with PENDING role
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.PENDING, config=config, parent_id=uuid4()
    )
    agent.assign_task("Write a Python function to calculate fibonacci numbers")

    # And: Prepare FakeLLM that returns SIMPLE complexity
    import json

    fake_llm = FakeLLM(
        canned_response=json.dumps(
            {
                "complexity": "simple",
                "reasoning": "This is a single, focused task that can be implemented directly",
            }
        )
    )

    # When: Child evaluates complexity
    await agent.evaluate_complexity(llm_port=fake_llm)

    # Then: Agent role transitions from PENDING to WORKER
    assert agent.role == AgentRole.WORKER, "Agent should become WORKER for SIMPLE tasks"

    # And: ComplexityEvaluated event is recorded
    from core.domain.events import ComplexityEvaluated

    complexity_events = [e for e in agent.events if isinstance(e, ComplexityEvaluated)]
    assert len(complexity_events) == 1, "Should have 1 ComplexityEvaluated event"
    assert complexity_events[0].complexity == "simple"
    assert complexity_events[0].determined_role == AgentRole.WORKER.value
    assert "single" in complexity_events[0].reasoning.lower()

    # And: Agent status remains ANALYZING (ready to execute)
    assert agent.status == AgentStatus.ANALYZING


@pytest.mark.asyncio
async def test_child_evaluates_complex_complexity() -> None:
    """Test that child agent evaluates COMPLEX task and becomes MANAGER."""

    # Given: Create a child agent with PENDING role
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.PENDING, config=config, parent_id=uuid4()
    )
    agent.assign_task("Build a complete web scraping system with monitoring")

    # And: Prepare FakeLLM that returns COMPLEX complexity
    import json

    fake_llm = FakeLLM(
        canned_response=json.dumps(
            {
                "complexity": "complex",
                "reasoning": "Requires multiple subtasks: scraping, storage, monitoring, alerting",
            }
        )
    )

    # When: Child evaluates complexity
    await agent.evaluate_complexity(llm_port=fake_llm)

    # Then: Agent role transitions from PENDING to MANAGER
    assert agent.role == AgentRole.MANAGER, "Agent should become MANAGER for COMPLEX tasks"

    # And: ComplexityEvaluated event is recorded
    from core.domain.events import ComplexityEvaluated

    complexity_events = [e for e in agent.events if isinstance(e, ComplexityEvaluated)]
    assert len(complexity_events) == 1, "Should have 1 ComplexityEvaluated event"
    assert complexity_events[0].complexity == "complex"
    assert complexity_events[0].determined_role == AgentRole.MANAGER.value
    assert "multiple" in complexity_events[0].reasoning.lower()

    # And: Agent status remains ANALYZING (ready to decompose)
    assert agent.status == AgentStatus.ANALYZING


@pytest.mark.asyncio
async def test_complexity_evaluation_invalid_response() -> None:
    """Test that invalid LLM response for complexity evaluation fails gracefully."""

    # Given: Create a child agent with PENDING role
    agent_id = uuid4()
    config = {"model": "gpt-4"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.PENDING, config=config, parent_id=uuid4()
    )
    agent.assign_task("Some task")

    # And: Prepare FakeLLM that returns invalid JSON
    fake_llm = FakeLLM(canned_response="This is not valid JSON!")

    # When: Child attempts to evaluate complexity
    await agent.evaluate_complexity(llm_port=fake_llm)

    # Then: Agent transitions to FAILED status
    assert agent.status == AgentStatus.FAILED, "Agent should fail when complexity evaluation fails"

    # And: WorkFailed event is recorded
    work_failed_events = [e for e in agent.events if isinstance(e, WorkFailed)]
    assert len(work_failed_events) == 1, "Should have 1 WorkFailed event"
    assert "complexity" in work_failed_events[0].reason.lower()

    # And: Role remains PENDING (never determined)
    assert agent.role == AgentRole.PENDING
