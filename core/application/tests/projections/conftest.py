"""Shared test fixtures for projection tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from core.domain.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.subtask import Subtask


# Fixed UUIDs for predictable tests
BOSS_ID = UUID("00000000-0000-0000-0000-000000000001")
MANAGER_ID = UUID("00000000-0000-0000-0000-000000000002")
WORKER_ID = UUID("00000000-0000-0000-0000-000000000003")
WORKER2_ID = UUID("00000000-0000-0000-0000-000000000004")

# Base timestamp for events
BASE_TIME = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


def _subtask_config() -> dict:
    """Standard config for test subtasks."""
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5},
    }


@pytest.fixture
def agent_created_event() -> AgentCreated:
    """Create an AgentCreated event for BOSS."""
    return AgentCreated(
        aggregate_id=BOSS_ID,
        sequence_number=1,
        role="BOSS",
        parent_id=None,
        config={"model": "claude-3"},
        occurred_at=BASE_TIME,
    )


@pytest.fixture
def task_assigned_event() -> TaskAssigned:
    """Create a TaskAssigned event."""
    return TaskAssigned(
        aggregate_id=BOSS_ID,
        sequence_number=2,
        task_description="Build a web scraper for news articles",
        constraints={"language": "python", "max_complexity": "medium"},
        occurred_at=BASE_TIME + timedelta(seconds=1),
    )


@pytest.fixture
def status_changed_event() -> StatusChanged:
    """Create a StatusChanged event."""
    return StatusChanged(
        aggregate_id=BOSS_ID,
        sequence_number=3,
        old_status="CREATED",
        new_status="ANALYZING",
        reason="Task received",
        occurred_at=BASE_TIME + timedelta(seconds=2),
    )


@pytest.fixture
def complexity_evaluated_event() -> ComplexityEvaluated:
    """Create a ComplexityEvaluated event."""
    return ComplexityEvaluated(
        aggregate_id=MANAGER_ID,
        sequence_number=2,
        complexity="complex",
        determined_role="MANAGER",
        reasoning="Task requires multiple steps",
        occurred_at=BASE_TIME + timedelta(seconds=3),
    )


@pytest.fixture
def subtasks_defined_event() -> SubtasksDefined:
    """Create a SubtasksDefined event."""
    return SubtasksDefined(
        aggregate_id=MANAGER_ID,
        sequence_number=3,
        subtasks=[
            Subtask(
                description="Research web scraping libraries",
                config=_subtask_config(),
            ),
            Subtask(
                description="Implement HTML parser",
                config=_subtask_config(),
            ),
        ],
        occurred_at=BASE_TIME + timedelta(seconds=4),
    )


@pytest.fixture
def child_spawned_event() -> ChildSpawned:
    """Create a ChildSpawned event."""
    return ChildSpawned(
        aggregate_id=MANAGER_ID,
        sequence_number=4,
        child_id=WORKER_ID,
        child_role="PENDING",
        subtask=Subtask(
            description="Research web scraping libraries",
            config=_subtask_config(),
        ),
        child_config={"strategy": "heuristic", "tool": "claude_code"},
        occurred_at=BASE_TIME + timedelta(seconds=5),
    )


@pytest.fixture
def code_generation_started_event() -> CodeGenerationStarted:
    """Create a CodeGenerationStarted event."""
    return CodeGenerationStarted(
        aggregate_id=WORKER_ID,
        sequence_number=3,
        tool_name="claude_code",
        occurred_at=BASE_TIME + timedelta(seconds=6),
    )


@pytest.fixture
def thought_captured_event() -> ThoughtCaptured:
    """Create a ThoughtCaptured event."""
    return ThoughtCaptured(
        aggregate_id=WORKER_ID,
        sequence_number=4,
        content="Analyzing BeautifulSoup documentation...",
        stream="stdout",
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )


@pytest.fixture
def child_completed_event() -> ChildCompleted:
    """Create a ChildCompleted event."""
    return ChildCompleted(
        aggregate_id=MANAGER_ID,
        sequence_number=5,
        child_id=WORKER_ID,
        result="Research completed successfully",
        occurred_at=BASE_TIME + timedelta(seconds=8),
    )


@pytest.fixture
def work_completed_event() -> WorkCompleted:
    """Create a WorkCompleted event."""
    return WorkCompleted(
        aggregate_id=WORKER_ID,
        sequence_number=5,
        result="Task completed with generated code",
        occurred_at=BASE_TIME + timedelta(seconds=9),
    )


@pytest.fixture
def work_failed_event() -> WorkFailed:
    """Create a WorkFailed event."""
    return WorkFailed(
        aggregate_id=WORKER_ID,
        sequence_number=5,
        reason="API rate limit exceeded",
        occurred_at=BASE_TIME + timedelta(seconds=10),
    )


@pytest.fixture
def status_changed_to_failed_event() -> StatusChanged:
    """Create a StatusChanged event transitioning to FAILED."""
    return StatusChanged(
        aggregate_id=WORKER_ID,
        sequence_number=6,
        old_status="EXECUTING",
        new_status="FAILED",
        reason="Work failed due to API error",
        occurred_at=BASE_TIME + timedelta(seconds=11),
    )


@pytest.fixture
def all_event_types(
    agent_created_event,
    task_assigned_event,
    status_changed_event,
    complexity_evaluated_event,
    subtasks_defined_event,
    child_spawned_event,
    code_generation_started_event,
    thought_captured_event,
    child_completed_event,
    work_completed_event,
    work_failed_event,
) -> list[DomainEvent]:
    """Get one of each event type for comprehensive testing."""
    return [
        agent_created_event,
        task_assigned_event,
        status_changed_event,
        complexity_evaluated_event,
        subtasks_defined_event,
        child_spawned_event,
        code_generation_started_event,
        thought_captured_event,
        child_completed_event,
        work_completed_event,
        work_failed_event,
    ]


@pytest.fixture
def sample_events() -> list[DomainEvent]:
    """Create a realistic sequence of events for a simple run."""
    return [
        AgentCreated(
            aggregate_id=BOSS_ID,
            sequence_number=1,
            role="BOSS",
            parent_id=None,
            config={},
            occurred_at=BASE_TIME,
        ),
        TaskAssigned(
            aggregate_id=BOSS_ID,
            sequence_number=2,
            task_description="Simple task",
            constraints={},
            occurred_at=BASE_TIME + timedelta(seconds=1),
        ),
        StatusChanged(
            aggregate_id=BOSS_ID,
            sequence_number=3,
            old_status="CREATED",
            new_status="ANALYZING",
            reason="Starting analysis",
            occurred_at=BASE_TIME + timedelta(seconds=2),
        ),
        WorkCompleted(
            aggregate_id=BOSS_ID,
            sequence_number=4,
            result="Task completed",
            occurred_at=BASE_TIME + timedelta(seconds=3),
        ),
    ]


class FakeEventStore:
    """In-memory event store for testing."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[DomainEvent]] = {}

    def add_events(self, agent_id: UUID, events: list[DomainEvent]) -> None:
        """Add events for an agent."""
        self._events[agent_id] = events

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def initialize_schema(self) -> None:
        pass

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        agent_id = event.aggregate_id
        if agent_id not in self._events:
            self._events[agent_id] = []
        self._events[agent_id].append(event)

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        return self._events.get(aggregate_id, [])

    async def get_all_aggregate_ids(self) -> list[UUID]:
        return list(self._events.keys())


@pytest.fixture
def fake_event_store() -> FakeEventStore:
    """Create a fake event store for testing."""
    return FakeEventStore()
