"""Tests for ``_run_flat_mode`` cleanup consolidation (E.4 Part 1).

Verifies that ``cleanup_worker_execution`` is invoked exactly once whether the
flat-mode run completes normally or fails partway through. The single
``try/finally`` wrapping the post-prepare body is the discriminator: if the
cleanup call is duplicated inside the inner exception arm or at the tail (as it
was before this change), or if a downstream method raises and bypasses cleanup
entirely (as the old code allowed), these tests fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from bootstrap.application import ExecutionLimitsBridge
from config import BossConfig, ManagerConfig
from core.application.agent_orchestrator import AgentOrchestrator
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
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.ports.domain_plugin_port import WorkerExecutionContext


def _system_limits() -> ExecutionLimitsBridge:
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
    )


def _service_config(tmp_path: Path) -> ServiceConfig:
    boss_config = BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    manager_config = ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    return ServiceConfig(
        max_retries=3,
        poll_interval=0.5,
        output_directory=str(tmp_path),
        default_worker_tool="claude_code",
        boss_config=boss_config,
        manager_config=manager_config,
        mode="flat",
    )


def _flat_invariant_builder() -> Any:
    def _builder(
        *,
        task: str,
        domain_context: object | None,
        run_dir: Path,
    ) -> FlatModeBundle:
        del domain_context
        return FlatModeBundle(
            spec=TaskPromptSpec(rendered_prompt="prompt", prompt_sha="abc", task=task),
            tool_policy=ToolPolicy(allowed=(), disallowed=(), allowed_bash_commands=()),
            timeouts=TimeoutBudget(per_worker_call=10, per_run_total=10),
            workspace=WorkspaceSpec(root=run_dir),
        )

    return _builder


class _StubDomainPlugin:
    """Minimal DomainPlugin stub that records prepare/cleanup invocations."""

    def __init__(self, working_directory: str) -> None:
        self.working_directory = working_directory
        self.prepare_worker_execution = AsyncMock(
            return_value=WorkerExecutionContext(
                working_directory=working_directory,
                task_context={},
            )
        )
        self.cleanup_worker_execution = AsyncMock(return_value=None)

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        return None

    def get_run_metadata(self, domain_context: object) -> dict[str, Any]:
        return {}

    def get_tag_mappings(self) -> dict[str, Any]:
        return {}

    def get_provenance_patterns(self) -> list[tuple[str, Any]]:
        return []

    async def prepare_run(
        self, *, root_id: UUID, run_output_path: Path, domain_context: object | None
    ) -> None:
        return None

    def get_prompt_strategy(self) -> Any:
        return None

    def get_decomposition_validator(self) -> Any:
        return None

    def get_decomposition_policy(self) -> Any:
        return None

    def get_procedure_executor(self) -> Any:
        return None


class _CompletingWorker:
    """``WorkerPort`` stub that returns a completed ``WorkerResult``."""

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult:
        del spec, tool_policy, timeouts, workspace
        return WorkerResult(
            run_id=run_id,
            exit_status="completed",
            tokens_used=0,
            wall_time_seconds=0.0,
            output_summary="ok",
            events=(),
        )


class _RaisingWorker:
    """``WorkerPort`` stub that raises from ``run_task``."""

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
        raise RuntimeError("worker exploded")


def _build_service(
    *,
    tmp_path: Path,
    domain_plugin: _StubDomainPlugin,
    flat_worker: Any,
    root_id: UUID,
) -> AgentExecutionService:
    """Wire an ``AgentExecutionService`` configured for flat-mode dispatch.

    The repository's ``load`` / ``persist_events`` are mocked at the
    ``AgentRepository`` boundary so the test does not depend on the event-store
    replay path; the focus here is cleanup invariants, not aggregate state.
    """
    mock_event_store = AsyncMock()
    mock_llm_port = AsyncMock()

    limits_registry = HierarchyLimitsRegistry()
    limits_registry.create_root(
        root_id,
        max_depth=-1,
        max_children_per_node=-1,
        max_retries=3,
    )

    prompt_builder = PromptBuilder("prompts", "claude_code")
    repository = AgentRepository(event_store=mock_event_store, max_retries=3)

    boss_session = AgentSession.create(
        agent_id=root_id,
        role=AgentRole.BOSS,
        config={
            "strategy": "heuristic",
            "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        },
        parent_id=None,
    )
    boss_session.assign_task("flat task")
    boss_session.mark_changes_as_committed()

    repository.load = AsyncMock(return_value=boss_session)  # type: ignore[method-assign]
    repository.persist_events = AsyncMock(return_value=[])  # type: ignore[method-assign]
    # Required by ``AgentQueryService.get_statistics`` inside ``_emit_run_completed``.
    repository.get_hierarchy_events_grouped = AsyncMock(return_value={})  # type: ignore[method-assign]

    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )

    orchestrator = AgentOrchestrator(
        llm_port=mock_llm_port,
        worker_port=MagicMock(),
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=AsyncMock(),
        sibling_view_port=AsyncMock(),
        parent_notifier=ParentNotificationService(repository=repository),
        prompt_builder=prompt_builder,
        domain_plugin=domain_plugin,
        flat_worker=flat_worker,
        flat_invariant_builder=_flat_invariant_builder(),
    )
    service = AgentExecutionService(
        event_store=mock_event_store,
        dependencies=dependencies,
        config=_service_config(tmp_path),
        system_limits=_system_limits(),
    )

    # Pre-populate the flat-mode bookkeeping that ``create_boss_agent`` would
    # normally seed for a real run.
    service._current_task = "flat task"
    service._current_domain_context = None
    service._working_directory = tmp_path

    return service


@pytest.mark.asyncio
async def test_flat_mode_calls_cleanup_on_exception(tmp_path: Path) -> None:
    # Given: a service whose downstream ``_emit_run_completed`` raises after
    # the worker returns. This wedge is the discriminator — the OLD code
    # called cleanup only inside the worker's ``except`` arm or at the tail,
    # so a raise from ``_emit_run_completed`` would skip cleanup entirely.
    root_id = uuid4()
    domain_plugin = _StubDomainPlugin(str(tmp_path))
    service = _build_service(
        tmp_path=tmp_path,
        domain_plugin=domain_plugin,
        flat_worker=_CompletingWorker(),
        root_id=root_id,
    )
    service._emit_run_completed = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("emit_run_completed boom")
    )

    # When: the run dispatches and the failure propagates.
    with pytest.raises(RuntimeError, match="emit_run_completed boom"):
        await service._run_flat_mode(root_id)

    # Then: cleanup ran exactly once via the ``finally`` block.
    domain_plugin.cleanup_worker_execution.assert_awaited_once_with(
        root_id=root_id,
        agent_id=root_id,
        domain_context=None,
    )


@pytest.mark.asyncio
async def test_flat_mode_calls_cleanup_on_worker_exception(tmp_path: Path) -> None:
    # Given: a flat worker whose ``run_task`` raises. This exercises the
    # inner ``except Exception`` arm; cleanup should still happen exactly
    # once via ``finally`` (not twice as it would if the old call inside the
    # arm had been left in place).
    root_id = uuid4()
    domain_plugin = _StubDomainPlugin(str(tmp_path))
    service = _build_service(
        tmp_path=tmp_path,
        domain_plugin=domain_plugin,
        flat_worker=_RaisingWorker(),
        root_id=root_id,
    )

    # When: the worker error is handled internally and the run completes.
    await service._run_flat_mode(root_id)

    # Then: cleanup ran exactly once.
    domain_plugin.cleanup_worker_execution.assert_awaited_once_with(
        root_id=root_id,
        agent_id=root_id,
        domain_context=None,
    )


@pytest.mark.asyncio
async def test_flat_mode_calls_cleanup_on_success(tmp_path: Path) -> None:
    # Given: a normal completion path through the worker and finalization.
    root_id = uuid4()
    domain_plugin = _StubDomainPlugin(str(tmp_path))
    service = _build_service(
        tmp_path=tmp_path,
        domain_plugin=domain_plugin,
        flat_worker=_CompletingWorker(),
        root_id=root_id,
    )

    # When: the flat-mode run completes.
    await service._run_flat_mode(root_id)

    # Then: cleanup ran exactly once on the success path.
    domain_plugin.cleanup_worker_execution.assert_awaited_once_with(
        root_id=root_id,
        agent_id=root_id,
        domain_context=None,
    )
