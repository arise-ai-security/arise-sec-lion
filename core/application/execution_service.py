"""Agent Execution Service - orchestrates agent lifecycle and execution.

This is the main orchestration service that coordinates:
- Agent step execution (load -> dispatch -> persist)
- Child agent spawning
- Parent notification on completion
- System loop for continuous execution
"""

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.application.run_invariants import WorkerResult
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    DispatchContext,
    HierarchyLimitsRegistry,
    ParentNotificationService,
    RetryPolicy,
    build_role_handlers,
)
from core.application.types import ProgressCallback
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    ChildSpawned,
    DomainEvent,
    SealedArtifact,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import ConcurrencyError
from core.domain.services.context_update_parser import parse_context_update
from core.domain.values.json_types import JsonObject


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from config import BossConfig, ManagerConfig
    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkspaceSpec,
    )
    from core.application.services import PromptBuilder
    from core.ports.domain_plugin_port import DomainPlugin, PreparedRunWorkspace
    from core.ports.event_store_port import EventStorePort
    from core.ports.runtime_ports import (
        SharedContextPort,
        SiblingViewPort,
        SystemLimitsPort,
        ReconToolPort,
    )
    from core.ports.worker_port import WorkerPort


@dataclass(frozen=True)
class FlatModeBundle:
    """Invariants assembled by bootstrap for a flat-mode run.

    Bundles the four ``run_invariants`` value objects so the dispatcher can
    pass them to ``WorkerPort.run_task`` without re-deriving them.
    """

    spec: "TaskPromptSpec"
    tool_policy: "ToolPolicy"
    timeouts: "TimeoutBudget"
    workspace: "WorkspaceSpec"


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


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for AgentExecutionService."""

    max_retries: int
    poll_interval: float
    output_directory: str
    default_worker_tool: str
    boss_config: "BossConfig"
    manager_config: "ManagerConfig"
    mode: Literal["hierarchical", "flat"] = "hierarchical"
    # Whitelist of top-level workspace dirs for the <workspace> prompt listing;
    # None = legacy behavior (everything except _WORKSPACE_SKIP_DIRS).
    workspace_listing_dirs: tuple[str, ...] | None = None
    # Cap on listing entries (overflow summarized); None = unlimited.
    workspace_listing_max_entries: int | None = None
    # Verification-failure retries per worker; each retry re-runs a full
    # worker conversation, so this directly multiplies failing-worker cost.
    verification_max_retries: int = 2


@dataclass(frozen=True)
class ExecutionServiceDependencies:
    """Collaborators for AgentExecutionService (Parameter Object pattern)."""

    repository: AgentRepository
    orchestrator: AgentOrchestrator
    limits_registry: HierarchyLimitsRegistry
    child_factory: ChildAgentFactory
    query_service: AgentQueryService
    shared_context_port: "SharedContextPort"
    sibling_view_port: "SiblingViewPort"
    parent_notifier: ParentNotificationService
    prompt_builder: "PromptBuilder"
    domain_plugin: "DomainPlugin | None" = None
    recon_tool: "ReconToolPort | None" = None
    flat_worker: "WorkerPort | None" = None
    flat_invariant_builder: FlatInvariantBuilder | None = None


# =============================================================================
# Execution Service
# =============================================================================


class AgentExecutionService:
    """Orchestrates agent workflow using composed services."""

    def __init__(
            self,
            event_store: "EventStorePort",
            dependencies: ExecutionServiceDependencies,
            config: ServiceConfig,
            system_limits: "SystemLimitsPort",
            progress_callback: ProgressCallback | None = None,
    ) -> None:
        # Core ports
        self._event_store = event_store
        self._shared_context_port = dependencies.shared_context_port
        self._sibling_view_port = dependencies.sibling_view_port

        # Configuration
        self._config = config
        self._system_limits = system_limits

        # Injected collaborators
        self._repository = dependencies.repository
        self._orchestrator = dependencies.orchestrator
        self._limits_registry = dependencies.limits_registry
        self._child_factory = dependencies.child_factory
        self._query_service = dependencies.query_service
        self._parent_notifier = dependencies.parent_notifier
        self._prompt_builder = dependencies.prompt_builder
        self._domain_plugin = dependencies.domain_plugin
        self._recon_tool = dependencies.recon_tool
        self._flat_worker = dependencies.flat_worker
        self._flat_invariant_builder = dependencies.flat_invariant_builder

        # Workspace state (inlined from WorkspaceContextProvider)
        self._working_directory: Path | None = None
        # Flat-mode bookkeeping: captured at boss-creation so run_system_loop
        # can dispatch directly to ``WorkerPort`` without re-resolving them.
        self._current_task: str | None = None
        self._current_domain_context: object | None = None

        # Worker concurrency control
        semaphore_limit = (
            system_limits.max_concurrent_workers
            if system_limits.is_workers_limited()
            else 10000
        )
        self._worker_semaphore = asyncio.Semaphore(semaphore_limit)

        # LLM concurrency control
        llm_limit = (
            system_limits.max_concurrent_llm_calls
            if system_limits.is_llm_limited()
            else 10000
        )
        self._llm_semaphore = asyncio.Semaphore(llm_limit)

        # Manager-cache prime (env-gated): before the depth-1 phase managers run their
        # assessments in parallel, issue one cheap request warming their longest common
        # (prompt-prefix, tool defs) so each manager's real call reads it from OpenAI's
        # cache. Off by default => no behavior change and no extra LLM call.
        self._prime_manager_cache = (
            os.environ.get("ARISE_PRIME_MANAGER_CACHE", "").lower()
            not in ("", "0", "false", "no")
        )
        self._llm_jitter_max_ms = system_limits.llm_jitter_max_ms
        self._max_agent_step_seconds = float(system_limits.max_agent_step_seconds)

        # Progress callback
        self._progress_callback = progress_callback

        # Retry config (from OrchestrationConfig.retry)
        self._retry_config = getattr(system_limits, "retry", None)
        self._retry_policy = RetryPolicy(
            repository=self._repository,
            retry_config=self._retry_config,
            progress_callback=self._progress_callback,
        )
        self._role_handlers = build_role_handlers(
            DispatchContext(
                orchestrator=self._orchestrator,
                sibling_view_port=self._sibling_view_port,
                worker_semaphore=self._worker_semaphore,
                domain_plugin=self._domain_plugin,
                get_agent_depth=self._get_agent_depth,
                get_root_id=self._limits_registry.get_root_id,
                get_run_output_path=lambda: self._working_directory,
                get_working_directory=self._get_working_directory_str,
                get_workspace_context=self._get_workspace_context,
                persist_checkpoint=self._persist_agent_events,
            )
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def initialize(self) -> None:
        await self._event_store.connect()
        await self._event_store.initialize_schema()

    async def cleanup(self) -> None:
        """Cleanup infrastructure connections."""
        await self._event_store.disconnect()

    async def create_boss_agent(
            self,
            task_description: str,
            domain_context: object | None = None,
    ) -> UUID:
        root_id = uuid4()
        self._reset_for_new_run(root_id, domain_context=domain_context)
        self._current_task = task_description
        self._current_domain_context = domain_context

        self._prompt_builder.set_run_context(
            user_prompt=task_description,
            domain_context=domain_context,
        )
        prepared = await self._setup_working_directory(root_id, domain_context)
        await self._create_shared_context(root_id)

        boss_agent = self._create_boss_session(root_id, task_description)
        domain_metadata = self._get_run_metadata(domain_context)
        boss_agent.emit_run_started(
            task_description=task_description,
            domain_metadata=domain_metadata,
        )
        if prepared is not None and prepared.sealed_surface is not None:
            boss_agent.emit_runtime_surface_sealed(
                surface=prepared.sealed_surface.surface,
                sealed_artifacts=[
                    SealedArtifact(
                        container_path=a.container_path,
                        kind=a.kind,
                        non_golden=a.non_golden,
                        content_sha256=a.content_sha256,
                    )
                    for a in prepared.sealed_surface.artifacts
                ],
            )
        await self._repository.save_new_agent(boss_agent)

        return root_id

    async def _create_shared_context(self, root_id: UUID) -> None:
        shared_context = await self._shared_context_port.get_or_create(
            root_id=root_id, config={}
        )
        await self._shared_context_port.save(shared_context, expected_version=0)

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one workflow step: load -> dispatch -> persist with OCC retry."""
        retry_count = 0
        # Track the in-memory aggregate so any failure path can release
        # the pending child-spawn reservation before reload or bubbling
        # the error up. ``_spawn_children`` stashes the count on
        # ``agent.pending_reservation``; if a path never reached spawning
        # the count stays 0 and release is a no-op.
        last_agent = None

        while retry_count < self._config.max_retries:
            try:
                agent = await self._load_agent_with_context(agent_id)
                last_agent = agent

                await self._dispatch_agent_action(agent)
                uncommitted = await self._persist_agent_events(agent)
                await self._handle_post_step(agent, uncommitted)
                return

            except ConcurrencyError as e:
                # OCC retry: release the failed attempt's reservation so the
                # retry sees the real available cap.
                await self._release_pending_reservation(last_agent)

                retry_count += 1
                if retry_count >= self._config.max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        actual_version=e.actual_version,
                    ) from e

            except asyncio.CancelledError:
                # Cancellation aborts the step without writing a failure
                # event (the run is shutting down); still release any
                # reservation so the cap isn't permanently shrunk.
                await self._release_pending_reservation(last_agent)
                raise

            except Exception as e:
                # Any non-OCC failure (EventStoreError, RuntimeError, ...)
                # must release the reservation before delegating to
                # ``_handle_step_failure``. Otherwise the failed spawn
                # leaks slots and the next caller's ``try_reserve`` fails
                # spuriously with ``reservation_failed:total_agents``.
                await self._release_pending_reservation(last_agent)
                await self._handle_step_failure(agent_id, e)
                raise

    async def _release_pending_reservation(
        self, agent: AgentSession | None
    ) -> None:
        """Release the in-memory agent's pending child-spawn reservation, if any.

        ``_spawn_children`` stashes the reserved count on
        ``agent.pending_reservation`` after a successful
        ``ChildAgentFactory.try_reserve``. On every failure path
        (ConcurrencyError, EventStoreError, cancellation) we must
        release it; otherwise the slots stay parked in ``_reserved`` and
        future ``try_reserve`` calls see a shrunken cap.
        """
        if agent is None:
            return
        pending = agent.pending_reservation
        if not pending:
            return
        await self._child_factory.release_reservation(pending)
        agent.pending_reservation = 0

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents until all reach terminal state.

        In ``flat`` mode the BOSS itself is the worker: there is no decomposition,
        no children, and no polling loop — we dispatch the task directly to
        ``WorkerPort.run_task`` and finalize the run. In ``hierarchical`` mode
        the original behaviour applies.
        """
        if self._config.mode == "flat":
            await self._run_flat_mode(root_agent_id)
            return

        start_time = time.monotonic()
        in_progress: set[UUID] = set()
        tasks: dict[UUID, asyncio.Task] = {}
        task_started_at: dict[UUID, float] = {}
        # Per-agent staleness tracker: agents that get repeatedly rescheduled
        # (each task completes fast, but the agent never emits a new event) need
        # a different signal than per-task wall-clock age. Track event counts
        # over polls; reset the timer whenever the count grows.
        agent_event_count: dict[UUID, int] = {}
        agent_last_progress_at: dict[UUID, float] = {}
        run_status_override: str | None = None
        manager_cache_primed = False
        debug_poll = bool(os.environ.get("ARISE_DEBUG_POLL"))
        debug_last_log = 0.0
        debug_interval = float(os.environ.get("ARISE_DEBUG_POLL_INTERVAL_S", "10"))

        while True:
            self._reap_completed_tasks(tasks, in_progress, task_started_at)

            if self._is_run_timed_out(start_time):
                run_status_override = "timed_out"
                await self._handle_run_timeout(
                    root_agent_id=root_agent_id,
                    tasks=tasks,
                    in_progress=in_progress,
                )
                break

            await self._kill_overdue_tasks(tasks, in_progress, task_started_at)
            await self._kill_stale_agents(
                in_progress, agent_event_count, agent_last_progress_at,
            )

            active_agents = await self._query_service.get_active_agent_ids(
                root_id=root_agent_id, sequential_workers=True
            )
            new_agents = [aid for aid in active_agents if aid not in in_progress]

            if self._prime_manager_cache and not manager_cache_primed and new_agents:
                manager_cache_primed = await self._maybe_prime_manager_cache(new_agents)

            for agent_id in new_agents:
                in_progress.add(agent_id)
                # ``task_started_at`` is populated inside ``_run_agent_step_safe``
                # once the LLM semaphore is acquired, so the watchdog budget
                # measures work time and not time spent queued behind the slot.
                tasks[agent_id] = asyncio.create_task(
                    self._run_agent_step_safe(agent_id, task_started_at)
                )
                agent_last_progress_at.setdefault(agent_id, time.monotonic())

            now = time.monotonic()
            if debug_poll and (now - debug_last_log) >= debug_interval:
                debug_last_log = now
                active_short = [str(a)[:8] for a in active_agents]
                tasks_summary = {
                    str(aid)[:8]: f"age={now - task_started_at.get(aid, now):.0f}s "
                                  f"done={tasks[aid].done()}"
                    for aid in tasks
                }
                logger.warning(
                    "[POLL root=%s elapsed=%.0fs] active=%s in_progress=%d tasks=%s",
                    str(root_agent_id)[:8],
                    now - start_time,
                    active_short,
                    len(in_progress),
                    tasks_summary,
                )

            if not active_agents and not in_progress:
                break

            await asyncio.sleep(self._config.poll_interval)

        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

        await self._emit_run_completed(
            root_agent_id,
            start_time,
            status_override=run_status_override,
        )

    async def _run_flat_mode(self, root_agent_id: UUID) -> None:
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
        if self._current_task is None:
            raise RuntimeError(
                "_run_flat_mode invoked before create_boss_agent populated the run state"
            )
        if self._working_directory is None:
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
                run_output_path=self._working_directory,
                domain_context=self._current_domain_context,
            )

        try:
            bundle = self._flat_invariant_builder(
                task=self._current_task,
                domain_context=self._current_domain_context,
                run_dir=(
                    Path(worker_context.working_directory)
                    if worker_context is not None and worker_context.working_directory
                    else self._working_directory
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
            tool_name = self._config.default_worker_tool

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
                await self._repository.persist_events(
                    reloaded, self._progress_callback
                )
                await self._emit_run_completed(
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
            await self._repository.persist_events(reloaded, self._progress_callback)

            await self._emit_run_completed(
                root_agent_id,
                start_time,
                status_override=self._classify_flat_run(result.exit_status),
            )
        finally:
            if self._domain_plugin is not None:
                await self._domain_plugin.cleanup_worker_execution(
                    root_id=root_agent_id,
                    agent_id=root_agent_id,
                    domain_context=self._current_domain_context,
                )

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

    async def _maybe_prime_manager_cache(self, new_agents: list[UUID]) -> bool:
        """Warm OpenAI's prompt cache for a newly-active batch of depth-1 managers.

        Depth/root come from the in-process ``_limits_registry`` (same source the step
        path uses). Returns True once a prime has been attempted (caller stops checking);
        False while fewer than two managers are present, so the caller re-checks on later
        polls. Best-effort: any failure is swallowed so a prime problem never breaks a run.
        """
        manager_ids = [
            aid
            for aid in new_agents
            if (limits := self._limits_registry.get(aid)) is not None
            and limits.current_depth == 1
        ]
        if len(manager_ids) < 2:
            return False
        try:
            managers = [await self._load_agent_with_context(aid) for aid in manager_ids]
            primed_chars = await self._orchestrator.prime_manager_cache(managers)
            if primed_chars:
                logger.info(
                    "manager-cache primed: %d managers, %d-char shared prefix",
                    len(managers),
                    primed_chars,
                )
            else:
                logger.info("manager-cache prime skipped (shared prefix below floor)")
        except Exception:
            logger.warning("manager-cache prime failed; managers run cold", exc_info=True)
        return True

    async def _run_agent_step_safe(
        self,
        agent_id: UUID,
        task_started_at: dict[UUID, float] | None = None,
    ) -> None:
        """Execute agent step with jitter, rate limiting, and a hard per-step timeout.

        The hard timeout prevents an orchestrator step from blocking forever in
        LiteLLM retry storms. On timeout, the agent is marked failed so the poll
        loop progresses.
        """
        try:
            if self._llm_jitter_max_ms > 0:
                jitter_ms = random.randint(0, self._llm_jitter_max_ms)  # noqa: S311
                await asyncio.sleep(jitter_ms / 1000.0)

            async with self._llm_semaphore:
                # Watchdog clock starts here so queued time does not count
                # against the per-step budget.
                if task_started_at is not None:
                    task_started_at[agent_id] = time.monotonic()
                await asyncio.wait_for(
                    self.run_agent_step(agent_id),
                    timeout=self._max_agent_step_seconds,
                )
        except asyncio.CancelledError:
            logger.warning("Agent %s step cancelled", agent_id)
            raise
        except asyncio.TimeoutError:
            logger.error(
                "Agent %s step exceeded %.0fs timeout - marking failed",
                agent_id,
                self._max_agent_step_seconds,
            )
            try:
                agent = await self._repository.load_if_exists(agent_id)
                if agent is not None and not agent.is_terminal():
                    await self._fail_agent(
                        agent,
                        f"Agent step timed out after {self._max_agent_step_seconds:.0f}s",
                    )
            except Exception:
                logger.exception("Failed to mark timed-out agent %s as failed", agent_id)
        except Exception:
            logger.exception("Agent %s step failed unexpectedly", agent_id)

    async def _emit_run_completed(
            self,
            root_agent_id: UUID,
            start_time: float,
            status_override: str | None = None,
    ) -> None:
        duration_seconds = time.monotonic() - start_time
        stats = await self._query_service.get_statistics(root_id=root_agent_id)

        boss_agent = await self._repository.load(root_agent_id)
        run_status = status_override or (
            "completed" if boss_agent.status.value == "completed" else "failed"
        )

        boss_agent.emit_run_completed(
            status=run_status,
            duration_seconds=duration_seconds,
            total_agents=stats.total_agents,
            completed_agents=stats.completed,
            failed_agents=stats.failed,
        )

        await self._repository.persist_events(
            boss_agent, self._progress_callback
        )

    def _reap_completed_tasks(
            self,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
            task_started_at: dict[UUID, float] | None = None,
    ) -> None:
        completed = [aid for aid, task in tasks.items() if task.done()]
        for aid in completed:
            in_progress.discard(aid)
            task = tasks.pop(aid)
            if task_started_at is not None:
                task_started_at.pop(aid, None)
            if task.cancelled():
                logger.warning("Agent %s task was cancelled", aid)
                continue
            exc = task.exception()
            if exc is not None:
                logger.error("Agent %s failed with exception", aid, exc_info=exc)

    async def _kill_overdue_tasks(
            self,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
            task_started_at: dict[UUID, float],
    ) -> None:
        """Cancel and fail any task running past the per-step budget.

        Safety net for cases where the in-step ``asyncio.wait_for`` cannot reach
        the running code (thread-bound worker, uncancellable subprocess wait, etc.).
        The 1.5x grace avoids racing the in-step timeout's own failure path.
        """
        budget = self._max_agent_step_seconds * 1.5
        now = time.monotonic()
        overdue = [
            aid
            for aid, started in task_started_at.items()
            if aid in tasks and not tasks[aid].done() and (now - started) > budget
        ]
        for aid in overdue:
            elapsed = now - task_started_at.get(aid, now)
            logger.error(
                "Agent %s task overdue (%.0fs > %.0fs); cancelling and failing",
                aid,
                elapsed,
                budget,
            )
            task = tasks.get(aid)
            if task is not None and not task.done():
                task.cancel()
            try:
                agent = await self._repository.load_if_exists(aid)
                if agent is not None and not agent.is_terminal():
                    await self._fail_agent(
                        agent,
                        f"Agent step exceeded watchdog budget {budget:.0f}s",
                    )
            except Exception:
                logger.exception("Watchdog failed to mark agent %s failed", aid)
            in_progress.discard(aid)
            tasks.pop(aid, None)
            task_started_at.pop(aid, None)

    # Budget before a silent aggregate is declared stale. Set high because
    # manager agents legitimately have no own-events while their sub-workers
    # are queued behind max_concurrent_workers=1. The run-level hard timeout
    # (max_run_duration_seconds) provides the ultimate safety net.
    _STALE_AGENT_BUDGET_SECONDS = 7200.0
    _STALE_AGENT_GRACE_SECONDS = 60.0  # skip checks while agent is fresh

    async def _kill_stale_agents(
            self,
            in_progress: set[UUID],
            agent_event_count: dict[UUID, int],
            agent_last_progress_at: dict[UUID, float],
    ) -> None:
        """Force-fail agents that stay in_progress without persisting any new events.

        Complements ``_kill_overdue_tasks``: that one catches a single task
        running too long; this one catches the pattern where ``run_agent_step``
        completes fast on each poll but the agent never reaches a terminal
        state (boss in judge-retry loop, manager in re-decomposition cycle).

        Polls event counts per active agent. Resets the timer when an agent's
        count grows. Fails the agent after ``_STALE_AGENT_BUDGET_SECONDS`` of
        zero progress.
        """
        if not in_progress:
            return
        now = time.monotonic()
        active_ids = list(in_progress)
        try:
            counts = await self._query_service.get_subtree_event_counts(active_ids)
        except Exception:
            logger.exception("Stale-agent watchdog failed to fetch event counts")
            return

        for aid in active_ids:
            current = counts.get(aid, 0)
            previous = agent_event_count.get(aid, current)
            agent_event_count[aid] = current
            if current > previous:
                # Real progress — reset the stale timer.
                agent_last_progress_at[aid] = now
                continue
            first_seen = agent_last_progress_at.setdefault(aid, now)
            stale_for = now - first_seen
            if stale_for <= self._STALE_AGENT_BUDGET_SECONDS:
                continue
            logger.error(
                "Agent %s stale: no new events for %.0fs (budget %.0fs); failing",
                aid,
                stale_for,
                self._STALE_AGENT_BUDGET_SECONDS,
            )
            try:
                agent = await self._repository.load_if_exists(aid)
                if agent is not None and not agent.is_terminal():
                    await self._fail_agent(
                        agent,
                        f"Agent stale for {stale_for:.0f}s with no event progress",
                    )
            except Exception:
                logger.exception("Stale-agent watchdog failed to fail agent %s", aid)
            in_progress.discard(aid)
            agent_event_count.pop(aid, None)
            agent_last_progress_at.pop(aid, None)

    def _is_run_timed_out(self, start_time: float) -> bool:
        return (
                time.monotonic() - start_time
        ) > self._system_limits.max_run_duration_seconds

    async def _handle_run_timeout(
            self,
            *,
            root_agent_id: UUID,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
    ) -> None:
        """Cancel in-flight work and fail any remaining active agents."""
        max_run_duration = self._system_limits.max_run_duration_seconds
        timeout_reason = f"Run timed out after {max_run_duration} seconds"

        logger.error(
            "System loop timed out for root %s after %s seconds",
            root_agent_id,
            max_run_duration,
        )

        await self._cancel_inflight_tasks(tasks, in_progress)
        await self._fail_remaining_active_agents(root_agent_id, timeout_reason)

    async def _cancel_inflight_tasks(
            self,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
    ) -> None:
        if not tasks:
            return

        for task in tasks.values():
            task.cancel()

        await asyncio.gather(*tasks.values(), return_exceptions=True)
        tasks.clear()
        in_progress.clear()

    async def _fail_remaining_active_agents(
            self,
            root_agent_id: UUID,
            reason: str,
    ) -> None:
        """Best-effort failure propagation for agents still active at timeout."""
        previous_active_ids: tuple[UUID, ...] | None = None

        while True:
            active_ids = tuple(
                await self._query_service.get_active_agent_ids(
                    root_id=root_agent_id,
                    sequential_workers=True,
                )
            )
            if not active_ids:
                return

            if active_ids == previous_active_ids:
                logger.error(
                    "Active agents remained after timeout handling for root %s: %s",
                    root_agent_id,
                    ", ".join(str(agent_id) for agent_id in active_ids),
                )
                return

            previous_active_ids = active_ids
            candidates: list[AgentSession] = []
            for agent_id in active_ids:
                agent = await self._repository.load_if_exists(agent_id)
                if agent is None or agent.is_terminal():
                    continue
                candidates.append(agent)

            candidates.sort(key=self._get_agent_depth, reverse=True)

            for agent in candidates:
                try:
                    await self._fail_agent(agent, reason)
                except Exception:
                    logger.exception(
                        "Failed to mark agent %s as timed out",
                        agent.agent_id,
                    )

    async def _fail_agent(self, agent: AgentSession, reason: str) -> None:
        if agent.is_terminal():
            return

        try:
            agent.fail_with_reason(reason)
            await self._repository.persist_events(
                agent,
                self._progress_callback,
            )
            await self._parent_notifier.notify_if_failed(agent)
        except ConcurrencyError:
            reloaded = await self._repository.load_if_exists(agent.agent_id)
            if reloaded is None or reloaded.is_terminal():
                return

            reloaded.fail_with_reason(reason)
            await self._repository.persist_events(
                reloaded,
                self._progress_callback,
            )
            await self._parent_notifier.notify_if_failed(reloaded)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        return await self._query_service.get_result(agent_id)

    async def get_system_statistics(
            self, root_id: UUID | None = None
    ) -> SystemStatisticsDTO:
        return await self._query_service.get_statistics(root_id=root_id)

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _reset_for_new_run(
            self, root_id: UUID, domain_context: object | None = None
    ) -> None:
        self._working_directory = None
        self._limits_registry.reset()
        self._child_factory.reset(initial_count=1)
        self._retry_policy.reset()

        self._limits_registry.create_root(
            root_id=root_id,
            max_depth=self._system_limits.max_depth,
            max_children_per_node=self._system_limits.max_children_per_node,
            max_retries=self._config.max_retries,
            max_total_agents=self._system_limits.max_total_agents,
            domain_context=domain_context,
        )

    def _get_run_metadata(self, domain_context: object | None) -> JsonObject | None:
        if self._domain_plugin is None or domain_context is None:
            return None

        metadata = self._domain_plugin.get_run_metadata(domain_context)
        return metadata or None

    async def _setup_working_directory(
            self,
            root_id: UUID,
            domain_context: object | None = None,
    ) -> "PreparedRunWorkspace | None":
        """Setup working directory for the run."""
        if not self._config.output_directory:
            return None

        base_output = Path(self._config.output_directory).resolve()
        base_output.mkdir(parents=True, exist_ok=True)

        run_output_path = base_output / str(root_id)
        run_output_path.mkdir(parents=True, exist_ok=True)

        self._working_directory = run_output_path
        if self._domain_plugin is None:
            return None

        prepared = await self._domain_plugin.prepare_run(
            root_id=root_id,
            run_output_path=run_output_path,
            domain_context=domain_context,
        )
        if prepared is not None and prepared.working_directory:
            self._working_directory = Path(prepared.working_directory)

        self._configure_recon_workspace(prepared)
        return prepared

    def _configure_recon_workspace(
        self,
        prepared: "PreparedRunWorkspace | None",
    ) -> None:
        if self._recon_tool is None or self._working_directory is None:
            return

        set_wd = getattr(self._recon_tool, "set_working_directory", None)
        if callable(set_wd):
            set_wd(str(self._working_directory))

        set_aliases = getattr(self._recon_tool, "set_path_aliases", None)
        if callable(set_aliases):
            aliases = prepared.path_aliases if prepared is not None else ()
            set_aliases(aliases)

        logger.info("Recon tools targeting: %s", self._working_directory)

    # Top-level directories that contain the target project source tree.
    # Workers access source code inside their container, not via the
    # workspace listing, so including these would bloat the prompt with
    # tens of thousands of irrelevant paths.
    _WORKSPACE_SKIP_DIRS: frozenset[str] = frozenset({"src"})

    def _get_workspace_context(self) -> str | None:
        """Return fresh file listing for workspace.

        Always scans fresh so workers can see files created by other workers.
        With ``workspace_listing_dirs`` set, only those top-level directories
        are listed (artifact discovery without build-tree noise: an out-of-tree
        CMake build under work/ alone is ~650 object/Makefile entries). The
        legacy mode skips just the source tree (``src/``), which is accessed
        inside the container — only worker-produced artifacts matter here.
        """
        if self._working_directory is None or not self._working_directory.exists():
            return None

        include_dirs = self._config.workspace_listing_dirs

        try:
            files = []
            for item in self._working_directory.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                rel_path = item.relative_to(self._working_directory)
                if not rel_path.parts:
                    continue
                if include_dirs is not None:
                    if rel_path.parts[0] not in include_dirs:
                        continue
                elif rel_path.parts[0] in self._WORKSPACE_SKIP_DIRS:
                    continue
                if item.is_file():
                    files.append(str(rel_path))

            if not files:
                return None

            files.sort()
            max_entries = self._config.workspace_listing_max_entries
            if max_entries is not None and len(files) > max_entries:
                overflow = len(files) - max_entries
                files = [*files[:max_entries], f"… (+{overflow} more files)"]
            return "\n".join(f"- {f}" for f in files)
        except OSError:
            return None

    def _get_working_directory_str(self) -> str | None:
        return str(self._working_directory) if self._working_directory else None

    def _create_boss_session(self, root_id: UUID, task_description: str) -> AgentSession:
        boss_cfg = self._config.boss_config
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_cfg.model,
                "temperature": boss_cfg.temperature,
                "max_tokens": boss_cfg.max_tokens,
                "api_base": boss_cfg.api_base,
            },
            "tool": self._config.default_worker_tool,
        }

        boss_agent = AgentSession.create(
            agent_id=root_id,
            role=AgentRole.BOSS,
            config=boss_config,
            parent_id=None,
        )
        boss_agent.assign_task(task_description)
        return boss_agent

    async def _load_agent_with_context(self, agent_id: UUID) -> AgentSession:
        agent = await self._repository.load(agent_id)

        limits = self._limits_registry.get(agent_id)
        if limits is not None:
            limits = limits.with_agent_counts(
                current_total=self._child_factory.total_created,
                max_total=self._child_factory.max_total_agents,
            )
            agent.set_hierarchy_limits(limits)

        return agent

    async def _dispatch_agent_action(self, agent: AgentSession) -> None:
        """Dispatch based on role."""
        if agent.status != AgentStatus.ANALYZING:
            return

        handler = self._role_handlers.get(agent.role)
        if handler is None:
            raise ValueError(f"Unknown agent role: {agent.role}")

        await handler.handle(agent)

    def _get_agent_depth(self, agent: AgentSession) -> int:
        limits = self._limits_registry.get(agent.agent_id)
        return limits.current_depth if limits else 0

    async def _persist_agent_events(
            self, agent: AgentSession
    ) -> list[DomainEvent]:
        return await self._repository.persist_events(
            agent, self._progress_callback
        )

    async def _handle_post_step(
            self, agent: AgentSession, events: list[DomainEvent]
    ) -> None:
        child_events = [e for e in events if isinstance(e, ChildSpawned)]
        if child_events:
            await self._child_factory.create_children_from_events(
                child_events, agent.agent_id
            )
            # The factory's ``commit_reservation`` has now moved the
            # reserved slots into ``_total_created`` for these children;
            # clear the in-memory marker so a later failure path on the
            # same aggregate cannot double-release the slot count.
            agent.pending_reservation = 0

        if agent.role == AgentRole.WORKER and agent.status == AgentStatus.COMPLETED:
            await self._process_worker_context_updates(agent)

        if agent.status == AgentStatus.COMPLETED:
            await self._parent_notifier.notify_if_complete(agent)
            return

        if agent.status == AgentStatus.FAILED:
            # Verification-specific retry: up to 2 retries with judge feedback
            if await self._maybe_retry_verification(agent):
                return

            retried = await self._retry_policy.maybe_schedule_retry(agent)
            if retried:
                return  # Agent re-queued for execution, don't notify parent

            await self._parent_notifier.notify_if_failed(agent)

    async def _maybe_retry_verification(self, agent: AgentSession) -> bool:
        """Retry a worker that failed verification, up to verification_max_retries.

        Returns True if a retry was scheduled (caller should not notify parent).
        """
        if agent.role != AgentRole.WORKER:
            return False

        if not agent.verification_feedback:
            return False

        if agent.retry_count >= self._config.verification_max_retries:
            return False

        reason = agent.error_message or "Verification failed"
        agent.schedule_retry(reason=reason, escalated_model=None)

        await self._repository.persist_events(
            agent, self._progress_callback
        )
        logger.info(
            "Verification retry %d/%d for agent %s: %s",
            agent.retry_count,
            self._config.verification_max_retries,
            agent.agent_id,
            agent.verification_feedback,
        )
        return True

    async def _process_worker_context_updates(self, agent: AgentSession) -> None:
        if agent.result is None:
            return

        parsed = parse_context_update(agent.result)
        if parsed is None:
            return

        root_id = self._limits_registry.get_root_id(agent.agent_id)
        context = await self._shared_context_port.get(root_id)
        if context is None:
            return

        current_version = context.version

        for decision in parsed.decisions:
            context.record_decision(
                decision_key=decision.key,
                decision_value=decision.value,
                rationale=decision.rationale,
                decided_by=agent.agent_id,
            )

        for output in parsed.artifacts:
            context.store_artifact(
                key=output.key,
                content_type="text/plain",
                stored_by=agent.agent_id,
                content=output.description,
            )

        if context.events:
            await self._shared_context_port.save(context, expected_version=current_version)
            context.mark_changes_as_committed()

    async def _handle_step_failure(self, agent_id: UUID, error: Exception) -> None:
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                return

            agent.fail_with_reason(f"Execution error: {error!r}")
            await self._repository.persist_events(
                agent, self._progress_callback
            )

            await self._parent_notifier.notify_if_failed(agent)
        except Exception as persist_error:
            raise RuntimeError(
                f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
            ) from error
