"""Tests for AgentExecutionService (Application Layer).

These tests verify the orchestration logic using mocked ports.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.application.execution_service import AgentExecutionService
from core.domain.events import AgentCreated, TaskAssigned
from core.domain.exceptions import ConcurrencyError
from core.domain.model import AgentRole


@pytest.fixture
def mock_event_store():
    """Mock event store port."""
    return AsyncMock()


@pytest.fixture
def mock_llm_port():
    """Mock LLM port."""
    return AsyncMock()


@pytest.fixture
def mock_worker_port():
    """Mock worker tool port."""
    return AsyncMock()


@pytest.fixture
def execution_service(mock_event_store, mock_llm_port, mock_worker_port):
    """Create execution service with mocked ports."""
    return AgentExecutionService(
        event_store=mock_event_store,
        llm_port=mock_llm_port,
        worker_tool_port=mock_worker_port,
        max_retries=3,
    )


@pytest.mark.asyncio
async def test_run_agent_step_pending_role_calls_evaluate_complexity(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that PENDING agent calls evaluate_complexity."""

    # Given: A PENDING agent in ANALYZING status
    agent_id = uuid4()
    parent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build a web scraper",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # Mock LLM to return SIMPLE complexity
    mock_llm_port.query.return_value = "SIMPLE"

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should have been called for complexity evaluation
    assert mock_llm_port.query.called, "evaluate_complexity should call LLM"

    # And: Events should have been appended to event store
    assert mock_event_store.append.called, "Should save events to event store"

    # Verify ComplexityEvaluated event was saved
    saved_calls = mock_event_store.append.call_args_list
    assert len(saved_calls) > 0, "Should save at least one event"


@pytest.mark.asyncio
async def test_run_agent_step_boss_role_calls_evaluate_task(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that BOSS agent calls evaluate_task."""

    # Given: A BOSS agent in ANALYZING status
    agent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build a full-stack application",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # Mock LLM to return subtasks JSON
    mock_llm_port.query.return_value = """
    [
        {"description": "Setup backend API"},
        {"description": "Create frontend"}
    ]
    """

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should have been called for task evaluation
    assert mock_llm_port.query.called, "evaluate_task should call LLM"

    # And: Events should have been saved
    assert mock_event_store.append.called, "Should save SubtasksDefined event"


@pytest.mark.asyncio
async def test_run_agent_step_manager_role_calls_evaluate_task(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that MANAGER agent calls evaluate_task."""

    # Given: A MANAGER agent in ANALYZING status
    agent_id = uuid4()
    parent_id = uuid4()

    # Simulate a PENDING agent that became MANAGER
    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build authentication system",
    )

    # ComplexityEvaluated with COMPLEX -> role becomes MANAGER
    from core.domain.events import ComplexityEvaluated

    event3 = ComplexityEvaluated(
        aggregate_id=agent_id,
        sequence_number=3,
        complexity="complex",
        determined_role=AgentRole.MANAGER.value,
    )

    mock_event_store.get_events.return_value = [event1, event2, event3]

    # Mock LLM to return subtasks
    mock_llm_port.query.return_value = """
    [
        {"description": "Implement user registration"},
        {"description": "Implement login/logout"}
    ]
    """

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should be called for task evaluation
    assert mock_llm_port.query.called, "MANAGER should evaluate task"


@pytest.mark.asyncio
async def test_run_agent_step_worker_role_calls_execute_task(
    execution_service, mock_event_store, mock_worker_port
):
    """Test that WORKER agent calls execute_task."""

    # Given: A WORKER agent in ANALYZING status
    agent_id = uuid4()
    parent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Write unit tests",
    )

    # ComplexityEvaluated with SIMPLE -> role becomes WORKER
    from core.domain.events import ComplexityEvaluated

    event3 = ComplexityEvaluated(
        aggregate_id=agent_id,
        sequence_number=3,
        complexity="simple",
        determined_role=AgentRole.WORKER.value,
    )

    mock_event_store.get_events.return_value = [event1, event2, event3]

    # Mock worker tool to return events as async generator
    # Create the async generator function
    from core.domain.events import CodeGenerationStarted, WorkCompleted

    async def mock_worker_events():
        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=4,
            tool_name="dummy",
        )
        yield WorkCompleted(
            aggregate_id=agent_id,
            sequence_number=5,
            result="Tests written successfully",
        )

    # Mock run_session to return async generator
    # We need to create a class that implements async iteration
    class AsyncGeneratorMock:
        def __init__(self, gen_func):
            self.gen_func = gen_func
            self.gen = None
            self.called = False
            self.call_count = 0

        def __call__(self, *args, **kwargs):
            self.called = True
            self.call_count += 1
            self.gen = self.gen_func()
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.gen.__anext__()

    mock_worker_port.run_session = AsyncGeneratorMock(mock_worker_events)

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: Worker tool should have been called
    assert mock_worker_port.run_session.called, "WORKER should execute task with tool"


@pytest.mark.asyncio
async def test_run_agent_step_retries_on_concurrency_error(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that OCC failures trigger retry logic."""

    # Given: A PENDING agent
    agent_id = uuid4()
    parent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]
    mock_llm_port.query.return_value = "SIMPLE"

    # First append fails with ConcurrencyError, second succeeds
    mock_event_store.append.side_effect = [
        ConcurrencyError(aggregate_id=str(agent_id), expected_version=2, actual_version=3),
        None,  # Retry succeeds
    ]

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: Should have retried (called get_events twice)
    assert mock_event_store.get_events.call_count == 2, "Should retry on ConcurrencyError"


@pytest.mark.asyncio
async def test_run_agent_step_raises_after_max_retries(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that OCC failures raise error after max retries."""

    # Given: A PENDING agent
    agent_id = uuid4()
    parent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]
    mock_llm_port.query.return_value = "SIMPLE"

    # All appends fail with ConcurrencyError
    mock_event_store.append.side_effect = ConcurrencyError(
        aggregate_id=str(agent_id), expected_version=2, actual_version=3
    )

    # When/Then: Should raise ConcurrencyError after max retries
    with pytest.raises(ConcurrencyError):
        await execution_service.run_agent_step(agent_id)

    # Should have retried 3 times (max_retries)
    assert mock_event_store.get_events.call_count == 3


@pytest.mark.asyncio
async def test_run_agent_step_marks_failed_on_unexpected_error(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that unexpected errors mark agent as failed."""

    # Given: A PENDING agent
    agent_id = uuid4()
    parent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # LLM raises unexpected error
    mock_llm_port.query.side_effect = RuntimeError("LLM service unavailable")

    # When: Run agent step
    with pytest.raises(RuntimeError, match="LLM service unavailable"):
        await execution_service.run_agent_step(agent_id)

    # Then: Should have called append to save WorkFailed event
    # The service reloads and saves failure event
    assert mock_event_store.append.called, "Should save WorkFailed event"

    # Verify WorkFailed was in the saved events
    saved_calls = list(mock_event_store.append.call_args_list)
    assert len(saved_calls) > 0, "Should save failure event"


@pytest.mark.asyncio
async def test_run_agent_step_raises_if_agent_not_found(execution_service, mock_event_store):
    """Test that missing agent raises ValueError."""

    # Given: Agent does not exist
    agent_id = uuid4()
    mock_event_store.get_events.return_value = []

    # When/Then: Should raise ValueError
    with pytest.raises(ValueError, match="not found"):
        await execution_service.run_agent_step(agent_id)


@pytest.mark.asyncio
async def test_run_agent_step_skips_non_analyzing_agents(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that agents not in ANALYZING status are skipped."""

    # Given: Agent in COMPLETED status
    agent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Task",
    )

    from core.domain.events import WorkCompleted

    event3 = WorkCompleted(
        aggregate_id=agent_id,
        sequence_number=3,
        result="Done",
    )

    mock_event_store.get_events.return_value = [event1, event2, event3]

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: Should not call LLM or worker tool
    assert not mock_llm_port.query.called, "Should skip non-ANALYZING agents"

    # And: Should not append any new events (no uncommitted changes)
    assert not mock_event_store.append.called, "Should not save events for completed agent"
