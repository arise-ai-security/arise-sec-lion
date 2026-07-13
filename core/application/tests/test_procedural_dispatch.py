"""Procedural dispatch tests: registry match, host-side execution, agentic escalation."""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

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
    ChildCompleted,
    ChildFailed,
    CodeGenerationStarted,
    DomainEvent,
    FailureDigestRecorded,
    PatchPlanApproved,
    PostStepCompleted,
    PostStepRequested,
    ProcedureExecutionFinished,
    ProcedureExecutionStarted,
    RetryScheduled,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import ConcurrencyError, EventStoreError
from core.domain.values.procedure import (
    ProcedureEvidence,
    ProcedurePlanApproval,
    ProcedureResult,
)


class FakeProcedureExecutor:
    """Registry with one procedure keyed on a bracket marker."""

    def __init__(
        self,
        result: ProcedureResult | None = None,
        raises: Exception | None = None,
        results: tuple[ProcedureResult, ...] | None = None,
    ) -> None:
        if result is not None and results is not None:
            raise ValueError("provide result or results, not both")
        self.execute_calls: list[str] = []
        self.params_calls: list[dict[str, Any]] = []
        self._results = results or ((result,) if result is not None else ())
        self._raises = raises

    def match(self, task_description: str, domain_context: object | None) -> str | None:
        return "fake_validation" if "[Fake-Procedure]" in task_description else None

    def resolve(self, procedure_ref: str) -> bool:
        return procedure_ref == "fake_validation"

    async def execute(
        self,
        procedure_ref: str,
        task_description: str,
        domain_context: object | None,
        params: dict[str, Any],
    ) -> ProcedureResult:
        self.execute_calls.append(procedure_ref)
        self.params_calls.append(dict(params))
        if self._raises is not None:
            raise self._raises
        assert self._results
        index = min(len(self.execute_calls) - 1, len(self._results) - 1)
        return self._results[index]


class RecordingWorker:
    """Agentic worker fake that records the prompt it received and succeeds."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        self.prompts.append(task_context["task_description"])
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(aggregate_id=agent_id, sequence_number=0, tool_name="fake")
        yield WorkCompleted(aggregate_id=agent_id, sequence_number=0, result="agentic done")


class FailingWorker:
    """Agentic worker fake that records one failed recovery attempt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        self.prompts.append(task_context["task_description"])
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(aggregate_id=agent_id, sequence_number=0, tool_name="fake")
        yield ThoughtCaptured(
            aggregate_id=agent_id,
            sequence_number=0,
            content="agentic fallback inspected the rejected artifact",
            output_type="tool_result",
            tool_name="inspect",
        )
        yield WorkFailed(aggregate_id=agent_id, sequence_number=0, reason="agentic failed")


class RaisingWorker:
    """Agentic worker fake that raises before emitting a terminal event."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def run_session(self, _task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        raise RuntimeError(self._message)
        yield


class FinishCheckpointConflictStore(InMemoryEventStore):
    """Event store that conflicts once after a procedure has executed."""

    def __init__(self) -> None:
        super().__init__()
        self.target_agent_id: UUID | None = None
        self.conflicted = False

    async def append_batch(self, events: list[DomainEvent]) -> None:
        if (
            self.target_agent_id is not None
            and not self.conflicted
            and events[0].aggregate_id == self.target_agent_id
            and any(isinstance(event, ProcedureExecutionFinished) for event in events)
        ):
            self.conflicted = True
            raise ConcurrencyError(
                aggregate_id=str(self.target_agent_id),
                actual_version=len(self._events[self.target_agent_id]),
            )
        await super().append_batch(events)


class TerminalCheckpointConflictStore(InMemoryEventStore):
    """Event store that conflicts once while persisting a procedure terminal."""

    def __init__(self) -> None:
        super().__init__()
        self.target_agent_id: UUID | None = None
        self.conflicted = False

    def _should_conflict(self, events: list[DomainEvent]) -> bool:
        return (
            self.target_agent_id is not None
            and not self.conflicted
            and events[0].aggregate_id == self.target_agent_id
            and any(isinstance(event, (WorkCompleted, WorkFailed)) for event in events)
        )

    def _raise_conflict(self) -> None:
        assert self.target_agent_id is not None
        self.conflicted = True
        raise ConcurrencyError(
            aggregate_id=str(self.target_agent_id),
            actual_version=len(self._events[self.target_agent_id]),
        )

    async def append(self, event: DomainEvent) -> None:
        if self._should_conflict([event]):
            self._raise_conflict()
        await super().append(event)

    async def append_batch(self, events: list[DomainEvent]) -> None:
        if self._should_conflict(events):
            self._raise_conflict()
        await super().append_batch(events)


class PostStepMarkerFailureStore(InMemoryEventStore):
    """Event store that rejects the first post-step completion marker."""

    def __init__(self) -> None:
        super().__init__()
        self.target_agent_id: UUID | None = None
        self.marker_failed = False

    async def append(self, event: DomainEvent) -> None:
        if (
            self.target_agent_id == event.aggregate_id
            and isinstance(event, PostStepCompleted)
            and not self.marker_failed
        ):
            self.marker_failed = True
            raise EventStoreError("simulated post-step marker write failure")
        await super().append(event)


class FailingCleanupPlugin:
    """Domain-plugin stub whose worker cleanup fails a fixed number of times."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.cleanup_calls = 0

    async def cleanup_worker_execution(self, **_kwargs: Any) -> None:
        self.cleanup_calls += 1
        if self.cleanup_calls <= self._failures:
            raise RuntimeError("cleanup failed")


class RaisingVerificationPipeline:
    """Verifier stub that models an infrastructure error before a verdict."""

    async def verify(self, _agent: Any) -> None:
        raise RuntimeError("verification unavailable")


def _wire_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: Any,
    procedure_executor: FakeProcedureExecutor,
    *,
    domain_plugin: Any | None = None,
    verification_pipeline: Any | None = None,
) -> AgentExecutionService:
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.1,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )
    limits = ExecutionLimitsBridge(
        max_depth=-1, max_children_per_node=-1, max_total_agents=-1, max_concurrent_workers=-1
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
        skip_judge=True,
        procedure_executor=procedure_executor,
    )
    if verification_pipeline is not None:
        orchestrator._verification_pipeline = verification_pipeline
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
        domain_plugin=domain_plugin,
    )
    return AgentExecutionService(
        event_store=event_store, dependencies=dependencies, config=config, system_limits=limits
    )


def _subtask_json(description: str, **extra: Any) -> str:
    return json.dumps([{"description": description, "config": FakeLLM.VALID_CONFIG, **extra}])


async def _spawn_worker(svc: AgentExecutionService, event_store: InMemoryEventStore):
    boss_id = await svc.create_boss_agent("Top task")
    await svc.run_agent_step(boss_id)
    child_id = _find_child_ids(event_store, boss_id)[0]
    await svc.run_agent_step(child_id)  # assess → WORKER
    return boss_id, child_id


def _events_for(event_store: InMemoryEventStore, agent_id) -> list[DomainEvent]:
    return event_store._events[agent_id]


async def _persist_terminal_without_post_step(
    svc: AgentExecutionService,
    agent_id: UUID,
) -> None:
    agent = await svc._load_agent_with_context(agent_id)
    await svc._dispatch_agent_action(agent)
    await svc._persist_agent_events(agent)


@pytest.mark.asyncio
async def test_matched_task_executes_procedurally_without_llm_turns() -> None:
    """A registry-matched task completes host-side: no prompt, no worker session."""

    # Given: a worker task matching the fake registry and a succeeding procedure
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=True,
            summary="VERDICT: PASS",
            evidence=(
                ProcedureEvidence(argv=("tool", "validate"), exit_code=0, output_sha256="ab" * 32),
            ),
        )
    )
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)

    # When: the worker step runs
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: the procedure executed and the worker completed with its summary
    assert executor.execute_calls == ["fake_validation"]
    assert _get_agent_status(event_store, child_id) == "completed"
    events = _events_for(event_store, child_id)
    assert any(isinstance(e, ProcedureExecutionStarted) for e in events)
    finished = [e for e in events if isinstance(e, ProcedureExecutionFinished)]
    assert len(finished) == 1 and finished[0].success and finished[0].evidence

    # And: no agentic machinery ran — no prompt, no worker session
    assert worker.prompts == []
    assert not any(isinstance(e, CodeGenerationStarted) for e in events)


@pytest.mark.asyncio
async def test_failed_procedure_gets_agentic_repair_then_host_recheck() -> None:
    """A failed procedure gets one repair prompt and a successful Host recheck."""

    # Given: a matching task whose procedure fails with a diagnostic digest
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        results=(
            ProcedureResult(
                success=False,
                summary="VERDICT: FAIL — signature mismatch",
                digest="FAILURE: artifact signature mismatch (observed vs expected)",
            ),
            ProcedureResult(success=True, summary="VERDICT: PASS after Host recheck"),
        )
    )
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)

    # When: the procedural attempt fails
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: the failure recorded the procedure's digest and scheduled a retry
    events = _events_for(event_store, child_id)
    digests = [e for e in events if isinstance(e, FailureDigestRecorded)]
    assert len(digests) == 1 and digests[0].source == "procedure_failure"
    retries = [e for e in events if isinstance(e, RetryScheduled)]
    assert len(retries) == 1 and "Procedural attempt failed" in retries[0].reason
    assert _get_agent_status(event_store, child_id) == "analyzing"

    # When: the retry step runs
    await svc.run_agent_step(child_id)

    # Then: dispatch went agentic once with the digest, then Host-rechecked the repair.
    assert executor.execute_calls == ["fake_validation", "fake_validation"]
    assert len(worker.prompts) == 1
    assert "Previous Attempt Failure" in worker.prompts[0]
    assert "signature mismatch" in worker.prompts[0]
    assert _get_agent_status(event_store, child_id) == "completed"
    finished = [
        event
        for event in _events_for(event_store, child_id)
        if isinstance(event, ProcedureExecutionFinished)
    ]
    assert [event.success for event in finished] == [False, True]
    assert len([e for e in _events_for(event_store, child_id) if isinstance(e, WorkCompleted)]) == 1
    assert [params["procedure_attempt"] for params in executor.params_calls] == [1, 2]


@pytest.mark.asyncio
async def test_occ_after_procedure_execution_resumes_same_host_attempt() -> None:
    event_store = FinishCheckpointConflictStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=False,
            summary="VERDICT: FAIL",
            digest="FAILURE: invalid artifact",
        )
    )
    svc = _wire_service(event_store, llm, RecordingWorker(), executor)
    _, child_id = await _spawn_worker(svc, event_store)
    event_store.target_agent_id = child_id

    await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    assert event_store.conflicted
    assert executor.execute_calls == ["fake_validation", "fake_validation"]
    assert [params["procedure_attempt"] for params in executor.params_calls] == [1, 1]
    assert [params["procedure_resume"] for params in executor.params_calls] == [False, True]
    assert len([event for event in events if isinstance(event, ProcedureExecutionStarted)]) == 1
    assert len([event for event in events if isinstance(event, ProcedureExecutionFinished)]) == 1
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"


@pytest.mark.asyncio
async def test_occ_after_successful_procedure_outcome_finalizes_without_reexecution() -> None:
    event_store = TerminalCheckpointConflictStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(success=True, summary="VERDICT: PASS")
    )
    svc = _wire_service(event_store, llm, RecordingWorker(), executor)
    _, child_id = await _spawn_worker(svc, event_store)
    event_store.target_agent_id = child_id

    await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    assert event_store.conflicted
    assert executor.execute_calls == ["fake_validation"]
    assert [params["procedure_attempt"] for params in executor.params_calls] == [1]
    assert len([event for event in events if isinstance(event, ProcedureExecutionStarted)]) == 1
    assert len([event for event in events if isinstance(event, ProcedureExecutionFinished)]) == 1
    assert len([event for event in events if isinstance(event, WorkCompleted)]) == 1
    assert _get_agent_status(event_store, child_id) == "completed"


@pytest.mark.asyncio
async def test_occ_after_failed_procedure_outcome_finalizes_without_reexecution() -> None:
    event_store = TerminalCheckpointConflictStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=False,
            summary="VERDICT: FAIL",
            digest="FAILURE: invalid artifact",
        )
    )
    svc = _wire_service(event_store, llm, RecordingWorker(), executor)
    _, child_id = await _spawn_worker(svc, event_store)
    event_store.target_agent_id = child_id

    await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    assert event_store.conflicted
    assert executor.execute_calls == ["fake_validation"]
    assert [params["procedure_attempt"] for params in executor.params_calls] == [1]
    assert len([event for event in events if isinstance(event, ProcedureExecutionStarted)]) == 1
    assert len([event for event in events if isinstance(event, ProcedureExecutionFinished)]) == 1
    assert len([event for event in events if isinstance(event, WorkFailed)]) == 1
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"


@pytest.mark.asyncio
async def test_agentic_completion_is_not_checkpointed_before_verification() -> None:
    """A verifier error cannot leave a durable unverified worker completion."""

    # Given: an ordinary agentic worker whose verifier raises before a verdict
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("plain agentic task")
    executor = FakeProcedureExecutor()
    svc = _wire_service(
        event_store,
        llm,
        RecordingWorker(),
        executor,
        verification_pipeline=RaisingVerificationPipeline(),
    )
    _, child_id = await _spawn_worker(svc, event_store)

    # When: verification fails after the worker emitted WorkCompleted
    with pytest.raises(RuntimeError, match="verification unavailable"):
        await svc.run_agent_step(child_id)

    # Then: only the execution failure is durable; unverified success is absent
    events = _events_for(event_store, child_id)
    assert not any(isinstance(event, WorkCompleted) for event in events)
    assert len([event for event in events if isinstance(event, WorkFailed)]) == 1


@pytest.mark.asyncio
async def test_procedure_verification_error_preserves_finished_evidence() -> None:
    """A verifier error preserves Host evidence without persisting completion."""

    # Given: a successful procedure with Host evidence and a verifier that raises
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=True,
            summary="VERDICT: PASS",
            evidence=(
                ProcedureEvidence(
                    argv=("tool", "validate"),
                    exit_code=0,
                    output_sha256="ab" * 32,
                ),
            ),
        )
    )
    svc = _wire_service(
        event_store,
        llm,
        RecordingWorker(),
        executor,
        verification_pipeline=RaisingVerificationPipeline(),
    )
    _, child_id = await _spawn_worker(svc, event_store)

    # When: verification raises after the procedure finishes
    with pytest.raises(RuntimeError, match="verification unavailable"):
        await svc.run_agent_step(child_id)

    # Then: Host evidence survived, while no unverified completion was persisted
    events = _events_for(event_store, child_id)
    finished = [event for event in events if isinstance(event, ProcedureExecutionFinished)]
    assert len(finished) == 1 and finished[0].success and finished[0].evidence
    assert not any(isinstance(event, WorkCompleted) for event in events)
    assert len([event for event in events if isinstance(event, WorkFailed)]) == 1


@pytest.mark.asyncio
async def test_cleanup_error_preserves_verified_procedure_terminal() -> None:
    """Cleanup cannot replace a verified procedure completion with failure."""

    # Given: a successful procedure followed by failing runtime cleanup
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=True,
            summary="VERDICT: PASS",
            evidence=(
                ProcedureEvidence(
                    argv=("tool", "validate"),
                    exit_code=0,
                    output_sha256="ab" * 32,
                ),
            ),
        )
    )
    cleanup = FailingCleanupPlugin(failures=1)
    svc = _wire_service(
        event_store,
        llm,
        RecordingWorker(),
        executor,
        domain_plugin=cleanup,
    )
    _, child_id = await _spawn_worker(svc, event_store)

    # When: cleanup raises after the verified terminal checkpoint
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await svc.run_agent_step(child_id)

    # Then: completion and Host evidence remain durable without a second terminal
    events = _events_for(event_store, child_id)
    assert len([event for event in events if isinstance(event, ProcedureExecutionFinished)]) == 1
    assert len([event for event in events if isinstance(event, WorkCompleted)]) == 1
    assert not any(isinstance(event, WorkFailed) for event in events)
    assert _get_agent_status(event_store, child_id) == "completed"


@pytest.mark.asyncio
async def test_failed_procedure_cleanup_error_retains_single_repair() -> None:
    """Cleanup after a failed procedure cannot consume its one repair attempt."""

    # Given: the initial Host attempt fails, cleanup fails once, and recheck succeeds
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        results=(
            ProcedureResult(
                success=False,
                summary="VERDICT: FAIL",
                digest="FAILURE: invalid artifact",
            ),
            ProcedureResult(success=True, summary="VERDICT: PASS after repair"),
        )
    )
    worker = RecordingWorker()
    cleanup = FailingCleanupPlugin(failures=1)
    svc = _wire_service(
        event_store,
        llm,
        worker,
        executor,
        domain_plugin=cleanup,
    )
    _, child_id = await _spawn_worker(svc, event_store)

    # When: cleanup fails after the initial procedure's durable failure
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await svc.run_agent_step(child_id)

    # Then: failure evidence is retained and exactly one repair is scheduled
    first_events = _events_for(event_store, child_id)
    assert len([event for event in first_events if isinstance(event, WorkFailed)]) == 1
    assert len([event for event in first_events if isinstance(event, RetryScheduled)]) == 1
    assert len(
        [event for event in first_events if isinstance(event, ProcedureExecutionFinished)]
    ) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"

    # When: the scheduled repair completes and the Host rechecks it
    await svc.run_agent_step(child_id)

    # Then: one agentic repair and one recheck complete the role
    events = _events_for(event_store, child_id)
    assert len(worker.prompts) == 1
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert len([event for event in events if isinstance(event, WorkFailed)]) == 1
    assert len([event for event in events if isinstance(event, WorkCompleted)]) == 1
    assert [params["procedure_attempt"] for params in executor.params_calls] == [1, 2]
    assert _get_agent_status(event_store, child_id) == "completed"


@pytest.mark.asyncio
async def test_failed_procedure_gets_exactly_one_agentic_attempt() -> None:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=False,
            summary="VERDICT: FAIL",
            digest="FAILURE: procedure rejected the artifact",
        )
    )
    worker = FailingWorker()
    svc = _wire_service(event_store, llm, worker, executor)
    boss_id, child_id = await _spawn_worker(svc, event_store)

    await svc.run_agent_step(child_id)
    await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    digests = [event for event in events if isinstance(event, FailureDigestRecorded)]
    assert [digest.source for digest in digests] == [
        "procedure_failure",
        "worker_crash",
    ]
    assert "FAILURE: agentic failed" in digests[-1].digest
    assert "agentic fallback inspected the rejected artifact" in digests[-1].digest
    parent_failures = [
        event for event in _events_for(event_store, boss_id) if isinstance(event, ChildFailed)
    ]
    assert parent_failures[-1].digest == digests[-1].digest
    assert executor.execute_calls == ["fake_validation"]
    assert len(worker.prompts) == 1
    assert _get_agent_status(event_store, child_id) == "failed"


@pytest.mark.asyncio
async def test_agentic_worker_exception_records_digest_and_schedules_retry() -> None:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("plain agentic task")
    svc = _wire_service(
        event_store,
        llm,
        RaisingWorker("ordinary worker crashed"),
        FakeProcedureExecutor(),
    )
    boss_id, child_id = await _spawn_worker(svc, event_store)

    with pytest.raises(RuntimeError, match="ordinary worker crashed"):
        await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    digests = [event for event in events if isinstance(event, FailureDigestRecorded)]
    assert len(digests) == 1
    assert digests[0].source == "worker_crash"
    assert "ordinary worker crashed" in digests[0].digest
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"
    assert not any(
        isinstance(event, ChildFailed) for event in _events_for(event_store, boss_id)
    )


@pytest.mark.asyncio
async def test_agentic_repair_exception_replaces_procedure_digest() -> None:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=False,
            summary="VERDICT: FAIL",
            digest="FAILURE: original procedure mismatch",
        )
    )
    svc = _wire_service(
        event_store,
        llm,
        RaisingWorker("agentic repair crashed"),
        executor,
    )
    boss_id, child_id = await _spawn_worker(svc, event_store)

    await svc.run_agent_step(child_id)
    with pytest.raises(RuntimeError, match="agentic repair crashed"):
        await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    digests = [event for event in events if isinstance(event, FailureDigestRecorded)]
    assert [digest.source for digest in digests] == [
        "procedure_failure",
        "worker_crash",
    ]
    assert "agentic repair crashed" in digests[-1].digest
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert _get_agent_status(event_store, child_id) == "failed"
    parent_failures = [
        event for event in _events_for(event_store, boss_id) if isinstance(event, ChildFailed)
    ]
    assert parent_failures[-1].digest == digests[-1].digest


@pytest.mark.asyncio
async def test_failed_host_recheck_does_not_get_second_agentic_attempt() -> None:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        results=(
            ProcedureResult(
                success=False,
                summary="VERDICT: FAIL — original",
                digest="FAILURE: original mismatch",
            ),
            ProcedureResult(
                success=False,
                summary="VERDICT: FAIL — Host recheck",
                digest="FAILURE: repaired artifact still mismatched",
            ),
        )
    )
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)
    boss_id, child_id = await _spawn_worker(svc, event_store)

    await svc.run_agent_step(child_id)
    await svc.run_agent_step(child_id)

    assert executor.execute_calls == ["fake_validation", "fake_validation"]
    assert len(worker.prompts) == 1
    events = _events_for(event_store, child_id)
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert _get_agent_status(event_store, child_id) == "failed"
    parent_failures = [
        event for event in _events_for(event_store, boss_id) if isinstance(event, ChildFailed)
    ]
    assert parent_failures[-1].digest == "FAILURE: repaired artifact still mismatched"


@pytest.mark.asyncio
async def test_procedure_records_plan_approval_provenance() -> None:
    """A host-approved plan is frozen on the worker event stream."""

    # Given: A matched procedure returning host validation provenance
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] render the patch")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=True,
            summary="patch rendered",
            plan_approval=ProcedurePlanApproval(
                plan_sha256="ab" * 32,
                evidence_references=("/workspace/analysis.txt",),
            ),
        )
    )
    svc = _wire_service(event_store, llm, RecordingWorker(), executor)

    # When: The procedure completes
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: PatchPlanApproved records the host-frozen identity
    approvals = [
        event
        for event in _events_for(event_store, child_id)
        if isinstance(event, PatchPlanApproved)
    ]
    assert len(approvals) == 1
    assert approvals[0].plan_sha256 == "ab" * 32


@pytest.mark.asyncio
async def test_infrastructure_fault_fails_open_to_agentic() -> None:
    """An executor exception becomes a failed result, not a crash."""

    # Given: a matching task whose executor raises
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(raises=RuntimeError("container gone"))
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)

    # When: the procedural attempt hits the infrastructure fault
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: the fault converted to a failed procedure and escalation was scheduled
    events = _events_for(event_store, child_id)
    finished = [e for e in events if isinstance(e, ProcedureExecutionFinished)]
    assert len(finished) == 1 and not finished[0].success
    assert "infrastructure fault" in finished[0].summary
    assert _get_agent_status(event_store, child_id) == "analyzing"


@pytest.mark.asyncio
async def test_parent_agentic_hint_cannot_bypass_registry_match() -> None:
    """Only the Host registry decides whether a matching task is procedural."""

    # Given: a matching description explicitly marked agentic by the parent
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json(
        "[Fake-Procedure] validate the artifact", execution_mode="agentic"
    )
    executor = FakeProcedureExecutor(
        result=ProcedureResult(success=True, summary="unused")
    )
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)

    # When: the worker step runs
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: the Host registry won; the untrusted execution hint was ignored
    assert executor.execute_calls == ["fake_validation"]
    assert worker.prompts == []
    assert _get_agent_status(event_store, child_id) == "completed"


@pytest.mark.asyncio
async def test_parent_procedure_hint_cannot_select_registry_entry() -> None:
    """A parent cannot dispatch a procedure for a task the Host registry did not match."""

    # Given: a registered procedure hint on a task with no Host registry match
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json(
        "plain task, no bracket",
        execution_mode="procedural",
        procedure_ref="fake_validation",
    )
    executor = FakeProcedureExecutor(
        result=ProcedureResult(success=True, summary="unused")
    )
    worker = RecordingWorker()
    svc = _wire_service(event_store, llm, worker, executor)

    # When: the worker step runs
    _, child_id = await _spawn_worker(svc, event_store)
    await svc.run_agent_step(child_id)

    # Then: no Host match means agentic execution, despite the registered hint
    assert executor.execute_calls == []
    assert len(worker.prompts) == 1
    assert _get_agent_status(event_store, child_id) == "completed"


@pytest.mark.asyncio
async def test_sigkill_after_failed_procedure_terminal_persistence_schedules_repair() -> None:
    """A durable request lets reconciliation schedule the lost agentic repair."""

    # Given: a failed initial procedure whose terminal batch was persisted before SIGKILL
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    svc = _wire_service(
        event_store,
        llm,
        RecordingWorker(),
        FakeProcedureExecutor(
            result=ProcedureResult(
                success=False,
                summary="VERDICT: FAIL",
                digest="FAILURE: invalid artifact",
            )
        ),
    )
    _, child_id = await _spawn_worker(svc, event_store)
    await _persist_terminal_without_post_step(svc, child_id)

    # Then: the failed terminal has a request but no completion marker.
    before_reconciliation = _events_for(event_store, child_id)
    assert len([event for event in before_reconciliation if isinstance(event, WorkFailed)]) == 1
    assert len(
        [event for event in before_reconciliation if isinstance(event, PostStepRequested)]
    ) == 1
    assert not any(isinstance(event, PostStepCompleted) for event in before_reconciliation)
    assert child_id in await svc._query_service.get_active_agent_ids()

    # When: a replacement process reconciles the scheduled terminal agent.
    await svc.run_agent_step(child_id)

    # Then: it schedules exactly the required agentic repair and records completion.
    after_reconciliation = _events_for(event_store, child_id)
    assert len([event for event in after_reconciliation if isinstance(event, RetryScheduled)]) == 1
    assert len(
        [event for event in after_reconciliation if isinstance(event, PostStepCompleted)]
    ) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"


@pytest.mark.asyncio
async def test_retry_survives_post_step_marker_write_failure() -> None:
    """A marker-write failure reconciles pending work without a second failure event."""

    event_store = PostStepMarkerFailureStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    svc = _wire_service(
        event_store,
        llm,
        RecordingWorker(),
        FakeProcedureExecutor(
            result=ProcedureResult(
                success=False,
                summary="VERDICT: FAIL",
                digest="FAILURE: invalid artifact",
            )
        ),
    )
    _, child_id = await _spawn_worker(svc, event_store)
    event_store.target_agent_id = child_id

    with pytest.raises(EventStoreError, match="marker write failure"):
        await svc.run_agent_step(child_id)

    events = _events_for(event_store, child_id)
    assert event_store.marker_failed
    assert len([event for event in events if isinstance(event, WorkFailed)]) == 1
    assert len([event for event in events if isinstance(event, RetryScheduled)]) == 1
    assert len([event for event in events if isinstance(event, PostStepRequested)]) == 1
    assert len([event for event in events if isinstance(event, PostStepCompleted)]) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"


@pytest.mark.asyncio
async def test_sigkill_after_child_completion_terminal_persistence_notifies_parent() -> None:
    """A completed child remains schedulable until its parent notification is durable."""

    # Given: a completed child whose terminal batch was persisted before SIGKILL.
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("plain agentic task")
    svc = _wire_service(event_store, llm, RecordingWorker(), FakeProcedureExecutor())
    boss_id, child_id = await _spawn_worker(svc, event_store)
    await _persist_terminal_without_post_step(svc, child_id)

    # Then: the terminal child is scheduled solely for its unmatched post-step request.
    assert child_id in await svc._query_service.get_active_agent_ids()
    assert not any(
        isinstance(event, ChildCompleted) for event in _events_for(event_store, boss_id)
    )

    # When: a replacement process reconciles the child.
    await svc.run_agent_step(child_id)

    # Then: the parent receives one completion and the child's request is completed.
    parent_events = _events_for(event_store, boss_id)
    child_events = _events_for(event_store, child_id)
    assert len([event for event in parent_events if isinstance(event, ChildCompleted)]) == 1
    assert len([event for event in child_events if isinstance(event, PostStepCompleted)]) == 1


@pytest.mark.asyncio
async def test_repeated_post_step_reconciliation_emits_no_duplicates() -> None:
    """Replaying an already-completed post-step does not duplicate durable effects."""

    # Given: a completed child whose persisted terminal outcome is awaiting reconciliation.
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("plain agentic task")
    svc = _wire_service(event_store, llm, RecordingWorker(), FakeProcedureExecutor())
    boss_id, child_id = await _spawn_worker(svc, event_store)
    await _persist_terminal_without_post_step(svc, child_id)

    # When: reconciliation is invoked twice from independently reloaded aggregates.
    first = await svc._load_agent_with_context(child_id)
    await svc._handle_post_step(first, [])
    second = await svc._load_agent_with_context(child_id)
    await svc._handle_post_step(second, [])

    # Then: parent notification and request completion are each recorded once.
    parent_events = _events_for(event_store, boss_id)
    child_events = _events_for(event_store, child_id)
    assert len([event for event in parent_events if isinstance(event, ChildCompleted)]) == 1
    assert len([event for event in child_events if isinstance(event, PostStepCompleted)]) == 1
