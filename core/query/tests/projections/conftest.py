"""Shared test fixtures for projection tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from core.domain.events.events import (
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
    TokensConsumed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.values.subtask import Subtask
from core.query.ports.sink_port import SinkPort
from core.query.projections.registry import RegistryError, register_sink


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

    async def get_hierarchy_events_grouped(
        self, root_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        """Get all events for hierarchy rooted at root_id, grouped by agent.

        Uses ChildSpawned events to discover children recursively.
        """
        result: dict[UUID, list[DomainEvent]] = {}
        to_visit = [root_id]
        visited: set[UUID] = set()

        while to_visit:
            agent_id = to_visit.pop(0)
            if agent_id in visited:
                continue
            visited.add(agent_id)

            events = self._events.get(agent_id, [])
            if events:
                result[agent_id] = events

            # Find children from ChildSpawned events
            for event in events:
                if isinstance(event, ChildSpawned):
                    if event.child_id not in visited:
                        to_visit.append(event.child_id)

        return result


@pytest.fixture
def fake_event_store() -> FakeEventStore:
    """Create a fake event store for testing."""
    return FakeEventStore()


@pytest.fixture
def tokens_consumed_event() -> TokensConsumed:
    """Create a TokensConsumed event for BOSS."""
    return TokensConsumed(
        aggregate_id=BOSS_ID,
        sequence_number=5,
        model="gpt-4o",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        cost_usd=0.05,
        operation="complexity_evaluation",
        occurred_at=BASE_TIME + timedelta(seconds=5),
    )


@pytest.fixture
def worker_cost_recorded_event() -> WorkerCostRecorded:
    """Create a WorkerCostRecorded event for WORKER."""
    return WorkerCostRecorded(
        aggregate_id=WORKER_ID,
        sequence_number=6,
        tool_name="claude_code",
        model="claude-3-5-sonnet-20241022",
        tokens=5000,
        cost_usd=0.50,
        duration_seconds=120.0,
        occurred_at=BASE_TIME + timedelta(seconds=10),
    )


@pytest.fixture
def cost_events_hierarchy() -> list[DomainEvent]:
    """Create a realistic hierarchy with cost events.

    BOSS -> MANAGER -> 2 WORKERS
    Each agent has associated cost events.
    """
    return [
        # BOSS created
        AgentCreated(
            aggregate_id=BOSS_ID,
            sequence_number=1,
            role="BOSS",
            parent_id=None,
            config={},
            occurred_at=BASE_TIME,
        ),
        # BOSS LLM cost for task decomposition
        TokensConsumed(
            aggregate_id=BOSS_ID,
            sequence_number=2,
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            cost_usd=0.10,
            operation="task_decomposition",
            occurred_at=BASE_TIME + timedelta(seconds=1),
        ),
        # MANAGER created
        AgentCreated(
            aggregate_id=MANAGER_ID,
            sequence_number=1,
            role="MANAGER",
            parent_id=BOSS_ID,
            config={},
            occurred_at=BASE_TIME + timedelta(seconds=2),
        ),
        # MANAGER LLM cost
        TokensConsumed(
            aggregate_id=MANAGER_ID,
            sequence_number=2,
            model="gpt-4o-mini",
            prompt_tokens=800,
            completion_tokens=400,
            total_tokens=1200,
            cost_usd=0.05,
            operation="task_decomposition",
            occurred_at=BASE_TIME + timedelta(seconds=3),
        ),
        # WORKER 1 created
        AgentCreated(
            aggregate_id=WORKER_ID,
            sequence_number=1,
            role="WORKER",
            parent_id=MANAGER_ID,
            config={},
            occurred_at=BASE_TIME + timedelta(seconds=4),
        ),
        # WORKER 1 complexity evaluation
        TokensConsumed(
            aggregate_id=WORKER_ID,
            sequence_number=2,
            model="gpt-4o-mini",
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
            cost_usd=0.01,
            operation="complexity_evaluation",
            occurred_at=BASE_TIME + timedelta(seconds=5),
        ),
        # WORKER 1 tool execution cost
        WorkerCostRecorded(
            aggregate_id=WORKER_ID,
            sequence_number=3,
            tool_name="claude_code",
            model="claude-3-5-sonnet-20241022",
            tokens=5000,
            cost_usd=0.50,
            duration_seconds=120.0,
            occurred_at=BASE_TIME + timedelta(seconds=6),
        ),
        # WORKER 2 created
        AgentCreated(
            aggregate_id=WORKER2_ID,
            sequence_number=1,
            role="WORKER",
            parent_id=MANAGER_ID,
            config={},
            occurred_at=BASE_TIME + timedelta(seconds=4),
        ),
        # WORKER 2 tool execution cost (no model info)
        WorkerCostRecorded(
            aggregate_id=WORKER2_ID,
            sequence_number=2,
            tool_name="openhands",
            model=None,
            tokens=None,
            cost_usd=0.30,
            duration_seconds=90.0,
            occurred_at=BASE_TIME + timedelta(seconds=7),
        ),
    ]


class FakeStringSink(SinkPort):
    """Simple string sink for testing (no infrastructure dependency)."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def write(self, content: str) -> None:
        self._lines.append(content)

    def getvalue(self) -> str:
        return "\n".join(self._lines)

    @property
    def lines(self) -> list[str]:
        return list(self._lines)


try:
    register_sink("string")(FakeStringSink)
except RegistryError:
    # "string" may already be registered by infrastructure bootstrap imports.
    # Query projection tests should not depend on global import order.
    pass


@pytest.fixture
def test_string_sink() -> FakeStringSink:
    """Create a test string sink."""
    return FakeStringSink()
