"""Test cases for Worker Agent execution logic (TDD approach).

This module tests the WORKER agent's ability to execute tasks using
external tools (Claude Code, OpenHands) and capture their thinking process.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from core.domain.events import (
    CodeGenerationStarted,
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
)
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.ports.worker_port import WorkerToolPort


class FakeWorkerTool(WorkerToolPort):
    """Fake worker tool implementation for testing.

    Simulates a worker tool (Claude Code/OpenHands) execution by yielding
    predefined events without actually running external processes.
    """

    def __init__(
        self,
        thoughts: list[str] | None = None,
        result: str = "Task completed successfully",
    ) -> None:
        """Initialize with predefined thoughts and result.

        Args:
            thoughts: List of thought strings to emit as ThoughtCaptured events.
            result: Final result string for WorkCompleted event.
        """
        self.thoughts = thoughts or [
            "Analyzing the task requirements",
            "Writing code to solve the problem",
            "Testing the solution",
        ]
        self.result = result

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Simulate worker tool execution by yielding events.

        Args:
            task_context: Task execution context (unused in fake).

        Yields:
            ThoughtCaptured events followed by WorkCompleted event.
        """
        # Yield thought events
        for thought in self.thoughts:
            yield ThoughtCaptured(
                aggregate_id=task_context.get("session_id", uuid4()),
                sequence_number=0,  # Will be overridden by aggregate
                content=thought,
                stream="tool",
            )

        # Yield completion event
        yield WorkCompleted(
            aggregate_id=task_context.get("session_id", uuid4()),
            sequence_number=0,  # Will be overridden by aggregate
            result=self.result,
        )


@pytest.mark.asyncio
async def test_worker_execution_flow() -> None:
    """Test that a WORKER agent can execute tasks using a worker tool."""

    # Given: Create a WORKER agent with a task
    agent_id = uuid4()
    config = {"tool": "claude-code"}

    agent = AgentSession.create(
        session_id=agent_id, role=AgentRole.WORKER, config=config, parent_id=uuid4()
    )
    agent.assign_task("Write a Python function to calculate fibonacci numbers")

    # And: Prepare a FakeWorkerTool
    fake_tool = FakeWorkerTool(
        thoughts=[
            "Analyzing the fibonacci requirements",
            "Writing recursive implementation",
            "Adding memoization for efficiency",
        ],
        result="Successfully created fibonacci function with memoization",
    )

    # When: Call agent.execute_task with the fake tool
    await agent.execute_task(tool_port=fake_tool)

    # Then: Verify CodeGenerationStarted event is recorded
    code_gen_events = [e for e in agent.events if isinstance(e, CodeGenerationStarted)]
    assert len(code_gen_events) == 1, "Should have 1 CodeGenerationStarted event"
    assert code_gen_events[0].tool_name == "claude-code"

    # And: Verify ThoughtCaptured events are recorded
    thought_events = [e for e in agent.events if isinstance(e, ThoughtCaptured)]
    assert len(thought_events) == 3, "Should have 3 ThoughtCaptured events"
    assert thought_events[0].content == "Analyzing the fibonacci requirements"
    assert thought_events[1].content == "Writing recursive implementation"
    assert thought_events[2].content == "Adding memoization for efficiency"
    assert all(e.stream == "tool" for e in thought_events)

    # And: Verify WorkCompleted event is recorded
    completed_events = [e for e in agent.events if isinstance(e, WorkCompleted)]
    assert len(completed_events) == 1, "Should have 1 WorkCompleted event"
    assert completed_events[0].result == "Successfully created fibonacci function with memoization"

    # And: Verify agent status transitions to COMPLETED
    assert agent.status == AgentStatus.COMPLETED, (
        "Status should transition to COMPLETED after execution"
    )

    # And: Verify result is stored
    assert agent.result is not None, "Agent result should be set"
    assert "fibonacci function with memoization" in agent.result
