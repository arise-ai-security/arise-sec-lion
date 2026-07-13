"""Procedural dispatch tests: registry match, host-side execution, agentic escalation."""

import json
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
    ChildFailed,
    CodeGenerationStarted,
    DomainEvent,
    FailureDigestRecorded,
    PatchPlanApproved,
    ProcedureExecutionFinished,
    ProcedureExecutionStarted,
    RetryScheduled,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.values.procedure import (
    ProcedureEvidence,
    ProcedurePlanApproval,
    ProcedureResult,
)


class FakeProcedureExecutor:
    """Registry with one procedure keyed on a bracket marker."""

    def __init__(self, result: ProcedureResult | None = None, raises: Exception | None = None):
        self.execute_calls: list[str] = []
        self._result = result
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
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


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


def _wire_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: Any,
    procedure_executor: FakeProcedureExecutor,
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
async def test_failed_procedure_escalates_agentic_with_digest() -> None:
    """A failed procedure retries agentically; the prompt carries its digest."""

    # Given: a matching task whose procedure fails with a diagnostic digest
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    llm._default_subtasks = _subtask_json("[Fake-Procedure] validate the artifact")
    executor = FakeProcedureExecutor(
        result=ProcedureResult(
            success=False,
            summary="VERDICT: FAIL — signature mismatch",
            digest="FAILURE: artifact signature mismatch (observed vs expected)",
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

    # Then: dispatch went agentic (no second procedure call) with the digest injected
    assert executor.execute_calls == ["fake_validation"]
    assert len(worker.prompts) == 1
    assert "Previous Attempt Failure" in worker.prompts[0]
    assert "signature mismatch" in worker.prompts[0]
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
