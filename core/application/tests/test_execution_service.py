"""Tests for AgentExecutionService (Application Layer).

These tests verify the orchestration logic using mocked ports.
"""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from config import OrchestrationConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    ServiceConfig,
)
from core.application.services.agent_repository import AgentNotFoundError, AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.context_registry import HierarchyLimitsRegistry
from core.application.services.parent_notifier import ParentNotificationService
from core.application.services.query_service import AgentQueryService
from core.application.services.workspace_context import WorkspaceContextProvider
from core.domain.events.events import AgentCreated, TaskAssigned
from core.domain.exceptions import ConcurrencyError
from core.domain.values.llm_response import LLMResponse, LLMUsage
from core.domain.aggregates.agent_session import AgentRole, AgentStatus
from core.application.services.prompt_builder import PromptBuilder


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
def limits_registry():
    """Create limits registry for tests."""
    return HierarchyLimitsRegistry()


@pytest.fixture
def execution_service(mock_event_store, mock_llm_port, mock_worker_port, limits_registry):
    """Create execution service with mocked ports and injected collaborators."""
    from config import BossConfig, ManagerConfig

    boss_config = BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    manager_config = ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)

    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.5,
        output_directory="./test_output",
        default_worker_tool="claude_code",
        boss_config=boss_config,
        manager_config=manager_config,
    )
    system_limits = _test_system_limits()

    prompt_builder = PromptBuilder("prompts", "claude_code")
    repository = AgentRepository(
        event_store=mock_event_store,
        max_retries=config.max_retries,
    )
    workspace = WorkspaceContextProvider()
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=system_limits.max_total_agents,
        manager_config=manager_config,
    )

    orchestrator = AgentOrchestrator(
        llm_port=mock_llm_port,
        worker_port=mock_worker_port,
        prompt_builder=prompt_builder,
    )

    # Mock sibling view to return empty view
    from core.domain.values.context import SiblingView

    sibling_view_mock = AsyncMock()
    sibling_view_mock.build_view.return_value = SiblingView(
        current_agent_id="test",
        parent_task=None,
        sibling_tasks=(),
        shared_decisions=(),
    )

    parent_notifier = ParentNotificationService(
        repository=repository,
        progress_callback=None,
    )

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        workspace=workspace,
        shared_context_port=AsyncMock(),
        sibling_view_port=sibling_view_mock,
        parent_notifier=parent_notifier,
    )

    return AgentExecutionService(
        event_store=mock_event_store,
        dependencies=dependencies,
        config=config,
        system_limits=system_limits,
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

    # And: Events should have been appended to event store (append or append_batch)
    events_persisted = mock_event_store.append.called or mock_event_store.append_batch.called
    assert events_persisted, "Should save events to event store"


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

    # And: Events should have been saved (append or append_batch)
    events_persisted = mock_event_store.append.called or mock_event_store.append_batch.called
    assert events_persisted, "Should save SubtasksDefined event"


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
    from core.domain.events.events import ComplexityEvaluated

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
    execution_service, mock_event_store, mock_worker_port, limits_registry
):
    """Test that WORKER agent calls execute_task."""

    # Given: A WORKER agent in ANALYZING status (no parent for this isolated test)
    agent_id = uuid4()

    # Register as root limits (required for sibling view lookup)
    limits_registry.create_root(agent_id, max_depth=-1, max_children_per_node=-1, max_retries=3)

    event1 = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.PENDING.value,
        parent_id=None,
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Write unit tests",
    )

    # ComplexityEvaluated with SIMPLE -> role becomes WORKER
    from core.domain.events.events import ComplexityEvaluated

    event3 = ComplexityEvaluated(
        aggregate_id=agent_id,
        sequence_number=3,
        complexity="simple",
        determined_role=AgentRole.WORKER.value,
    )

    mock_event_store.get_events.return_value = [event1, event2, event3]

    # Mock worker tool to return events as async generator
    # Create the async generator function
    from core.domain.events.events import CodeGenerationStarted, WorkCompleted

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

    # First append_batch fails with ConcurrencyError, retry succeeds
    # (Multiple events use append_batch, not append)
    mock_event_store.append_batch.side_effect = [
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
        config=_test_config(),
    )

    event2 = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Simple task",
    )

    mock_event_store.get_events.return_value = [event1, event2]
    mock_llm_port.query_with_usage.return_value = _make_llm_response("SIMPLE")

    # All appends fail with ConcurrencyError (use append_batch for multiple events)
    mock_event_store.append_batch.side_effect = ConcurrencyError(
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

    # Then: Should have called append/append_batch to save WorkFailed event
    # The service reloads and saves failure event
    events_persisted = mock_event_store.append.called or mock_event_store.append_batch.called
    assert events_persisted, "Should save WorkFailed event"


@pytest.mark.asyncio
async def test_run_agent_step_raises_if_agent_not_found(execution_service, mock_event_store):
    """Test that missing agent raises AgentNotFoundError."""

    # Given: Agent does not exist
    agent_id = uuid4()
    mock_event_store.get_events.return_value = []

    # When/Then: Should raise AgentNotFoundError
    with pytest.raises(AgentNotFoundError):
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

    from core.domain.events.events import WorkCompleted

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
    no_events_persisted = not mock_event_store.append.called and not mock_event_store.append_batch.called
    assert no_events_persisted, "Should not save events for completed agent"


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

    # Track all append/append_batch calls to verify child creation
    all_events = []

    async def track_append(event, expected_version):
        all_events.append(event)

    async def track_append_batch(events, expected_version):
        all_events.extend(events)

    mock_event_store.append.side_effect = track_append
    mock_event_store.append_batch.side_effect = track_append_batch

    # When: Run agent step
    await execution_service.run_agent_step(agent_id)

    # Then: Should have saved parent's events AND child creation events
    # Parent events: TokensConsumed, SubtasksDefined, ChildSpawned(x2), StatusChanged (WAITING)
    # Child 1 events: AgentCreated, TaskAssigned
    # Child 2 events: AgentCreated, TaskAssigned
    assert len(all_events) > 4, "Should save parent events + child creation events"

    # Verify child creation events exist
    child_created_events = [
        e for e in all_events if isinstance(e, AgentCreated) and e.aggregate_id != agent_id
    ]
    assert len(child_created_events) == 2, "Should create 2 child agents"

    # Verify child task assignment events exist
    child_task_events = [
        e for e in all_events if isinstance(e, TaskAssigned) and e.aggregate_id != agent_id
    ]
    assert len(child_task_events) == 2, "Should assign tasks to 2 children"


@pytest.mark.asyncio
async def test_run_agent_step_notifies_parent_when_child_completes(
    execution_service, mock_event_store, mock_worker_port, limits_registry
):
    """Test that parent is notified when child agent completes."""
    from core.domain.events.events import ChildCompleted

    # Given: A WORKER child that's about to complete
    parent_id = uuid4()
    child_id = uuid4()

    # Register limits (parent is root, child inherits)
    limits_registry.create_root(parent_id, max_depth=-1, max_children_per_node=-1, max_retries=3)
    limits_registry.propagate_to_child(parent_id, child_id)

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

    from core.domain.events.events import ComplexityEvaluated

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

    from core.domain.events.events import ChildSpawned, StatusChanged
    from core.domain.values.subtask import Subtask

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
    from core.domain.events.events import CodeGenerationStarted, WorkCompleted

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

    # Track append/append_batch calls
    all_events = []

    async def track_append(event, expected_version):
        all_events.append(event)

    async def track_append_batch(events, expected_version):
        all_events.extend(events)

    mock_event_store.append.side_effect = track_append
    mock_event_store.append_batch.side_effect = track_append_batch

    # When: Run child agent step (which will complete)
    await execution_service.run_agent_step(child_id)

    # Then: Should have saved child completion events AND parent notification
    # Verify ChildCompleted event was sent to parent
    child_completed_events = [e for e in all_events if isinstance(e, ChildCompleted)]
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
    from core.domain.events.events import WorkCompleted

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
    from core.domain.events.events import WorkFailed

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

    # Mock event store - use get_all_events_grouped() for single-query efficiency
    mock_event_store.get_all_events_grouped.return_value = {
        active_agent_id: active_events,
        completed_agent_id: completed_events,
        failed_agent_id: failed_events,
    }

    # When: Get active agent IDs via query service
    active_ids = await execution_service._query_service.get_active_agent_ids()

    # Then: Only the active agent should be returned
    assert len(active_ids) == 1
    assert active_ids[0] == active_agent_id
