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
    RetryPolicy,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    PromptSent,
    RetryScheduled,
    WorkCompleted,
    WorkFailed,
)
from core.domain.values.limits import HierarchyLimits


def _make_limits() -> ExecutionLimitsBridge:
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
    )


def _make_system_limits_with_retry(
    escalation_chain: list[str] | None = None,
    max_worker_retries: int | None = None,
    max_verification_retries: int = 2,
    circuit_breaker_threshold: int = 3,
    circuit_breaker_reset_seconds: int = 300,
) -> Any:
    """Create limits config with retry settings."""
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        retry=RetryConfig(
            max_worker_retries=max_worker_retries,
            max_verification_retries=max_verification_retries,
            model_escalation_chain=escalation_chain or [],
            circuit_breaker_threshold=circuit_breaker_threshold,
            circuit_breaker_reset_seconds=circuit_breaker_reset_seconds,
        ),
    )


def _worker_config(model: str = "gpt-4o") -> dict[str, Any]:
    return {
        "strategy": "heuristic",
        "base": {"model": model, "temperature": 0.5, "max_tokens": 1000},
        "tool": "claude_code",
    }


async def _persist_agent(repository: AgentRepository, agent: AgentSession) -> None:
    await repository.persist_events(
        agent,
        expected_version=agent.version - len(agent.events),
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


class RecordingWorker:
    """Worker that records prompts and completes immediately."""

    def __init__(self) -> None:
        self.task_contexts: list[dict[str, Any]] = []

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        self.task_contexts.append(task_context)
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id, sequence_number=0, tool_name="fake"
        )
        yield WorkCompleted(
            aggregate_id=agent_id, sequence_number=0, result="Done"
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
    async def test_worker_retries_same_model_without_chain_when_budget_configured(self) -> None:
        """Explicit worker retry budget allows same-model retries with no chain."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()

        svc = _wire_service(
            event_store,
            llm,
            worker,
            system_limits=_make_system_limits_with_retry(
                escalation_chain=[],
                max_worker_retries=1,
            ),
        )

        boss_id = await svc.create_boss_agent("Same-model retry test")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        assert _get_agent_status(event_store, child_id) == "analyzing"
        retry_events = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, RetryScheduled)
        ]
        assert len(retry_events) == 1
        assert retry_events[0].escalated_model is None

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

    @pytest.mark.asyncio
    async def test_retry_budget_longer_than_chain_reuses_last_model(self) -> None:
        """Worker retries can exceed chain length by reusing the last chain model."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = AlwaysFailWorker()

        svc = _wire_service(
            event_store,
            llm,
            worker,
            system_limits=_make_system_limits_with_retry(
                escalation_chain=["gpt-4o-mini"],
                max_worker_retries=3,
            ),
        )

        boss_id = await svc.create_boss_agent("Chain shorter than retry budget")
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)

        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "analyzing"

        retry_events = [
            e for e in event_store.events_for(child_id)
            if isinstance(e, RetryScheduled)
        ]
        assert len(retry_events) == 3
        assert [e.attempt for e in retry_events] == [1, 2, 3]
        assert [e.escalated_model for e in retry_events] == [
            "gpt-4o-mini",
            "gpt-4o-mini",
            "gpt-4o-mini",
        ]

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

    def test_retry_tracks_last_retry_reason(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("task")
        agent.fail_with_reason("verification feedback")
        agent.schedule_retry(reason="verification feedback", escalated_model=None)

        assert agent.last_retry_reason == "verification feedback"

    def test_verification_retry_uses_separate_counter(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("task")
        agent.mark_verification_failed(
            failed_stage="judge",
            feedback="Missing tests",
            stages_passed=["structural"],
        )
        agent.schedule_retry(
            reason=agent.error_message or "Verification failed",
            is_verification_retry=True,
        )

        retry_event = agent.events[-1]
        assert isinstance(retry_event, RetryScheduled)
        assert retry_event.is_verification_retry is True
        assert agent.retry_count == 0
        assert agent.verification_retry_count == 1


class TestRetryPromptFeedback:
    """Retry prompts include prior-attempt failure context."""

    @pytest.mark.asyncio
    async def test_retry_prompt_includes_previous_attempt_feedback(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = RecordingWorker()
        svc = _wire_service(event_store, llm, worker)

        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config={
                "strategy": "heuristic",
                "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
                "tool": "claude_code",
            },
        )
        agent.assign_task("Investigate failing test")
        agent.fail_with_reason("Unit tests still fail on edge case")
        agent.schedule_retry(
            reason="Unit tests still fail on edge case",
            escalated_model="gpt-4o-mini",
        )
        await event_store.append_batch(agent.events, expected_version=0)
        agent.mark_changes_as_committed()
        svc._limits_registry.set(
            agent.agent_id,
            HierarchyLimits(
                current_depth=0,
                max_depth=-1,
                max_children_per_node=-1,
                root_id=agent.agent_id,
                max_total_agents=-1,
                current_total_agents=1,
            ),
        )

        await svc.run_agent_step(agent.agent_id)

        prompt = worker.task_contexts[-1]["task_description"]
        assert "<previous_attempt_feedback>" in prompt
        assert "Unit tests still fail on edge case" in prompt

        prompt_events = [
            event for event in event_store.events_for(agent.agent_id)
            if isinstance(event, PromptSent)
        ]
        assert prompt_events
        assert "<previous_attempt_feedback>" in prompt_events[-1].prompt

    @pytest.mark.asyncio
    async def test_non_verification_retry_prompt_ignores_stale_verification_feedback(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = RecordingWorker()
        svc = _wire_service(event_store, llm, worker)

        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
            success_criteria="Must include unit tests",
        )
        agent.assign_task("Investigate failing test")
        agent.mark_verification_failed(
            failed_stage="judge",
            feedback="Missing tests",
            stages_passed=["structural"],
        )
        agent.schedule_retry(
            reason=agent.error_message or "Verification failed",
            is_verification_retry=True,
        )
        agent.fail_with_reason("Tool crashed before completing")
        agent.schedule_retry(reason="Tool crashed before completing")
        await event_store.append_batch(agent.events, expected_version=0)
        agent.mark_changes_as_committed()
        svc._limits_registry.set(
            agent.agent_id,
            HierarchyLimits(
                current_depth=0,
                max_depth=-1,
                max_children_per_node=-1,
                root_id=agent.agent_id,
                max_total_agents=-1,
                current_total_agents=1,
            ),
        )

        await svc.run_agent_step(agent.agent_id)

        prompt = worker.task_contexts[-1]["task_description"]
        assert "**Success criteria you MUST satisfy:**" not in prompt
        assert "<previous_attempt_feedback>" in prompt
        assert "Tool crashed before completing" in prompt


class TestVerificationRetryBudgeting:
    """Verification retries are classified and budgeted independently."""

    @pytest.mark.asyncio
    async def test_verification_retries_do_not_consume_worker_retry_budget(self) -> None:
        event_store = InMemoryEventStore()
        repository = AgentRepository(event_store=event_store, max_retries=3)
        policy = RetryPolicy(
            repository=repository,
            retry_config=RetryConfig(
                max_worker_retries=1,
                max_verification_retries=2,
                model_escalation_chain=["gpt-4o-mini"],
            ),
        )
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("Fix the failing test suite")
        await _persist_agent(repository, agent)

        for _ in range(2):
            agent.mark_verification_failed(
                failed_stage="judge",
                feedback="Missing tests",
                stages_passed=["structural"],
            )
            await _persist_agent(repository, agent)
            agent.schedule_retry(
                reason=agent.error_message or "Verification failed",
                is_verification_retry=True,
            )
            await _persist_agent(repository, agent)

        agent.fail_with_reason("Transient worker failure")
        await _persist_agent(repository, agent)

        retried = await policy.maybe_schedule_retry(agent)

        assert retried is True
        assert agent.verification_retry_count == 2
        assert agent.retry_count == 1
        retry_event = event_store.events_for(agent.agent_id)[-1]
        assert isinstance(retry_event, RetryScheduled)
        assert retry_event.is_verification_retry is False
        assert retry_event.escalated_model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_plain_work_failed_after_verification_failure_is_not_misclassified(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = RecordingWorker()
        svc = _wire_service(
            event_store,
            llm,
            worker,
            system_limits=_make_system_limits_with_retry(max_verification_retries=2),
        )
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("Retry classification test")
        await event_store.append_batch(agent.events, expected_version=0)
        agent.mark_changes_as_committed()

        agent.mark_verification_failed(
            failed_stage="judge",
            feedback="Missing evidence",
            stages_passed=["structural"],
        )
        agent.schedule_retry(
            reason=agent.error_message or "Verification failed",
            is_verification_retry=True,
        )
        agent.fail_with_reason("Plain worker failure")

        retried = await svc._maybe_retry_verification(agent)

        assert retried is False
        assert agent.failed_by_verification is False
        assert agent.verification_feedback == "Missing evidence"

    @pytest.mark.asyncio
    async def test_verification_retry_budget_reads_from_config(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = RecordingWorker()
        svc = _wire_service(
            event_store,
            llm,
            worker,
            system_limits=_make_system_limits_with_retry(max_verification_retries=1),
        )
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("Verification budget test")
        await event_store.append_batch(agent.events, expected_version=0)
        agent.mark_changes_as_committed()

        agent.mark_verification_failed(
            failed_stage="judge",
            feedback="Missing tests",
            stages_passed=["structural"],
        )
        await event_store.append_batch(agent.events, expected_version=2)
        agent.mark_changes_as_committed()
        first_retry = await svc._maybe_retry_verification(agent)
        assert first_retry is True
        assert agent.verification_retry_count == 1

        agent.mark_verification_failed(
            failed_stage="judge",
            feedback="Still missing tests",
            stages_passed=["structural"],
        )
        await event_store.append_batch(agent.events, expected_version=4)
        agent.mark_changes_as_committed()
        second_retry = await svc._maybe_retry_verification(agent)

        assert second_retry is False


class TestCircuitBreakerReset:
    """Circuit-breaker state resets after the configured cooldown window."""

    def test_circuit_breaker_resets_after_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repository = AgentRepository(event_store=InMemoryEventStore(), max_retries=3)
        policy = RetryPolicy(
            repository=repository,
            retry_config=RetryConfig(
                model_escalation_chain=["gpt-4o"],
                circuit_breaker_threshold=1,
                circuit_breaker_reset_seconds=10,
            ),
        )
        policy._model_failures["gpt-4o"] = 1
        policy._model_last_failure_time["gpt-4o"] = 0.0

        monkeypatch.setattr(
            "core.application.services.orchestration.retry_policy.time.monotonic",
            lambda: 11.0,
        )

        assert policy._is_circuit_broken("gpt-4o") is False
        assert "gpt-4o" not in policy._model_failures
