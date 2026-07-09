"""Tests for auto-healing and retry logic (Phase 4)."""

from collections.abc import AsyncIterator
from typing import Any
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
    FailureDigestRecorded,
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
    # max_redecompositions=0 isolates these Layer-1 worker-retry tests from the
    # Layer-2 all-children-failed redecomposition (covered in test_parent_notifier).
    parent_notifier = ParentNotificationService(repository=repository, max_redecompositions=0)

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
    async def test_hard_failed_worker_retries_via_digest(self) -> None:
        """A crashed worker retries via the failure digest even without an
        escalation chain, up to verification_max_retries, then propagates."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()

        # No escalation chain: retry_policy is disabled, so any retry here comes
        # solely from the digest path (verification_max_retries defaults to 2).
        svc = _wire_service(event_store, llm, worker)

        boss_id = await svc.create_boss_agent("Digest retry test")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]

        # Child → WORKER (assessment)
        await svc.run_agent_step(child_id)

        # First execution fails → crash digest recorded, worker retried
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"
        digests = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, FailureDigestRecorded)
        ]
        assert len(digests) == 1
        assert digests[0].source == "worker_crash"

        # Second failure → retry 2 (digest retained, not re-recorded)
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        # Third failure → verification_max_retries exhausted → propagate
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "failed"
        assert _get_agent_status(event_store, boss_id) == "failed"

        retry_events = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, RetryScheduled)
        ]
        assert len(retry_events) == 2  # verification_max_retries
        digests = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, FailureDigestRecorded)
        ]
        assert len(digests) == 1  # recorded once, retained across retries

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


def _worker_config() -> dict[str, Any]:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
        "tool": "claude_code",
    }


class TestWorkerCrashDigestGate:
    """The FAILED-branch crash-digest gate in _handle_post_step."""

    @pytest.mark.asyncio
    async def test_verification_failure_skips_digest_but_retries(self) -> None:
        """A verification failure already carries feedback context: no crash
        digest is recorded, yet the worker still retries on that feedback."""
        # Given: a WORKER that failed verification (feedback set, no crash digest)
        event_store = InMemoryEventStore()
        svc = _wire_service(event_store, FakeLLM(), AlwaysFailWorker())
        repository = AgentRepository(event_store=event_store, max_retries=3)

        worker = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
            parent_id=uuid4(),
        )
        worker.assign_task("do the thing")
        worker.start_worker_execution("claude_code")
        worker.mark_verification_failed(
            failed_stage="judge",
            feedback="Missing tests",
            stages_passed=["structural"],
            score=30,
        )
        await repository.save_new_agent(worker)

        # When: post-step handling runs on the failed worker
        reloaded = await repository.load(worker.agent_id)
        await svc._handle_post_step(reloaded, [])

        # Then: no crash digest is recorded, but a retry is scheduled
        events = event_store.events_for(worker.agent_id)
        assert not any(isinstance(e, FailureDigestRecorded) for e in events)
        assert any(isinstance(e, RetryScheduled) for e in events)

    @pytest.mark.asyncio
    async def test_existing_digest_not_overwritten(self) -> None:
        """A worker that already recorded a digest is not re-digested."""
        # Given: a hard-failed WORKER with a digest already recorded
        event_store = InMemoryEventStore()
        svc = _wire_service(event_store, FakeLLM(), AlwaysFailWorker())
        repository = AgentRepository(event_store=event_store, max_retries=3)

        worker = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
            parent_id=uuid4(),
        )
        worker.assign_task("do the thing")
        worker.start_worker_execution("claude_code")
        worker.fail_with_reason("Segfault")
        worker.record_failure_digest("PRE-EXISTING DIGEST", source="worker_crash")
        await repository.save_new_agent(worker)

        # When: post-step handling runs again on the failed worker
        reloaded = await repository.load(worker.agent_id)
        await svc._handle_post_step(reloaded, [])

        # Then: the original digest survives unchanged and the worker retries
        events = event_store.events_for(worker.agent_id)
        digests = [e for e in events if isinstance(e, FailureDigestRecorded)]
        assert len(digests) == 1
        assert digests[0].digest == "PRE-EXISTING DIGEST"
        assert any(isinstance(e, RetryScheduled) for e in events)
