"""Test cases for Worker Agent execution logic (TDD approach).

This module tests the WORKER agent's ability to execute tasks using
external tools (Claude Code, OpenHands) and capture their thinking process.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.services import PromptBuilder
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
)
from core.domain.values.llm_response import LLMResponse, LLMUsage
from core.ports.runtime_ports import LLMPort, WorkerToolPort


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
                aggregate_id=task_context.get("agent_id", uuid4()),
                sequence_number=0,  # Will be overridden by aggregate
                content=thought,
                stream="tool",
            )

        # Yield completion event
        yield WorkCompleted(
            aggregate_id=task_context.get("agent_id", uuid4()),
            sequence_number=0,  # Will be overridden by aggregate
            result=self.result,
        )


class FakeLLM(LLMPort):
    """Stub LLM for tests that don't need LLM calls."""

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """No-op implementation."""
        return "{}"

    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        """No-op implementation."""
        return LLMResponse(
            content="{}",
            usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model="fake-model",
            cost_usd=0.0,
        )


class FakeChildAgentFactory:
    """Fake child factory for testing (tracks agent counts)."""

    def __init__(self, max_total_agents: int = -1) -> None:
        self._total_created = 1  # Start with 1 (root agent)
        self._max_total_agents = max_total_agents

    @property
    def total_created(self) -> int:
        return self._total_created

    @property
    def max_total_agents(self) -> int:
        return self._max_total_agents

    def get_sibling_role_prefixes(self, agent_id, parent_id) -> set[str]:
        return set()

    def get_tree_role_prefixes(self, agent_id) -> set[str]:
        return set()

    def get_completed_role_prefixes(self, agent_id) -> set[str]:
        return set()

    def get_failed_role_prefixes(self, agent_id) -> set[str]:
        return set()

    def mark_role_completed(self, agent_id) -> None:
        return None

    def mark_role_failed(self, agent_id) -> None:
        return None

    async def try_reserve(self, n: int) -> bool:
        return True

    async def commit_reservation(self, n: int) -> None:
        self._total_created += n

    async def release_reservation(self, n: int) -> None:
        return None


def _create_orchestrator(
    worker_port: WorkerToolPort,
    llm_port: LLMPort | None = None,
    prompt_builder: PromptBuilder | None = None,
    child_factory: FakeChildAgentFactory | None = None,
) -> AgentOrchestrator:
    """Create an orchestrator with the given ports."""
    return AgentOrchestrator(
        llm_port=llm_port or FakeLLM(),
        worker_port=worker_port,
        prompt_builder=prompt_builder or PromptBuilder("prompts", "claude_code"),
        child_factory=child_factory or FakeChildAgentFactory(),
    )


@pytest.mark.asyncio
async def test_worker_execution_flow() -> None:
    """Test that a WORKER agent can execute tasks using a worker tool."""

    # Given: Create a WORKER agent with a task
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }

    agent = AgentSession.create(
        agent_id=agent_id, role=AgentRole.WORKER, config=config, parent_id=uuid4()
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
    orchestrator = _create_orchestrator(worker_port=fake_tool)

    # When: Call orchestrator.execute_task with the agent
    await orchestrator.execute_task(agent)

    # Then: Verify CodeGenerationStarted event is recorded
    code_gen_events = [e for e in agent.events if isinstance(e, CodeGenerationStarted)]
    assert len(code_gen_events) == 1, "Should have 1 CodeGenerationStarted event"
    assert code_gen_events[0].tool_name == "claude_code"

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
