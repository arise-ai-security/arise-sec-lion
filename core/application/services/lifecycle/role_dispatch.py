"""Role-specific execution handlers for agent step dispatch."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.application.agent_orchestrator import AgentOrchestrator
    from core.ports.domain_plugin_port import DomainPlugin, WorkerExecutionContext
    from core.ports.runtime_ports import SiblingViewPort


@dataclass(frozen=True)
class DispatchContext:
    """Dependencies and runtime hooks needed by role handlers."""

    orchestrator: "AgentOrchestrator"
    sibling_view_port: "SiblingViewPort"
    worker_semaphore: asyncio.Semaphore
    worker_semaphore_holders: set[UUID]
    domain_plugin: "DomainPlugin | None"
    get_agent_depth: Callable[[AgentSession], int]
    get_root_id: Callable[[UUID], UUID]
    get_run_output_path: Callable[[], Path | None]
    get_working_directory: Callable[[], str | None]
    get_workspace_context: Callable[[], str | None]
    worker_prepare_timeout_seconds: float = 120.0
    worker_execute_timeout_seconds: float = 600.0


class RoleHandler(ABC):
    """Handle execution for one agent role."""

    @abstractmethod
    async def handle(self, agent: AgentSession) -> None:
        """Execute one step for the provided agent."""


class LifecycleRoleHandler(RoleHandler, ABC):
    """Base handler for roles that emit execution lifecycle events."""

    def __init__(self, context: DispatchContext) -> None:
        self._context = context

    async def handle(self, agent: AgentSession) -> None:
        await self._execute_with_lifecycle(agent, lambda: self._execute(agent))

    async def _execute_with_lifecycle(
        self,
        agent: AgentSession,
        work: Callable[[], Awaitable[None]],
    ) -> None:
        depth = self._context.get_agent_depth(agent)
        start_time = time.monotonic()
        agent.emit_execution_started(role=agent.role.value, depth=depth)

        await work()

        duration = time.monotonic() - start_time
        agent.emit_execution_finished(
            role=agent.role.value,
            status=agent.status.value,
            duration_seconds=duration,
        )

    @abstractmethod
    async def _execute(self, agent: AgentSession) -> None:
        """Execute role-specific work inside lifecycle instrumentation."""


class PendingHandler(RoleHandler):
    """Handle task assessment for pending agents."""

    def __init__(self, context: DispatchContext) -> None:
        self._context = context

    async def handle(self, agent: AgentSession) -> None:
        await self._context.orchestrator.assess_task(agent)


class EvaluatorHandler(LifecycleRoleHandler):
    """Handle task decomposition for boss and manager agents."""

    async def _execute(self, agent: AgentSession) -> None:
        await self._context.orchestrator.evaluate_task(agent)


class WorkerHandler(LifecycleRoleHandler):
    """Handle worker execution, including worker-scoped runtime preparation."""

    async def _execute(self, agent: AgentSession) -> None:
        async with self._context.worker_semaphore:
            self._context.worker_semaphore_holders.add(agent.agent_id)
            try:
                root_id = self._context.get_root_id(agent.agent_id)
                handoff = await self._context.sibling_view_port.build_view(
                    agent_id=agent.agent_id,
                    parent_id=agent.parent_id,
                    root_id=root_id,
                )

                domain_context = (
                    agent.hierarchy_limits.domain_context
                    if agent.hierarchy_limits is not None
                    else None
                )
                try:
                    worker_context = await asyncio.wait_for(
                        self._prepare_worker_context(
                            root_id=root_id,
                            agent=agent,
                            domain_context=domain_context,
                        ),
                        timeout=self._context.worker_prepare_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "Worker context preparation timed out after "
                        f"{self._context.worker_prepare_timeout_seconds:.0f}s"
                    ) from exc

                try:
                    await asyncio.wait_for(
                        self._context.orchestrator.execute_task(
                            agent,
                            working_directory=self._resolve_working_directory(worker_context),
                            workspace_context=self._context.get_workspace_context(),
                            handoff=handoff,
                            task_context_overrides=(
                                worker_context.task_context if worker_context else None
                            ),
                        ),
                        timeout=self._context.worker_execute_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "Worker execution timed out after "
                        f"{self._context.worker_execute_timeout_seconds:.0f}s"
                    ) from exc
                finally:
                    # Skip cleanup if the worker completed and may get a
                    # verification retry — keeps the container alive so the
                    # retry reuses it (with all build artifacts, deliverables,
                    # and filesystem state from the prior attempt).
                    # The plugin's prepare_worker_execution checks
                    # is_session_alive and reuses if alive.
                    may_retry = (
                        agent.status == AgentStatus.COMPLETED
                        or (agent.status == AgentStatus.FAILED and agent.verification_feedback)
                    )
                    if not may_retry:
                        asyncio.create_task(
                            self._safe_cleanup_worker_context(
                                root_id=root_id,
                                agent=agent,
                                domain_context=domain_context,
                            )
                        )
            finally:
                self._context.worker_semaphore_holders.discard(agent.agent_id)

    async def _prepare_worker_context(
        self,
        *,
        root_id: UUID,
        agent: AgentSession,
        domain_context: object | None,
    ) -> "WorkerExecutionContext | None":
        if self._context.domain_plugin is None:
            return None

        run_output_path = self._context.get_run_output_path()
        if run_output_path is None:
            return None

        return await self._context.domain_plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=agent.agent_id,
            run_output_path=run_output_path,
            domain_context=domain_context,
        )

    async def _cleanup_worker_context(
        self,
        *,
        root_id: UUID,
        agent: AgentSession,
        domain_context: object | None,
    ) -> None:
        if self._context.domain_plugin is None:
            return

        await self._context.domain_plugin.cleanup_worker_execution(
            root_id=root_id,
            agent_id=agent.agent_id,
            domain_context=domain_context,
        )

    _CLEANUP_TIMEOUT: float = 30  # seconds

    async def _safe_cleanup_worker_context(
        self,
        *,
        root_id: UUID,
        agent: AgentSession,
        domain_context: object | None,
    ) -> None:
        """Best-effort cleanup with timeout. Runs as a detached task."""
        try:
            await asyncio.wait_for(
                self._cleanup_worker_context(
                    root_id=root_id,
                    agent=agent,
                    domain_context=domain_context,
                ),
                timeout=self._CLEANUP_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Worker cleanup timed out after %.0fs for agent %s",
                self._CLEANUP_TIMEOUT,
                agent.agent_id,
            )
        except Exception:
            logger.exception(
                "Worker cleanup failed for agent %s", agent.agent_id,
            )

    def _resolve_working_directory(
        self,
        worker_context: "WorkerExecutionContext | None",
    ) -> str | None:
        if worker_context and worker_context.working_directory:
            return worker_context.working_directory
        return self._context.get_working_directory()


def build_role_handlers(context: DispatchContext) -> dict[AgentRole, RoleHandler]:
    """Build the role-dispatch registry for execution."""
    evaluator = EvaluatorHandler(context)
    return {
        AgentRole.PENDING: PendingHandler(context),
        AgentRole.BOSS: evaluator,
        AgentRole.MANAGER: evaluator,
        AgentRole.WORKER: WorkerHandler(context),
    }
