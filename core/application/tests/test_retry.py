"""Tests for auto-healing and retry logic (Phase 4)."""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bootstrap.application import ExecutionLimitsBridge

# Reuse InMemoryEventStore from characterization tests
from bootstrap.tests.test_characterization import (
    FakeLLM,
    FakeSharedContextPort,
    FakeSiblingViewPort,
    InMemoryEventStore,
    _find_child_ids,
    _get_agent_status,
)
from config import BossConfig, ManagerConfig, RetryConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    HierarchyLimitsRegistry,
    ServiceConfig,
)
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    ParentNotificationService,
    PromptBuilder,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    RetryScheduled,
    WorkCompleted,
    WorkFailed,
)


def _make_limits() -> ExecutionLimitsBridge:
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
    )


class _LimitsWithRetry:
    """Wrapper that adds retry config to ExecutionLimitsBridge for testing.

    The real OrchestrationConfig has retry on the parent, but ExecutionService
    reads it via getattr(system_limits, 'retry', None).
    """

    def __init__(
        self,
        limits: ExecutionLimitsBridge,
        escalation_chain: list[str] | None = None,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        self._limits = limits
        self.retry = RetryConfig(
            model_escalation_chain=escalation_chain or [],
            circuit_breaker_threshold=circuit_breaker_threshold,
        )

    def __getattr__(self, name: str) -> Any:
        if name == "retry":
            return self.retry
        return getattr(self._limits, name)


def _make_system_limits_with_retry(
    escalation_chain: list[str] | None = None,
    circuit_breaker_threshold: int = 3,
) -> Any:
    """Create limits config with retry settings."""
    limits = _make_limits()
    return _LimitsWithRetry(
        limits, escalation_chain=escalation_chain,
        circuit_breaker_threshold=circuit_breaker_threshold,
    )


class FailThenSucceedWorker:
    """Worker that fails N times then succeeds."""

    def __init__(self, fail_count: int = 1, result: str = "Done") -> None:
        self._fail_count = fail_count
        self._call_count = 0
        self._result = result

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        self._call_count += 1
        agent_id = task_context["agent_id"]

        yield CodeGenerationStarted(
            aggregate_id=agent_id, sequence_number=0, tool_name="fake"
        )

        if self._call_count <= self._fail_count:
            yield WorkFailed(
                aggregate_id=agent_id, sequence_number=0, reason="Transient error"
            )
        else:
            yield WorkCompleted(
                aggregate_id=agent_id, sequence_number=0, result=self._result
            )


class AlwaysFailWorker:
    """Worker that always fails."""

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id, sequence_number=0, tool_name="fake"
        )
        yield WorkFailed(
            aggregate_id=agent_id, sequence_number=0, reason="Permanent error"
        )


def _wire_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: Any,
    system_limits: ExecutionLimitsBridge | None = None,
) -> AgentExecutionService:
    """Wire execution service with retry-aware configuration."""
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.1,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )
    limits = system_limits or _make_limits()

    prompt_builder = PromptBuilder("prompts", "claude_code")
    limits_registry = HierarchyLimitsRegistry()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=config.manager_config,
    )
    orchestrator = AgentOrchestrator(
        llm_port=llm,
        worker_port=worker,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )
    parent_notifier = ParentNotificationService(repository=repository)

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=FakeSharedContextPort(),
        sibling_view_port=FakeSiblingViewPort(),
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
    )

    return AgentExecutionService(
        event_store=event_store,
        dependencies=dependencies,
        config=config,
        system_limits=limits,
    )


class TestRetryOnFailure:
    """Failed workers retry before propagating failure."""

    @pytest.mark.asyncio
    async def test_worker_retries_on_failure_with_escalation(self) -> None:
        """Worker fails, gets retried with escalated model, succeeds."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FailThenSucceedWorker(fail_count=1)

        limits = _make_system_limits_with_retry(
            escalation_chain=["gpt-4o", "gpt-4o-mini"]
        )
        svc = _wire_service(event_store, llm, worker, system_limits=limits)

        # Create boss and run to get a worker
        boss_id = await svc.create_boss_agent("Retry test")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]

        # Child → WORKER
        await svc.run_agent_step(child_id)

        # Worker fails first attempt → retry scheduled
        await svc.run_agent_step(child_id)

        # Agent should be back in ANALYZING (retried)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        # Check RetryScheduled event exists
        retry_events = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, RetryScheduled)
        ]
        assert len(retry_events) == 1
        assert retry_events[0].attempt == 1

        # Second attempt succeeds
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "completed"

    @pytest.mark.asyncio
    async def test_no_retry_without_escalation_chain(self) -> None:
        """Without escalation chain, failure propagates immediately."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()

        # No escalation chain = no retries
        svc = _wire_service(event_store, llm, worker)

        boss_id = await svc.create_boss_agent("No retry test")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        # Should be FAILED (no retry)
        assert _get_agent_status(event_store, child_id) == "failed"
        # Boss should also be FAILED (propagated)
        assert _get_agent_status(event_store, boss_id) == "failed"

    @pytest.mark.asyncio
    async def test_retries_exhaust_then_propagate(self) -> None:
        """After all retries exhausted, failure propagates to parent."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()

        limits = _make_system_limits_with_retry(
            escalation_chain=["gpt-4o", "gpt-4o-mini"]  # 2 models = 2 retries
        )
        svc = _wire_service(event_store, llm, worker, system_limits=limits)

        boss_id = await svc.create_boss_agent("Exhaust retries")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)

        # Fail → retry 1
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        # Fail → retry 2
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        # Fail → retries exhausted, propagate
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "failed"
        assert _get_agent_status(event_store, boss_id) == "failed"


class TestRetryScheduledEvent:
    """RetryScheduled event transitions and state."""

    def test_retry_resets_agent_to_analyzing(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("task")
        agent.fail_with_reason("some error")
        assert agent.status == AgentStatus.FAILED

        agent.schedule_retry(reason="some error", escalated_model="gpt-4o-mini")
        assert agent.status == AgentStatus.ANALYZING
        assert agent.retry_count == 1
        assert agent.result is None
        assert agent.error_message is None

    def test_retry_with_model_escalation(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("task")
        agent.fail_with_reason("error")
        agent.schedule_retry(reason="error", escalated_model="gpt-4o-mini")

        assert agent.config.base.model == "gpt-4o-mini"


class TestNoProgressRestart:
    """No-progress restarts should use dedicated retry budget."""

    @pytest.mark.asyncio
    async def test_no_progress_schedules_retry_with_dedicated_budget(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()
        svc = _wire_service(event_store, llm, worker)

        worker_id = uuid4()
        agent = AgentSession.create(
            agent_id=worker_id,
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("stalled worker")
        await svc._repository.save_new_agent(agent)

        svc._query_service.get_no_progress_workers = AsyncMock(return_value=[worker_id])  # type: ignore[method-assign]
        restarted = await svc._restart_no_progress_workers(
            root_agent_id=worker_id,
            tasks={},
            in_progress=set(),
            task_started_at={},
        )
        assert restarted == 1

        updated = await svc._repository.load(worker_id)
        assert updated.status == AgentStatus.ANALYZING
        assert updated.retry_count == 1
        assert any(
            isinstance(e, RetryScheduled) for e in event_store.events_for(worker_id)
        )

    @pytest.mark.asyncio
    async def test_no_progress_honors_retry_budget_and_fails_permanently(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()
        svc = _wire_service(event_store, llm, worker)

        worker_id = uuid4()
        agent = AgentSession.create(
            agent_id=worker_id,
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("stalled worker")
        # Exhaust dedicated no-progress budget (default=2)
        no_progress_reason = "Zero thoughts after 180s — LLM likely unresponsive"
        agent.fail_with_reason("prior")
        agent.schedule_retry(reason=no_progress_reason)
        agent.fail_with_reason("prior")
        agent.schedule_retry(reason=no_progress_reason)
        await svc._repository.save_new_agent(agent)

        svc._query_service.get_no_progress_workers = AsyncMock(return_value=[worker_id])  # type: ignore[method-assign]
        restarted = await svc._restart_no_progress_workers(
            root_agent_id=worker_id,
            tasks={},
            in_progress=set(),
            task_started_at={},
        )
        assert restarted == 1

        updated = await svc._repository.load(worker_id)
        assert updated.status == AgentStatus.FAILED
        assert updated.retry_count == 2

        events = event_store.events_for(worker_id)
        assert isinstance(events[-1], WorkFailed)
        assert "restarting" not in (events[-1].reason or "").lower()

    @pytest.mark.asyncio
    async def test_no_progress_budget_is_independent_of_verification_retries(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()
        svc = _wire_service(event_store, llm, worker)

        worker_id = uuid4()
        agent = AgentSession.create(
            agent_id=worker_id,
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("stalled worker")
        # Simulate prior verification retries using shared retry_count.
        agent.fail_with_reason("Verification failed (judge): attempt 1")
        agent.schedule_retry(reason="Verification failed (judge): attempt 1")
        agent.fail_with_reason("Verification failed (judge): attempt 2")
        agent.schedule_retry(reason="Verification failed (judge): attempt 2")
        await svc._repository.save_new_agent(agent)

        svc._query_service.get_no_progress_workers = AsyncMock(return_value=[worker_id])  # type: ignore[method-assign]
        restarted = await svc._restart_no_progress_workers(
            root_agent_id=worker_id,
            tasks={},
            in_progress=set(),
            task_started_at={},
        )
        assert restarted == 1

        updated = await svc._repository.load(worker_id)
        assert updated.status == AgentStatus.ANALYZING
        # shared retry_count still increments, but budget decision is independent
        assert updated.retry_count == 3

        events = event_store.events_for(worker_id)
        retry_events = [e for e in events if isinstance(e, RetryScheduled)]
        assert len(retry_events) == 3
        assert retry_events[-1].reason.startswith("Zero thoughts after")
