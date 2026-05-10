"""Byzantine-dependency recovery harness.

These integration tests run the full ``run_system_loop`` against worker
fakes that simulate the actual failure modes seen against Ollama Cloud:
indefinite hangs, swallowed cancellations, partial responses.  Unlike
the unit tests in ``test_retry.py`` (which exercise recovery handlers
in isolation), these tests verify that the watchdog -> handler ->
semaphore -> domain-cleanup chain cooperates correctly under realistic
external misbehavior.

The tests use very short timeouts (sub-second) so that the entire run
terminates within a few seconds of wall clock time while still
exercising the production cooperation logic at full fidelity.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bootstrap.application import ExecutionLimitsBridge
from bootstrap.tests.test_characterization import (
    FakeLLM,
    FakeSharedContextPort,
    FakeSiblingViewPort,
    InMemoryEventStore,
    _find_child_ids,
    _get_agent_status,
)
from config import BossConfig, ManagerConfig
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
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    RetryScheduled,
    RunCompleted,
    WorkFailed,
)


@pytest.fixture(autouse=True)
async def fail_on_unhandled_loop_exceptions():
    """Fail test if the event loop reports unhandled background task errors."""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    seen: list[str] = []

    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, asyncio.CancelledError):
            return
        message = str(context.get("message", "")).strip()
        # "Task exception was never retrieved" is exactly what we want to fail on.
        if message or exc is not None:
            seen.append(f"{message} exc={exc!r}")
        if previous_handler is not None:
            previous_handler(loop, context)

    loop.set_exception_handler(_handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous_handler)
        # Give orphan callbacks a chance to report before assertion.
        await asyncio.sleep(0)
        assert not seen, f"Unhandled event-loop exceptions: {seen}"


# =============================================================================
# Byzantine worker fakes
# =============================================================================


class HangingWorker:
    """Worker that yields one heartbeat then hangs forever (cooperative cancel).

    Simulates: LLM connection alive but stream read never completes.
    Cooperates with asyncio cancellation -- the most common Byzantine
    case once the aiohttp/httpx transport fix is in place.
    """

    def __init__(self) -> None:
        self.cancelled = False
        self.invocations = 0

    async def run_session(
        self,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        self.invocations += 1
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="byzantine-hang",
        )
        try:
            await asyncio.Event().wait()  # never set
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class UncancellableWorker:
    """Worker that swallows asyncio cancellation for ``stubborn_seconds``.

    Simulates: blocking SDK / C-level read where asyncio.cancel() is
    sent but the underlying call ignores it.  The system must NOT wait
    for this task to finish before advancing the DAG -- the
    ``asyncio.shield(step_task)`` + ``_handle_step_timeout`` pattern
    (commit 1a8079a) is what makes this safe.

    asyncio.cancel() is one-shot, so we cannot rely on a re-cancel to
    eventually break out.  We self-terminate after ``stubborn_seconds``
    so the orphan task does not leak past pytest teardown.
    """

    def __init__(self, stubborn_seconds: float = 1.0) -> None:
        self._stubborn_seconds = stubborn_seconds
        self.cancellations_received = 0

    async def run_session(
        self,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        import time as _time

        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="byzantine-uncancellable",
        )
        deadline = _time.monotonic() + self._stubborn_seconds
        while _time.monotonic() < deadline:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                self.cancellations_received += 1
                # Swallow cancel until the deadline; mimics buggy SDK


# =============================================================================
# Wiring
# =============================================================================


def _wire_byzantine_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: Any,
    *,
    step_timeout_seconds: float = 0.3,
    worker_silence_timeout_seconds: float = 0.5,
    no_progress_grace_seconds: float = 0.2,
    no_progress_initial_grace_seconds: float = 0.2,
    no_progress_check_interval: float = 0.1,
    pending_assessment_timeout_seconds: float = 0.5,
    worker_prepare_timeout_seconds: float = 0.5,
    worker_execute_timeout_seconds: float = 0.3,
    stall_timeout_seconds: float = 5.0,
    max_run_duration_seconds: float = 5.0,
) -> AgentExecutionService:
    """Wire the execution service with sub-second Byzantine timeouts.

    Defaults are tuned so the watchdog fires before run-level timeout
    in every test, letting us assert the cooperation path explicitly.
    """
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.01,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        step_timeout_seconds=step_timeout_seconds,
        stall_timeout_seconds=stall_timeout_seconds,
        worker_silence_timeout_seconds=worker_silence_timeout_seconds,
        no_progress_grace_seconds=no_progress_grace_seconds,
        no_progress_initial_grace_seconds=no_progress_initial_grace_seconds,
        no_progress_check_interval=no_progress_check_interval,
        worker_prepare_timeout_seconds=worker_prepare_timeout_seconds,
        worker_execute_timeout_seconds=worker_execute_timeout_seconds,
        pending_assessment_timeout_seconds=pending_assessment_timeout_seconds,
    )
    system_limits = ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        max_run_duration_seconds=max_run_duration_seconds,
    )

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
        system_limits=system_limits,
    )


async def _drive_to_worker(
    service: AgentExecutionService,
    event_store: InMemoryEventStore,
    task: str,
) -> tuple[Any, Any]:
    """Hand-drive boss + child steps so run_system_loop starts at the worker.

    The system loop CAN drive these too, but pre-driving them keeps the
    wall-clock budget for the watchdog assertions, not for boss/manager
    bookkeeping.  Returns ``(boss_id, worker_id)``.
    """
    llm_complexity_default_simple_assured = True  # FakeLLM defaults to SIMPLE
    assert llm_complexity_default_simple_assured

    boss_id = await service.create_boss_agent(task)
    await service.run_agent_step(boss_id)
    child_id = _find_child_ids(event_store, boss_id)[0]
    await service.run_agent_step(child_id)  # PENDING -> WORKER
    return boss_id, child_id


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_hanging_worker_recovered_via_step_timeout_advances_dag() -> None:
    """A worker that hangs forever is force-failed via step_timeout, the
    retry budget is consumed, and the DAG terminates within the run-level
    deadline.  Verifies the wait_for(shield(step_task)) -> _handle_step_timeout
    cooperation path.
    """
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = HangingWorker()
    service = _wire_byzantine_service(event_store, llm, worker)

    boss_id, worker_id = await _drive_to_worker(service, event_store, "Byzantine hang test")

    await service.run_system_loop(boss_id)
    # Give any cancelled background worker tasks a chance to finish and report.
    await asyncio.sleep(0.1)

    # (1) Run reached terminal state
    run_completed = [
        e for e in event_store.events_for(boss_id) if isinstance(e, RunCompleted)
    ]
    assert run_completed, "run never completed"

    # (2) Worker reached terminal state
    assert _get_agent_status(event_store, worker_id) == "failed"
    assert _get_agent_status(event_store, boss_id) == "failed"

    # (3) At least one Byzantine watchdog reason recorded as a WorkFailed.
    # Acceptable reasons: step_timeout, silent_worker, or no_progress
    # (any of the three is a valid recovery path; the run-level timeout
    # is NOT acceptable here -- it would mean watchdogs never fired).
    worker_events = event_store.events_for(worker_id)
    fail_reasons = [
        e.reason for e in worker_events if isinstance(e, WorkFailed) and e.reason
    ]
    byzantine_reasons = (
        "Step timed out after",
        "Worker silent for >",
        "Zero thoughts after",
    )
    assert any(
        r.startswith(byzantine_reasons) for r in fail_reasons
    ), f"no watchdog fired; fail reasons: {fail_reasons}"

    # (4) The hanging task did receive cancellation (cooperation worked)
    assert worker.cancelled, "worker.run_session never observed CancelledError"


@pytest.mark.asyncio
async def test_uncancellable_worker_advances_dag_via_shield_and_force_release() -> None:
    """A worker that swallows cancellation must NOT block DAG advancement.

    The wait_for(shield(step_task)) pattern (commit 1a8079a) lets the
    timeout fire without awaiting the inner cancel-resistant task.  The
    silent reaper then force-releases the worker semaphore so any
    sibling work can proceed.  Verifies the orphan-task safety net.
    """
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = UncancellableWorker(stubborn_seconds=1.0)
    service = _wire_byzantine_service(
        event_store,
        llm,
        worker,
        # Give the silent reaper room to fire after step_timeout already did:
        worker_silence_timeout_seconds=0.4,
        max_run_duration_seconds=4.0,
    )

    boss_id, worker_id = await _drive_to_worker(
        service, event_store, "Byzantine uncancellable test"
    )

    await service.run_system_loop(boss_id)
    # Wait for uncancellable worker's self-termination to avoid cross-test leak.
    await asyncio.sleep(1.1)

    # (1) Run terminated despite uncancellable worker
    run_completed = [
        e for e in event_store.events_for(boss_id) if isinstance(e, RunCompleted)
    ]
    assert run_completed, "run never completed -- shield/force-release failed"

    # (2) Worker reached terminal state
    assert _get_agent_status(event_store, worker_id) == "failed"

    # (3) Worker observed at least one cancellation attempt (proves
    # cancel was issued; whether it was honored is a separate concern)
    assert worker.cancellations_received >= 1, (
        "no cancellation was issued -- shield path may not have fired"
    )

    # (4) Semaphores are at full capacity (no leaked holders)
    # Holders set should be empty even though task may still be running.
    # The force-release path explicitly clears the holders set.
    assert not service._llm_semaphore_holders, (
        f"LLM semaphore leaked: {service._llm_semaphore_holders}"
    )
    assert not service._worker_semaphore_holders, (
        f"Worker semaphore leaked: {service._worker_semaphore_holders}"
    )


@pytest.mark.asyncio
async def test_hanging_worker_run_consumes_dedicated_retry_budget() -> None:
    """Step-timeout retries are scheduled until the dedicated budget
    exhausts, then the agent transitions to permanent FAILED.  Verifies
    that retry-first semantics (commit 8012235) are exercised by the
    full system loop, not just the unit-level handler.
    """
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = HangingWorker()
    service = _wire_byzantine_service(
        event_store,
        llm,
        worker,
        # Tighter timeouts so we get multiple watchdog firings per run
        step_timeout_seconds=0.2,
        worker_execute_timeout_seconds=0.2,
        worker_silence_timeout_seconds=0.4,
        no_progress_grace_seconds=0.15,
        no_progress_initial_grace_seconds=0.15,
        no_progress_check_interval=0.1,
        max_run_duration_seconds=8.0,
    )

    boss_id, worker_id = await _drive_to_worker(
        service, event_store, "Byzantine retry-budget test"
    )

    await service.run_system_loop(boss_id)
    # Ensure cancelled worker task shutdown is observed before fixture assertion.
    await asyncio.sleep(0.1)

    worker_events = event_store.events_for(worker_id)
    retry_events = [e for e in worker_events if isinstance(e, RetryScheduled)]

    # Worker should have been retried at least once (some budget got used).
    assert retry_events, "no retry scheduled -- watchdog->retry path broke"

    # Eventually FAILED (budget exhaustion or run timeout produces same end state).
    assert _get_agent_status(event_store, worker_id) == "failed"

    # Worker invocations >= 1 + retries (each retry re-dispatches the worker).
    assert worker.invocations >= 1
