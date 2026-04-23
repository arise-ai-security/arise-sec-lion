"""Fixtures for presentation layer integration tests.

These fixtures provide mocked application services for testing CLI behavior
without real infrastructure dependencies.
"""

from uuid import UUID, uuid4

import pytest

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events.events import DomainEvent
from presentation.cli import CLI, CLIConfig


class FakeEventStore:
    """Minimal read-only event store stub for presentation tests."""

    def __init__(self, events: dict[UUID, list[DomainEvent]] | None = None) -> None:
        self._events: dict[UUID, list[DomainEvent]] = events or {}

    def set_events(self, aggregate_id: UUID, events: list[DomainEvent]) -> None:
        self._events[aggregate_id] = list(events)

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        return list(self._events.get(aggregate_id, []))


class FakeExecutionService:
    """Fake execution service for testing presentation layer.

    This fake mimics the AgentExecutionService interface without
    actual infrastructure dependencies.
    """

    def __init__(self) -> None:
        """Initialize with default test data."""
        self.initialized = False
        self.cleaned_up = False
        self.created_boss_id: str | None = None
        self.system_loop_called = False
        self.last_task_description: str | None = None

        # Configurable test data
        self.agent_result = AgentResultDTO(
            agent_id=str(uuid4()),
            status="completed",
            result="Test result from BOSS agent",
            task_description="Test task",
            role="boss",
        )
        self.system_stats = SystemStatisticsDTO(
            total_agents=5,
            completed=4,
            failed=1,
            active=0,
        )

    async def initialize(self) -> None:
        """Simulate infrastructure initialization."""
        self.initialized = True

    async def cleanup(self) -> None:
        """Simulate infrastructure cleanup."""
        self.cleaned_up = True

    async def create_boss_agent(
        self,
        task_description: str,
        domain_context: object | None = None,
    ) -> str:
        """Simulate BOSS agent creation."""
        self.last_task_description = task_description
        self.created_boss_id = str(uuid4())
        return self.created_boss_id

    async def run_system_loop(self, root_id: str) -> None:
        """Simulate system loop execution."""
        self.system_loop_called = True

    async def get_agent_result(self, agent_id: str) -> AgentResultDTO:
        """Return configured test result."""
        return self.agent_result

    async def get_system_statistics(
        self,
        root_id: str | None = None,
    ) -> SystemStatisticsDTO:
        """Return configured test statistics."""
        return self.system_stats


@pytest.fixture
def fake_execution_service() -> FakeExecutionService:
    """Provide a fake execution service for testing."""
    return FakeExecutionService()


@pytest.fixture
def fake_event_store() -> FakeEventStore:
    """Provide a fake read-only event store for testing."""
    return FakeEventStore()


@pytest.fixture
def cli_with_fake_service(
    fake_execution_service: FakeExecutionService,
    fake_event_store: FakeEventStore,
) -> CLI:
    """Provide CLI instance with fake execution service."""
    config = CLIConfig(verbose=False)
    return CLI(
        execution_service=fake_execution_service,  # type: ignore[arg-type]
        event_store=fake_event_store,  # type: ignore[arg-type]
        config=config,
    )


@pytest.fixture
def verbose_cli_with_fake_service(
    fake_execution_service: FakeExecutionService,
    fake_event_store: FakeEventStore,
) -> CLI:
    """Provide verbose CLI instance with fake execution service."""
    config = CLIConfig(verbose=True)
    return CLI(
        execution_service=fake_execution_service,  # type: ignore[arg-type]
        event_store=fake_event_store,  # type: ignore[arg-type]
        config=config,
    )
