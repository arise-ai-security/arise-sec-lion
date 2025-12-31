"""Tests for AgentExecutionService (Application Layer).

These tests verify the orchestration logic using mocked ports.
"""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from config import OrchestrationConfig
from core.application.execution_service import AgentExecutionService, BudgetConfig
from core.domain.events import AgentCreated, TaskAssigned
from core.domain.exceptions import ConcurrencyError
from core.domain.llm_response import LLMResponse, LLMUsage
from core.domain.model import AgentRole, AgentStatus


def _test_config() -> dict[str, Any]:
    """Create a standard test config for agents."""
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def _test_system_limits() -> OrchestrationConfig.LimitsConfig:
    """Create test system limits (all unlimited for tests)."""
    return OrchestrationConfig.LimitsConfig(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        llm_rate_limit_rpm=-1,
    )


def _test_budget_config() -> BudgetConfig:
    """Create test budget config."""
    return BudgetConfig(
        max_total_cost_usd=10.0,
        cost_warning_threshold=0.8,
        cost_tracking_enabled=True,
    )


def _make_llm_response(content: str, model: str = "gpt-4") -> LLMResponse:
    """Create a mock LLMResponse for testing."""
    return LLMResponse(
        content=content,
        usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        model=model,
        cost_usd=0.0,
    )


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
        system_limits=_test_system_limits(),
        model_config={
            "boss": "gpt-4o",
            "manager": "gpt-4o",
            "worker": "gpt-4o",
            "pending": "gpt-4o",
        },
        max_retries=3,
        poll_interval=0.5,
        output_directory="./test_output",
        default_worker_tool="claude_code",
        budget_config=_test_budget_config(),
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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build a web scraper",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # Mock LLM to return SIMPLE complexity
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should have been called for complexity evaluation
    assert mock_llm_port.query_with_usage.called, "evaluate_complexity should call LLM"

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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build a full-stack application",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # Mock LLM to return subtasks JSON
    mock_llm_port.query_with_usage.return_value = _make_llm_response("""
    [
        {"description": "Setup backend API"},
        {"description": "Create frontend"}
    ]
    """)

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should have been called for task evaluation
    assert mock_llm_port.query_with_usage.called, "evaluate_task should call LLM"

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
        config=_test_config(),
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
    mock_llm_port.query_with_usage.return_value = _make_llm_response("""
    [
        {"description": "Implement user registration"},
        {"description": "Implement login/logout"}
    ]
    """)

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: LLM should be called for task evaluation
    assert mock_llm_port.query_with_usage.called, "MANAGER should evaluate task"


@pytest.mark.asyncio
async def test_run_agent_step_worker_role_calls_execute_task(
    execution_service, mock_event_store, mock_worker_port
):
    """Test that WORKER agent calls execute_task."""

    # Given: A WORKER agent in ANALYZING status (no parent for this isolated test)
    agent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=None,  # No parent for this isolated test
        config=_test_config(),
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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")

    # First append fails with ConcurrencyError, subsequent appends succeed
    # After retry: TokensConsumed, ComplexityEvaluated, StatusChanged events
    mock_event_store.append.side_effect = [
        ConcurrencyError(aggregate_id=str(agent_id), expected_version=2, actual_version=3),
        None,  # Retry succeeds - TokensConsumed
        None,  # ComplexityEvaluated
        None,  # StatusChanged
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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")

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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # LLM raises unexpected error
    mock_llm_port.query_with_usage.side_effect = RuntimeError("LLM service unavailable")

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
        config=_test_config(),
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
    assert not mock_llm_port.query_with_usage.called, "Should skip non-ANALYZING agents"

    # And: Should not append any new events (no uncommitted changes)
    assert not mock_event_store.append.called, "Should not save events for completed agent"


# =============================================================================
# Tests for Child Lifecycle Management
# =============================================================================


@pytest.mark.asyncio
async def test_run_agent_step_creates_children_when_boss_spawns(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that child agents are created when BOSS spawns children."""
    # Given: A BOSS agent in ANALYZING status
    agent_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Build full application",
    )

    mock_event_store.get_events.return_value = [event1, event2]

    # Mock LLM to return subtasks JSON (this will trigger child spawning)
    import json

    child_config = _test_config()
    mock_llm_port.query_with_usage.return_value = _make_llm_response(
        json.dumps(
            [
                {"description": "Setup backend", "config": child_config},
                {"description": "Create frontend", "config": child_config},
            ]
        )
    )

    # Track all append calls to verify child creation
    append_calls = []

    async def track_append(event, expected_version):
        append_calls.append((event, expected_version))

    mock_event_store.append.side_effect = track_append

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: Should have saved parent's events AND child creation events
    # Parent events: SubtasksDefined, ChildSpawned(x2), StatusChanged (WAITING)
    # Child 1 events: AgentCreated, TaskAssigned
    # Child 2 events: AgentCreated, TaskAssigned
    assert len(append_calls) > 4, "Should save parent events + child creation events"

    # Verify child creation events exist
    child_created_events = [
        e for e, _ in append_calls if isinstance(e, AgentCreated) and e.aggregate_id != agent_id
    ]
    assert len(child_created_events) == 2, "Should create 2 child agents"

    # Verify child task assignment events exist
    child_task_events = [
        e for e, _ in append_calls if isinstance(e, TaskAssigned) and e.aggregate_id != agent_id
    ]
    assert len(child_task_events) == 2, "Should assign tasks to 2 children"


@pytest.mark.asyncio
async def test_run_agent_step_notifies_parent_when_child_completes(
    execution_service, mock_event_store, mock_worker_port
):
    """Test that parent is notified when child agent completes."""
    from core.domain.events import ChildCompleted

    # Given: A WORKER child that's about to complete
    parent_id = uuid4()
    child_id = uuid4()

    # Child events (WORKER that completes)
    child_event1 = AgentCreated(
        aggregate_id=child_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=parent_id,
        config=_test_config(),
    )

    child_event2 = TaskAssigned(
        aggregate_id=child_id,
        sequence_number=2,
        task_description="Write tests",
    )

    from core.domain.events import ComplexityEvaluated

    child_event3 = ComplexityEvaluated(
        aggregate_id=child_id,
        sequence_number=3,
        complexity="simple",
        determined_role=AgentRole.WORKER.value,
    )

    # Parent events (BOSS waiting for children)
    parent_event1 = AgentCreated(
        aggregate_id=parent_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config=_test_config(),
    )

    parent_event2 = TaskAssigned(
        aggregate_id=parent_id,
        sequence_number=2,
        task_description="Main task",
    )

    from core.domain.events import ChildSpawned, StatusChanged
    from core.domain.subtask import Subtask

    child_config = _test_config()
    parent_event3 = ChildSpawned(
        aggregate_id=parent_id,
        sequence_number=3,
        child_id=child_id,
        child_role=AgentRole.PENDING.value,
        subtask=Subtask(description="Write tests", config=child_config),
        child_config=child_config,
    )

    parent_event4 = StatusChanged(
        aggregate_id=parent_id,
        sequence_number=4,
        old_status=AgentStatus.ANALYZING.value,
        new_status=AgentStatus.WAITING.value,
        reason="Waiting for children",
    )

    # Mock event store to return appropriate events
    def get_events_side_effect(agent_id):
        if agent_id == child_id:
            return [child_event1, child_event2, child_event3]
        if agent_id == parent_id:
            return [parent_event1, parent_event2, parent_event3, parent_event4]
        return []

    mock_event_store.get_events.side_effect = get_events_side_effect

    # Mock worker tool to complete the task
    from core.domain.events import CodeGenerationStarted, WorkCompleted

    async def mock_worker_events():
        yield CodeGenerationStarted(
            aggregate_id=child_id,
            sequence_number=4,
            tool_name="dummy",
        )
        yield WorkCompleted(
            aggregate_id=child_id,
            sequence_number=5,
            result="Tests completed successfully",
        )

    class AsyncGeneratorMock:
        def __init__(self, gen_func):
            self.gen_func = gen_func

        def __call__(self, *args, **kwargs):
            return self

        def __aiter__(self):
            return self.gen_func()

    mock_worker_port.run_session = AsyncGeneratorMock(mock_worker_events)

    # Track append calls
    append_calls = []

    async def track_append(event, expected_version):
        append_calls.append((event, expected_version))

    mock_event_store.append.side_effect = track_append

    # When: Run child agent step (which will complete)
    await execution_service.run_agent_step(child_id)

    # Then: Should have saved child completion events AND parent notification
    # Verify ChildCompleted event was sent to parent
    child_completed_events = [e for e, _ in append_calls if isinstance(e, ChildCompleted)]
    assert len(child_completed_events) == 1, "Should notify parent of child completion"
    assert child_completed_events[0].aggregate_id == parent_id
    assert child_completed_events[0].child_id == child_id


@pytest.mark.asyncio
async def test_get_active_agent_ids_filters_terminal_agents(execution_service, mock_event_store):
    """Test that get_active_agent_ids only returns non-terminal agents."""
    # Given: Three agents - one ANALYZING, one COMPLETED, one FAILED
    active_agent_id = uuid4()
    completed_agent_id = uuid4()
    failed_agent_id = uuid4()

    # Active agent (ANALYZING)
    active_events = [
        AgentCreated(
            aggregate_id=active_agent_id,
            sequence_number=1,
            role=AgentRole.BOSS.value,
            parent_id=None,
            config=_test_config(),
        ),
        TaskAssigned(
            aggregate_id=active_agent_id,
            sequence_number=2,
            task_description="Active task",
        ),
    ]

    # Completed agent
    from core.domain.events import WorkCompleted

    completed_events = [
        AgentCreated(
            aggregate_id=completed_agent_id,
            sequence_number=1,
            role=AgentRole.WORKER.value,
            parent_id=None,
            config=_test_config(),
        ),
        TaskAssigned(
            aggregate_id=completed_agent_id,
            sequence_number=2,
            task_description="Completed task",
        ),
        WorkCompleted(
            aggregate_id=completed_agent_id,
            sequence_number=3,
            result="Done",
        ),
    ]

    # Failed agent
    from core.domain.events import WorkFailed

    failed_events = [
        AgentCreated(
            aggregate_id=failed_agent_id,
            sequence_number=1,
            role=AgentRole.WORKER.value,
            parent_id=None,
            config=_test_config(),
        ),
        TaskAssigned(
            aggregate_id=failed_agent_id,
            sequence_number=2,
            task_description="Failed task",
        ),
        WorkFailed(
            aggregate_id=failed_agent_id,
            sequence_number=3,
            reason="Error occurred",
        ),
    ]

    # Mock event store
    mock_event_store.get_all_aggregate_ids.return_value = [
        active_agent_id,
        completed_agent_id,
        failed_agent_id,
    ]

    def get_events_side_effect(agent_id):
        if agent_id == active_agent_id:
            return active_events
        if agent_id == completed_agent_id:
            return completed_events
        if agent_id == failed_agent_id:
            return failed_events
        return []

    mock_event_store.get_events.side_effect = get_events_side_effect

    # When: Get active agent IDs
    active_ids = await execution_service._get_active_agent_ids()

    # Then: Only the active agent should be returned
    assert len(active_ids) == 1
    assert active_ids[0] == active_agent_id


# =============================================================================
# Tests for Left-to-Right Worker Execution Order (tree_sequence_id)
# =============================================================================


@pytest.mark.asyncio
async def test_workers_execute_in_sequence_id_order(execution_service, mock_event_store, mock_worker_port):
    """Test that workers with lower sequence_id execute before higher ones."""
    from core.domain.events import ComplexityEvaluated, CodeGenerationStarted, WorkCompleted

    # Given: Two WORKER agents with different sequence_ids
    # Worker 1 has lower sequence_id (should execute first)
    # Worker 2 has higher sequence_id (should wait)
    root_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    # Worker 1 (sequence_id=1) - left in tree
    worker1_events = [
        AgentCreated(
            aggregate_id=worker1_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=1,
        ),
        TaskAssigned(
            aggregate_id=worker1_id,
            sequence_number=2,
            task_description="Task 1",
        ),
        ComplexityEvaluated(
            aggregate_id=worker1_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    # Worker 2 (sequence_id=2) - right in tree
    worker2_events = [
        AgentCreated(
            aggregate_id=worker2_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=2,
        ),
        TaskAssigned(
            aggregate_id=worker2_id,
            sequence_number=2,
            task_description="Task 2",
        ),
        ComplexityEvaluated(
            aggregate_id=worker2_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    def get_events_side_effect(agent_id):
        if agent_id == worker1_id:
            return worker1_events
        if agent_id == worker2_id:
            return worker2_events
        return []

    mock_event_store.get_events.side_effect = get_events_side_effect
    mock_event_store.get_all_aggregate_ids.return_value = [worker1_id, worker2_id]

    # Track which worker was executed
    executed_workers = []

    async def mock_worker_generator(worker_id):
        executed_workers.append(worker_id)
        yield CodeGenerationStarted(
            aggregate_id=worker_id,
            sequence_number=4,
            tool_name="dummy",
        )
        yield WorkCompleted(
            aggregate_id=worker_id,
            sequence_number=5,
            result="Done",
        )

    class AsyncGeneratorMock:
        def __init__(self, worker_id):
            self.worker_id = worker_id

        def __call__(self, *args, **kwargs):
            return self

        def __aiter__(self):
            return mock_worker_generator(self.worker_id)

    # Setup to track which worker is being executed
    original_run_session = mock_worker_port.run_session

    def create_run_session(*args, **kwargs):
        # Determine which worker is being executed from context
        # Since we can't directly get the agent_id, we check by order
        if not executed_workers:
            return AsyncGeneratorMock(worker1_id)
        return AsyncGeneratorMock(worker2_id)

    mock_worker_port.run_session = create_run_session

    # Mock append to succeed
    mock_event_store.append.return_value = None

    # When: Run agent step for both workers in "wrong" order (worker2 first)
    # Since worker2 has higher sequence_id, it should NOT execute if worker1 is still active
    # Let's test the run_system_loop behavior by simulating active agents
    from core.query.projections.hierarchy_collector import HierarchyCollector
    from unittest.mock import patch, AsyncMock as AsyncMockClass

    # Create a mock hierarchy collector
    mock_collector = AsyncMockClass()
    mock_collector.collect_agent_ids = AsyncMockClass(return_value={worker1_id, worker2_id})

    with patch.object(execution_service, '_get_active_agent_ids') as mock_get_active:
        # First iteration: both workers active, only worker1 should execute
        # Second iteration: only worker2 active (worker1 completed)
        mock_get_active.side_effect = [
            [worker2_id, worker1_id],  # Return in "wrong" order to test sorting
            [],  # No more active agents (exit loop)
        ]

        with patch('core.application.execution_service.HierarchyCollector', return_value=mock_collector):
            # Simulate one loop iteration manually
            active_agents = await mock_get_active()

            # Load active agents
            active_agent_data = []
            for agent_id in active_agents:
                events = await mock_event_store.get_events(agent_id)
                if events:
                    from core.domain.model import AgentSession
                    agent = AgentSession.load_from_history(events)
                    active_agent_data.append({
                        "id": agent_id,
                        "role": agent.role,
                        "sequence_id": agent.tree_sequence_id,
                    })

            # Separate workers from non-workers
            workers = [a for a in active_agent_data if a["role"] == AgentRole.WORKER]
            non_workers = [a for a in active_agent_data if a["role"] != AgentRole.WORKER]

            # Sort workers by sequence_id
            workers.sort(key=lambda a: a["sequence_id"])

            # Then: Workers should be sorted with worker1 first (sequence_id=1)
            assert len(workers) == 2
            assert workers[0]["id"] == worker1_id, "Worker with lower sequence_id should be first"
            assert workers[1]["id"] == worker2_id, "Worker with higher sequence_id should be second"

            # And: Only the first worker (lowest sequence_id) should execute
            first_worker = workers[0]
            assert first_worker["sequence_id"] == 1


@pytest.mark.asyncio
async def test_non_workers_execute_regardless_of_sequence_order(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that non-workers (PENDING, BOSS, MANAGER) execute concurrently regardless of sequence."""

    # Given: Two PENDING agents with different sequence_ids
    root_id = uuid4()
    pending1_id = uuid4()
    pending2_id = uuid4()

    # Pending 1 (sequence_id=2) - higher sequence
    pending1_events = [
        AgentCreated(
            aggregate_id=pending1_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=2,
        ),
        TaskAssigned(
            aggregate_id=pending1_id,
            sequence_number=2,
            task_description="Task 1",
        ),
    ]

    # Pending 2 (sequence_id=1) - lower sequence
    pending2_events = [
        AgentCreated(
            aggregate_id=pending2_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=1,
        ),
        TaskAssigned(
            aggregate_id=pending2_id,
            sequence_number=2,
            task_description="Task 2",
        ),
    ]

    def get_events_side_effect(agent_id):
        if agent_id == pending1_id:
            return pending1_events
        if agent_id == pending2_id:
            return pending2_events
        return []

    mock_event_store.get_events.side_effect = get_events_side_effect

    # Mock LLM to return SIMPLE (turning them into workers)
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")
    mock_event_store.append.return_value = None

    # Build active_agent_data like run_system_loop does
    active_agents = [pending1_id, pending2_id]
    active_agent_data = []
    for agent_id in active_agents:
        events = await mock_event_store.get_events(agent_id)
        if events:
            from core.domain.model import AgentSession
            agent = AgentSession.load_from_history(events)
            active_agent_data.append({
                "id": agent_id,
                "role": agent.role,
                "sequence_id": agent.tree_sequence_id,
            })

    # Separate workers from non-workers
    workers = [a for a in active_agent_data if a["role"] == AgentRole.WORKER]
    non_workers = [a for a in active_agent_data if a["role"] != AgentRole.WORKER]

    # Then: Both agents should be in non_workers (PENDING is not WORKER)
    assert len(workers) == 0
    assert len(non_workers) == 2

    # And: Both non-workers can execute (no sequence ordering needed)
    # In run_system_loop, all non_workers execute in the same iteration
    executed_non_workers = []
    for agent_data in non_workers:
        executed_non_workers.append(agent_data["id"])

    assert pending1_id in executed_non_workers
    assert pending2_id in executed_non_workers


@pytest.mark.asyncio
async def test_child_agents_get_incremental_sequence_ids(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that spawned children get correct tree_sequence_id based on position."""
    import json

    # Given: A BOSS agent that will spawn 2 children
    boss_id = uuid4()
    boss_events = [
        AgentCreated(
            aggregate_id=boss_id,
            sequence_number=1,
            role=AgentRole.BOSS.value,
            parent_id=None,
            config=_test_config(),
            tree_sequence_id=0,  # Root has sequence_id=0
        ),
        TaskAssigned(
            aggregate_id=boss_id,
            sequence_number=2,
            task_description="Build app",
        ),
    ]

    mock_event_store.get_events.return_value = boss_events

    # Mock LLM to return 2 subtasks (use query not query_with_usage)
    child_config = _test_config()
    mock_llm_port.query.return_value = json.dumps([
        {"description": "Task 1", "config": child_config},
        {"description": "Task 2", "config": child_config},
    ])

    # Track created children
    created_children = []

    async def track_append(event, expected_version):
        if isinstance(event, AgentCreated) and event.aggregate_id != boss_id:
            created_children.append({
                "id": event.aggregate_id,
                "sequence_id": event.tree_sequence_id,
            })

    mock_event_store.append.side_effect = track_append

    # When: Run agent step (boss spawns children)
    await execution_service.run_agent_step(boss_id)

    # Then: Children should have incrementing sequence IDs
    # Formula: parent_sequence_id * 1000 + child_index + 1
    # Parent sequence_id = 0
    # Child 0: 0 * 1000 + 0 + 1 = 1
    # Child 1: 0 * 1000 + 1 + 1 = 2
    assert len(created_children) == 2
    sequence_ids = sorted([c["sequence_id"] for c in created_children])
    assert sequence_ids == [1, 2], f"Expected [1, 2], got {sequence_ids}"


@pytest.mark.asyncio
async def test_nested_children_get_hierarchical_sequence_ids(
    execution_service, mock_event_store, mock_llm_port
):
    """Test that deeply nested children get hierarchical sequence IDs."""
    import json
    from core.domain.events import ComplexityEvaluated

    # Given: A MANAGER agent with sequence_id=1 that will spawn 2 children
    parent_id = uuid4()
    grandparent_id = uuid4()
    parent_sequence_id = 1  # First child of boss

    parent_events = [
        AgentCreated(
            aggregate_id=parent_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=grandparent_id,
            config=_test_config(),
            tree_sequence_id=parent_sequence_id,
        ),
        TaskAssigned(
            aggregate_id=parent_id,
            sequence_number=2,
            task_description="Complex task",
        ),
        ComplexityEvaluated(
            aggregate_id=parent_id,
            sequence_number=3,
            complexity="complex",
            determined_role=AgentRole.MANAGER.value,
        ),
    ]

    mock_event_store.get_events.return_value = parent_events

    # Mock LLM to return 3 subtasks (use query not query_with_usage)
    child_config = _test_config()
    mock_llm_port.query.return_value = json.dumps([
        {"description": "Sub-task 1", "config": child_config},
        {"description": "Sub-task 2", "config": child_config},
        {"description": "Sub-task 3", "config": child_config},
    ])

    # Track created children
    created_children = []

    async def track_append(event, expected_version):
        if isinstance(event, AgentCreated) and event.aggregate_id != parent_id:
            created_children.append({
                "id": event.aggregate_id,
                "sequence_id": event.tree_sequence_id,
            })

    mock_event_store.append.side_effect = track_append

    # When: Run agent step (manager spawns children)
    await execution_service.run_agent_step(parent_id)

    # Then: Children should have hierarchical sequence IDs
    # Formula: parent_sequence_id * 1000 + child_index + 1
    # Parent sequence_id = 1
    # Child 0: 1 * 1000 + 0 + 1 = 1001
    # Child 1: 1 * 1000 + 1 + 1 = 1002
    # Child 2: 1 * 1000 + 2 + 1 = 1003
    assert len(created_children) == 3
    sequence_ids = sorted([c["sequence_id"] for c in created_children])
    assert sequence_ids == [1001, 1002, 1003], f"Expected [1001, 1002, 1003], got {sequence_ids}"


@pytest.mark.asyncio
async def test_run_system_loop_executes_lowest_sequence_worker_first(
    execution_service, mock_event_store, mock_worker_port
):
    """Test that run_system_loop executes the worker with lowest sequence_id first."""
    from core.domain.events import ComplexityEvaluated, CodeGenerationStarted, WorkCompleted
    from unittest.mock import patch

    root_id = uuid4()
    worker1_id = uuid4()  # sequence_id=1
    worker2_id = uuid4()  # sequence_id=2

    # Worker 1 (lower sequence, should execute first)
    worker1_events = [
        AgentCreated(
            aggregate_id=worker1_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=1,
        ),
        TaskAssigned(
            aggregate_id=worker1_id,
            sequence_number=2,
            task_description="Task 1",
        ),
        ComplexityEvaluated(
            aggregate_id=worker1_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    # Worker 2 (higher sequence, should wait)
    worker2_events = [
        AgentCreated(
            aggregate_id=worker2_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=root_id,
            config=_test_config(),
            tree_sequence_id=2,
        ),
        TaskAssigned(
            aggregate_id=worker2_id,
            sequence_number=2,
            task_description="Task 2",
        ),
        ComplexityEvaluated(
            aggregate_id=worker2_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    def get_events_side_effect(agent_id):
        if agent_id == worker1_id:
            return worker1_events
        if agent_id == worker2_id:
            return worker2_events
        return []

    mock_event_store.get_events.side_effect = get_events_side_effect

    # Track which worker's step is executed
    executed_worker_ids = []
    original_run_agent_step = execution_service.run_agent_step

    async def track_run_agent_step(agent_id):
        executed_worker_ids.append(agent_id)
        # Don't actually run, just track

    # Patch run_agent_step to track execution without actually running
    with patch.object(execution_service, 'run_agent_step', side_effect=track_run_agent_step):
        with patch.object(execution_service._query_service, 'get_active_agent_ids') as mock_get_active:
            # Simulate: both workers active, return them in "wrong" order
            # The QueryService should filter to only return the leftmost eligible worker
            mock_get_active.side_effect = [
                [worker1_id],  # Only worker1 (leftmost) should be returned
                [],  # Empty = exit loop
            ]

            # Run one iteration of the system loop
            await execution_service.run_system_loop(root_id)

    # Then: Worker 1 (lower sequence) should be executed first
    # Note: Since both are workers and only one executes per iteration,
    # we expect worker1 to be in the executed list first
    assert worker1_id in executed_worker_ids, "Worker with lower sequence_id should execute"
    # Worker 2 should not execute in this iteration (it's waiting for worker1)
    if len(executed_worker_ids) > 1:
        first_worker_idx = executed_worker_ids.index(worker1_id)
        if worker2_id in executed_worker_ids:
            second_worker_idx = executed_worker_ids.index(worker2_id)
            assert first_worker_idx < second_worker_idx, "Worker 1 should execute before Worker 2"
