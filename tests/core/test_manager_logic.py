"""Test cases for Manager Agent decomposition logic (TDD approach).

This module tests the MANAGER agent's ability to decompose tasks into
subtasks and spawn child agents.
"""

import json
from typing import Any
from uuid import uuid4

import pytest

from core.domain.events import ChildSpawned, SubtasksDefined, WorkFailed
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
    assert child_spawned_events[0].child_role == AgentRole.WORKER.value
    assert child_spawned_events[1].child_role == AgentRole.WORKER.value

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
