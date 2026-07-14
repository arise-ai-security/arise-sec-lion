"""Integration test: flat-mode dispatch through ``AgentExecutionService``.

A fake ``WorkerPort`` returns a deterministic ``WorkerResult``. The full
service is wired with the in-memory fakes from ``bootstrap/tests``; we assert
that flat mode skips BOSS decomposition entirely and produces the expected
event sequence on the BOSS aggregate.

These tests are marked ``integration`` because they exercise multiple layers
end-to-end (composition root, ExecutionService loop, AgentSession aggregate,
domain events).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bootstrap.tests.test_characterization import (
    FakeLLM,
    FakeRealtimeCallback,
    FakeSharedContextPort,
    FakeSiblingViewPort,
    FakeWorkerTool,
    InMemoryEventStore,
)
from config import BossConfig, ManagerConfig
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    FlatModeBundle,
    HierarchyLimitsRegistry,
    ServiceConfig,
)
from core.application.run_invariants import (
    TaskPromptSpec,
    TimeoutBudget,
    ToolPolicy,
    WorkerResult,
    WorkspaceSpec,
)
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    ParentNotificationService,
    PromptBuilder,
)
from core.domain.events.events import (
    AgentCreated,
    AgentExecutionFinished,
    AgentExecutionStarted,
    CodeGenerationStarted,
    OperationFinished,
    OperationStarted,
    PostStepCompleted,
    PostStepRequested,
    PromptSent,
    RunCompleted,
    RunStarted,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from core.application.execution_service import FlatInvariantBuilder
    from core.ports.worker_port import WorkerPort


pytestmark = pytest.mark.integration


# =============================================================================
# Fakes
# =============================================================================


class FakeFlatWorker:
    """Minimal ``WorkerPort`` that returns a canned ``WorkerResult``."""

    def __init__(self, *, exit_status: str = "completed", summary: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._exit_status = exit_status
        self._summary = summary or "Flat-mode worker finished."

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult:
        self.calls.append(
            {
                "run_id": run_id,
                "spec": spec,
                "tool_policy": tool_policy,
                "timeouts": timeouts,
                "workspace": workspace,
            }
        )
        return WorkerResult(
            run_id=run_id,
            exit_status=self._exit_status,  # type: ignore[arg-type]
            wall_time_seconds=0.42,
            output_summary=self._summary,
        )


def _make_invariant_builder() -> FlatInvariantBuilder:
    """Return a closure producing deterministic invariants for tests."""

    def _build(*, task: str, domain_context: object | None, run_dir: Path) -> FlatModeBundle:
        del domain_context
        spec = TaskPromptSpec(
            rendered_prompt=f"BRIEFING\n\nTask: {task}",
            prompt_sha="abc123",
            domain_context=None,
            task=task,
        )
        policy = ToolPolicy(
            allowed=("Bash",),
            disallowed=(),
            allowed_bash_commands=(),
        )
        budget = TimeoutBudget(per_worker_call=300, per_run_total=1800)
        workspace = WorkspaceSpec(root=run_dir, extras={})
        return FlatModeBundle(
            spec=spec,
            tool_policy=policy,
            timeouts=budget,
            workspace=workspace,
        )

    return _build


# =============================================================================
# Wiring helper (mirrors bootstrap.tests but injects flat-mode bits).
# =============================================================================


class _LimitsBridge:
    """Minimal SystemLimitsPort for tests."""

    max_depth: int = -1
    max_children_per_node: int = -1
    max_total_agents: int = -1
    max_concurrent_workers: int = -1
    max_concurrent_llm_calls: int = -1
    llm_jitter_max_ms: int = 0
    max_agent_step_seconds: float = -1
    max_run_duration_seconds: float = 1800

    def is_workers_limited(self) -> bool:
        return False

    def is_llm_limited(self) -> bool:
        return False


def _wire_flat_service(
    *,
    flat_worker: WorkerPort,
    output_directory: Path,
    invariant_builder: FlatInvariantBuilder | None = None,
) -> tuple[AgentExecutionService, InMemoryEventStore]:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker_tool = FakeWorkerTool()
    shared_context = FakeSharedContextPort()
    sibling_view = FakeSiblingViewPort()
    realtime = FakeRealtimeCallback()

    repository = AgentRepository(event_store=event_store, max_retries=3)
    limits_registry = HierarchyLimitsRegistry()
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )

    # Lazy import to avoid pulling AgentOrchestrator into the module top level.
    from core.application.agent_orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(
        llm_port=llm,
        worker_port=worker_tool,
        prompt_builder=PromptBuilder("prompts", "claude_code"),
        child_factory=child_factory,
        realtime_callback=realtime,
    )

    parent_notifier = ParentNotificationService(repository=repository)

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=shared_context,
        sibling_view_port=sibling_view,
        parent_notifier=parent_notifier,
        prompt_builder=PromptBuilder("prompts", "claude_code"),
        flat_worker=flat_worker,
        flat_invariant_builder=invariant_builder or _make_invariant_builder(),
    )

    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.05,
        output_directory=str(output_directory),
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        mode="flat",
    )

    service = AgentExecutionService(
        event_store=event_store,
        dependencies=dependencies,
        config=config,
        system_limits=_LimitsBridge(),
    )
    return service, event_store


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_flat_mode_skips_decomposition_and_dispatches_via_worker_port(
    tmp_path: Path,
) -> None:
    """ExecutionService in flat mode dispatches the task directly to WorkerPort
    and never asks the LLM for a decomposition or assessment.
    """
    # Given: an ExecutionService wired with a fake WorkerPort and an output dir.
    fake_worker = FakeFlatWorker(exit_status="completed", summary="ok")
    service, event_store = _wire_flat_service(
        flat_worker=fake_worker,
        output_directory=tmp_path,
    )

    # When: creating the boss agent and running the system loop.
    root_id = await service.create_boss_agent("solve cve")
    await service.run_system_loop(root_id)

    # Then: the WorkerPort was invoked exactly once with the bundle.
    assert len(fake_worker.calls) == 1
    invocation = fake_worker.calls[0]
    assert invocation["run_id"] == root_id
    assert isinstance(invocation["spec"], TaskPromptSpec)
    assert invocation["spec"].task == "solve cve"

    # And: the LLM (assessment/decomposition) was never consulted.
    boss_events = event_store.events_for(root_id)
    assert not any(isinstance(e, SubtasksDefined) for e in boss_events)
    assert not any(isinstance(e, StatusChanged) for e in boss_events)


@pytest.mark.asyncio
async def test_flat_mode_emits_expected_event_sequence(tmp_path: Path) -> None:
    """The BOSS aggregate accrues AgentCreated -> TaskAssigned -> RunStarted ->
    CodeGenerationStarted -> WorkCompleted -> PostStepRequested -> RunCompleted in flat mode."""
    # Given: a flat-mode service with a successful fake worker.
    fake_worker = FakeFlatWorker(exit_status="completed", summary="done")
    service, event_store = _wire_flat_service(
        flat_worker=fake_worker,
        output_directory=tmp_path,
    )

    # When: running a flat-mode task.
    root_id = await service.create_boss_agent("solve cve")
    await service.run_system_loop(root_id)

    # Then: the boss event stream matches the expected sequence.
    boss_events = event_store.events_for(root_id)
    event_types = [type(e).__name__ for e in boss_events]
    assert event_types == [
        AgentCreated.__name__,
        TaskAssigned.__name__,
        RunStarted.__name__,
        AgentExecutionStarted.__name__,
        OperationStarted.__name__,
        CodeGenerationStarted.__name__,
        PromptSent.__name__,
        WorkCompleted.__name__,
        PostStepRequested.__name__,
        OperationFinished.__name__,
        AgentExecutionFinished.__name__,
        PostStepCompleted.__name__,
        RunCompleted.__name__,
    ]
    # And: the WorkCompleted carries the worker's summary.
    work_completed = next(e for e in boss_events if isinstance(e, WorkCompleted))
    assert work_completed.result == "done"
    # And: the RunCompleted reflects success.
    run_completed = next(e for e in boss_events if isinstance(e, RunCompleted))
    assert run_completed.status == "completed"
    assert root_id not in await service._query_service.get_active_agent_ids(root_id=root_id)


@pytest.mark.asyncio
async def test_flat_mode_translates_worker_failure_to_workfailed(
    tmp_path: Path,
) -> None:
    """A failed WorkerResult yields a WorkFailed event and a failed RunCompleted."""
    # Given: a fake worker that reports failure.
    fake_worker = FakeFlatWorker(exit_status="failed", summary="boom")
    service, event_store = _wire_flat_service(
        flat_worker=fake_worker,
        output_directory=tmp_path,
    )

    # When: running a flat-mode task.
    root_id = await service.create_boss_agent("solve cve")
    await service.run_system_loop(root_id)

    # Then: the boss stream contains a WorkFailed (not WorkCompleted) and the
    # RunCompleted carries status="failed".
    boss_events = event_store.events_for(root_id)
    assert any(isinstance(e, WorkFailed) for e in boss_events)
    assert not any(isinstance(e, WorkCompleted) for e in boss_events)
    run_completed = next(e for e in boss_events if isinstance(e, RunCompleted))
    assert run_completed.status == "failed"
    assert root_id not in await service._query_service.get_active_agent_ids(root_id=root_id)


@pytest.mark.asyncio
async def test_flat_mode_translates_worker_timeout_to_failed_run(
    tmp_path: Path,
) -> None:
    """A WorkerResult with exit_status='timeout' surfaces as a timed_out RunCompleted."""
    # Given: a fake worker that reports timeout.
    fake_worker = FakeFlatWorker(exit_status="timeout", summary="too slow")
    service, event_store = _wire_flat_service(
        flat_worker=fake_worker,
        output_directory=tmp_path,
    )

    # When: running a flat-mode task.
    root_id = await service.create_boss_agent("solve cve")
    await service.run_system_loop(root_id)

    # Then: the RunCompleted classifies the run as timed_out.
    boss_events = event_store.events_for(root_id)
    run_completed = next(e for e in boss_events if isinstance(e, RunCompleted))
    assert run_completed.status == "timed_out"


@pytest.mark.asyncio
async def test_flat_mode_recovers_from_worker_exception(tmp_path: Path) -> None:
    """When the WorkerPort raises, the service emits WorkFailed with the
    error message and finalizes the run as failed."""

    # Given: a worker that raises RuntimeError("boom").
    class _RaisingWorker:
        async def run_task(
            self,
            *,
            run_id: UUID,
            spec: TaskPromptSpec,
            tool_policy: ToolPolicy,
            timeouts: TimeoutBudget,
            workspace: WorkspaceSpec,
        ) -> WorkerResult:
            del run_id, spec, tool_policy, timeouts, workspace
            raise RuntimeError("boom")

    service, event_store = _wire_flat_service(
        flat_worker=_RaisingWorker(),
        output_directory=tmp_path,
    )

    # When: invoking the service.
    root_id = await service.create_boss_agent("solve cve")
    await service.run_system_loop(root_id)

    # Then: BOSS finishes in FAILED state with a WorkFailed event present.
    boss_events = event_store.events_for(root_id)
    failed_events = [e for e in boss_events if isinstance(e, WorkFailed)]
    assert len(failed_events) == 1
    # And: the failure reason references the flat-mode worker error or "boom".
    reason = failed_events[0].reason
    assert "flat-mode worker error" in reason or "boom" in reason

    # And: RunCompleted.status == "failed" and no exception propagated.
    run_completed = next(e for e in boss_events if isinstance(e, RunCompleted))
    assert run_completed.status == "failed"
    # And: WorkCompleted is absent — the failure path superseded the success path.
    assert not any(isinstance(e, WorkCompleted) for e in boss_events)


@pytest.mark.asyncio
async def test_flat_mode_requires_worker_and_invariant_builder(
    tmp_path: Path,
) -> None:
    """If bootstrap forgot to wire the flat worker or the invariant builder,
    run_system_loop must fail loudly (not silently fall through)."""
    # Given: a flat-mode service WITHOUT flat_worker / flat_invariant_builder.
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    limits_registry = HierarchyLimitsRegistry()
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )
    from core.application.agent_orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(
        llm_port=FakeLLM(),
        worker_port=FakeWorkerTool(),
        prompt_builder=PromptBuilder("prompts", "claude_code"),
        child_factory=child_factory,
    )
    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=FakeSharedContextPort(),
        sibling_view_port=FakeSiblingViewPort(),
        parent_notifier=ParentNotificationService(repository=repository),
        prompt_builder=PromptBuilder("prompts", "claude_code"),
        # NOTE: flat_worker / flat_invariant_builder intentionally unset.
    )
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.05,
        output_directory=str(tmp_path),
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        mode="flat",
    )
    service = AgentExecutionService(
        event_store=event_store,
        dependencies=dependencies,
        config=config,
        system_limits=_LimitsBridge(),
    )

    # When: running the system loop.
    root_id = await service.create_boss_agent("solve cve")

    # Then: a clear configuration error surfaces.
    with pytest.raises(RuntimeError, match="WorkerPort"):
        await service.run_system_loop(root_id)
