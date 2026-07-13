"""Flat-mode execution path: dispatch a task directly to a ``WorkerPort``.

In ``flat`` mode the BOSS itself is the worker: no decomposition, no children,
and no polling loop. ``FlatModeRunner`` runs the task through
``WorkerPort.run_task`` and finalizes the run, mirroring the hierarchical
worker's telemetry envelope so the two modes stay comparable in metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.domain.aggregates.agent_session import AgentRole
from core.domain.events.events import WorkCompleted, WorkFailed


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkerResult,
        WorkspaceSpec,
    )
    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.application.types import ProgressCallback
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.events.events import DomainEvent
    from core.ports.domain_plugin_port import DomainPlugin
    from core.ports.worker_port import WorkerPort


@dataclass(frozen=True)
class FlatModeBundle:
    """Invariants assembled by bootstrap for a flat-mode run.

    Bundles the four ``run_invariants`` value objects so the dispatcher can
    pass them to ``WorkerPort.run_task`` without re-deriving them.
    """

    spec: TaskPromptSpec
    tool_policy: ToolPolicy
    timeouts: TimeoutBudget
    workspace: WorkspaceSpec


class FlatInvariantBuilder(Protocol):
    """Callable that produces a ``FlatModeBundle`` for a single flat-mode run.

    Bootstrap registers a closure that captures ``Settings`` and the active
    domain plugin. The dispatcher invokes it once per run with the run-scoped
    inputs (``task``, ``domain_context``, ``run_dir``).
    """

    def __call__(
        self,
        *,
        task: str,
        domain_context: object | None,
        run_dir: Path,
    ) -> FlatModeBundle: ...


class FlatModeRunner:
    """Run a task directly through a ``WorkerPort`` and finalize the run."""

    def __init__(
        self,
        *,
        repository: AgentRepository,
        flat_worker: WorkerPort | None,
        flat_invariant_builder: FlatInvariantBuilder | None,
        domain_plugin: DomainPlugin | None,
        default_worker_tool: str,
        progress_callback: ProgressCallback | None,
    ) -> None:
        self._repository = repository
        self._flat_worker = flat_worker
        self._flat_invariant_builder = flat_invariant_builder
        self._domain_plugin = domain_plugin
        self._default_worker_tool = default_worker_tool
        self._progress_callback = progress_callback

    async def run(
        self,
        *,
        root_agent_id: UUID,
        task: str | None,
        domain_context: object | None,
        working_directory: Path | None,
        emit_run_completed: Callable[..., Awaitable[None]],
    ) -> None:
        """Dispatch the task directly to ``WorkerPort.run_task`` and finalize.

        Skips BOSS/MANAGER decomposition entirely. The BOSS aggregate created
        by ``create_boss_agent`` is treated as the worker: it transitions
        ANALYZING -> IN_PROGRESS -> COMPLETED via synthetic worker events
        carrying the ``WorkerResult`` outcome.
        """
        if self._flat_worker is None or self._flat_invariant_builder is None:
            raise RuntimeError(
                "orchestration.mode='flat' requires a WorkerPort and a "
                "FlatInvariantBuilder to be wired by bootstrap"
            )
        if task is None:
            raise RuntimeError(
                "_run_flat_mode invoked before create_boss_agent populated the run state"
            )
        if working_directory is None:
            raise RuntimeError(
                "_run_flat_mode requires output_directory to be configured so a "
                "run-scoped workspace can be materialized"
            )

        start_time = time.monotonic()
        worker_context = None
        if self._domain_plugin is not None:
            worker_context = await self._domain_plugin.prepare_worker_execution(
                root_id=root_agent_id,
                agent_id=root_agent_id,
                run_output_path=working_directory,
                domain_context=domain_context,
            )

        try:
            bundle = self._flat_invariant_builder(
                task=task,
                domain_context=domain_context,
                run_dir=(
                    Path(worker_context.working_directory)
                    if worker_context is not None and worker_context.working_directory
                    else working_directory
                ),
            )
            if worker_context is not None and worker_context.task_context:
                bundle = FlatModeBundle(
                    spec=bundle.spec,
                    tool_policy=bundle.tool_policy,
                    timeouts=bundle.timeouts,
                    workspace=bundle.workspace.model_copy(
                        update={
                            "extras": {
                                **dict(bundle.workspace.extras),
                                **dict(worker_context.task_context),
                            }
                        }
                    ),
                )

            boss = await self._repository.load(root_agent_id)
            tool_name = self._default_worker_tool

            # Mirror the hierarchical worker's telemetry envelope and ordering so
            # flat-mode runs are comparable to hierarchical worker runs in
            # metrics scripts.
            execution_started_at = time.monotonic()
            operation_started_at = execution_started_at
            boss.emit_execution_started(role=AgentRole.WORKER.value, depth=0)
            boss.emit_operation_started(operation_type="worker_execution")
            boss.start_worker_execution(tool_name)
            boss.emit_prompt_sent(
                prompt=bundle.spec.rendered_prompt,
                prompt_type="worker_execution",
                target=tool_name,
            )
            await self._repository.persist_events(boss, self._progress_callback)

            try:
                result = await self._flat_worker.run_task(
                    run_id=root_agent_id,
                    spec=bundle.spec,
                    tool_policy=bundle.tool_policy,
                    timeouts=bundle.timeouts,
                    workspace=bundle.workspace,
                )
            except Exception as error:
                logger.exception("flat-mode worker raised; failing run")
                reloaded = await self._repository.load(root_agent_id)
                reloaded.fail_with_reason(f"flat-mode worker error: {error!r}")
                now = time.monotonic()
                reloaded.emit_operation_finished(
                    operation_type="worker_execution",
                    duration_seconds=now - operation_started_at,
                )
                reloaded.emit_execution_finished(
                    role=AgentRole.WORKER.value,
                    status=reloaded.status.value,
                    duration_seconds=now - execution_started_at,
                )
                self._complete_pending_post_steps(reloaded)
                await self._repository.persist_events(reloaded, self._progress_callback)
                await emit_run_completed(
                    root_agent_id,
                    start_time,
                    status_override="failed",
                )
                return

            reloaded = await self._repository.load(root_agent_id)
            for event in result.events:
                reloaded.apply_worker_event(event)
            terminal_event = self._build_terminal_event(reloaded.agent_id, result)
            reloaded.apply_worker_event(terminal_event)
            now = time.monotonic()
            reloaded.emit_operation_finished(
                operation_type="worker_execution",
                duration_seconds=now - operation_started_at,
            )
            reloaded.emit_execution_finished(
                role=AgentRole.WORKER.value,
                status=reloaded.status.value,
                duration_seconds=now - execution_started_at,
            )
            self._complete_pending_post_steps(reloaded)
            await self._repository.persist_events(reloaded, self._progress_callback)

            await emit_run_completed(
                root_agent_id,
                start_time,
                status_override=self._classify_flat_run(result.exit_status),
            )
        finally:
            if self._domain_plugin is not None:
                await self._domain_plugin.cleanup_worker_execution(
                    root_id=root_agent_id,
                    agent_id=root_agent_id,
                    domain_context=domain_context,
                )

    @staticmethod
    def _complete_pending_post_steps(agent: AgentSession) -> None:
        while terminal_event_id := agent.pending_post_step_terminal_event_id:
            agent.complete_post_step(terminal_event_id)

    @staticmethod
    def _build_terminal_event(
        agent_id: UUID,
        result: WorkerResult,
    ) -> DomainEvent:
        """Translate a ``WorkerResult`` into a ``WorkCompleted``/``WorkFailed`` event.

        Sequence number is 0 — ``apply_worker_event`` rewrites it with the
        aggregate's actual next sequence at apply time.
        """
        if result.exit_status == "completed":
            payload = result.output_summary or "Flat-mode worker completed."
            return WorkCompleted(
                aggregate_id=agent_id,
                sequence_number=0,
                result=payload,
            )

        reason = result.output_summary or f"Flat-mode worker exit_status={result.exit_status}"
        return WorkFailed(
            aggregate_id=agent_id,
            sequence_number=0,
            reason=reason,
        )

    @staticmethod
    def _classify_flat_run(exit_status: str) -> str:
        """Translate ``WorkerResult.exit_status`` into the ``RunCompleted.status`` value."""
        if exit_status == "completed":
            return "completed"
        if exit_status == "timeout":
            return "timed_out"
        return "failed"
