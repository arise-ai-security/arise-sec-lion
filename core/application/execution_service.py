"""Agent Execution Service - orchestrates agent lifecycle and execution.

This is the main orchestration service that coordinates:
- Agent step execution (load -> dispatch -> persist)
- Child agent spawning
- Parent notification on completion
- System loop for continuous execution
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
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
from core.domain.events.events import ChildSpawned, DomainEvent
from core.domain.exceptions import ConcurrencyError
from core.domain.services.context_update_parser import parse_context_update
from core.domain.values.json_types import JsonObject


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from config import BossConfig, ManagerConfig
    from core.application.services import PromptBuilder
    from core.ports.domain_plugin_port import DomainPlugin
    from core.ports.event_store_port import EventStorePort
    from core.ports.runtime_ports import SharedContextPort, SiblingViewPort, SystemLimitsPort, Toolset


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for AgentExecutionService."""

    max_retries: int
    poll_interval: float
    output_directory: str
    default_worker_tool: str
    boss_config: "BossConfig"
    manager_config: "ManagerConfig"
    step_timeout_seconds: float = 600.0  # Hard cap per agent step; prevents hung LLM/worker calls from blocking the loop
    stall_timeout_seconds: float = 600.0  # No-progress watchdog; kills run when scheduler produces no work
    worker_silence_timeout_seconds: float = 600.0  # Per-worker watchdog; must be well below batch STALL_SEC (1200)


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
    recon_tool: "Toolset | None" = None


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

        # Workspace state (inlined from WorkspaceContextProvider)
        self._working_directory: Path | None = None

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
        self._llm_semaphore_holders: set[UUID] = set()
        self._llm_semaphore_waiters: set[UUID] = set()
        self._worker_semaphore_holders: set[UUID] = set()
        # Shared with the system loop so tasks can reset their own clock
        # after acquiring the semaphore (semaphore wait ≠ silence).
        self._task_started_at: dict[UUID, float] = {}
        self._llm_jitter_max_ms = system_limits.llm_jitter_max_ms

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
                worker_semaphore_holders=self._worker_semaphore_holders,
                domain_plugin=self._domain_plugin,
                get_agent_depth=self._get_agent_depth,
                get_root_id=self._limits_registry.get_root_id,
                get_run_output_path=lambda: self._working_directory,
                get_working_directory=self._get_working_directory_str,
                get_workspace_context=self._get_workspace_context,
            )
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize infrastructure connections."""
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
        """Create root BOSS agent with task."""
        root_id = uuid4()
        self._reset_for_new_run(root_id, domain_context=domain_context)

        self._prompt_builder.set_run_context(
            user_prompt=task_description,
            domain_context=domain_context,
        )
        await self._setup_working_directory(root_id, domain_context)
        await self._create_shared_context(root_id)

        boss_agent = self._create_boss_session(root_id, task_description)
        domain_metadata = self._get_run_metadata(domain_context)
        boss_agent.emit_run_started(
            task_description=task_description,
            domain_metadata=domain_metadata,
        )
        await self._repository.save_new_agent(boss_agent)

        return root_id

    async def _create_shared_context(self, root_id: UUID) -> None:
        """Create shared context for execution run."""
        shared_context = await self._shared_context_port.get_or_create(
            root_id=root_id, config={}
        )
        await self._shared_context_port.save(shared_context, expected_version=0)

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one workflow step: load -> dispatch -> persist with OCC retry."""
        retry_count = 0

        while retry_count < self._config.max_retries:
            try:
                agent = await self._load_agent_with_context(agent_id)
                current_version = agent.version

                # Emit early heartbeat for workers so the stall detector
                # sees DB activity even if the step takes a long time.
                if agent.role == AgentRole.WORKER and agent.status == AgentStatus.ANALYZING:
                    current_version = await self._emit_early_heartbeat(
                        agent, current_version,
                    )

                await self._dispatch_agent_action(agent)
                uncommitted = await self._persist_agent_events(agent, current_version)
                await self._handle_post_step(agent, uncommitted)
                return

            except ConcurrencyError as e:
                retry_count += 1
                if retry_count >= self._config.max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e

            except Exception as e:
                await self._handle_step_failure(agent_id, e)
                raise

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents until all reach terminal state."""
        start_time = time.monotonic()
        last_progress_time = time.monotonic()
        in_progress: set[UUID] = set()
        tasks: dict[UUID, asyncio.Task] = {}
        task_started_at = self._task_started_at
        task_started_at.clear()
        run_status_override: str | None = None

        while True:
            reaped = self._reap_completed_tasks(tasks, in_progress, task_started_at)

            silenced = await self._reap_silent_workers(tasks, in_progress, task_started_at)

            if self._is_run_timed_out(start_time):
                run_status_override = "timed_out"
                await self._handle_run_timeout(
                    root_agent_id=root_agent_id,
                    tasks=tasks,
                    in_progress=in_progress,
                )
                break

            active_agents = await self._query_service.get_active_agent_ids(
                root_id=root_agent_id, sequential_workers=True
            )
            new_agents = [aid for aid in active_agents if aid not in in_progress]

            llm_skip = self._query_service.llm_skip_agents
            now = time.monotonic()
            for agent_id in new_agents:
                in_progress.add(agent_id)
                task_started_at[agent_id] = now
                tasks[agent_id] = asyncio.create_task(
                    self._run_agent_step_safe(
                        agent_id,
                        skip_llm_semaphore=agent_id in llm_skip,
                    )
                )

            # Track progress for stall detection
            if reaped > 0 or silenced > 0 or new_agents:
                last_progress_time = time.monotonic()

            if not active_agents and not in_progress:
                # Verify the run is truly done (all agents terminal), not just
                # that all actionable agents are in-progress.  get_active_agent_ids
                # now filters out WAITING non-workers, so an empty return only
                # means "nothing to schedule" — children may still be running.
                has_non_terminal = await self._query_service.has_non_terminal_agents(
                    root_id=root_agent_id
                )
                if not has_non_terminal:
                    break
                # Non-terminal agents exist but nothing is schedulable
                if self._is_stalled(last_progress_time):
                    logger.error(
                        "Scheduling stall detected for run %s: "
                        "non-terminal agents exist but no work schedulable "
                        "for %.0fs",
                        root_agent_id,
                        self._config.stall_timeout_seconds,
                    )
                    run_status_override = "scheduling_deadlock"
                    await self._handle_run_timeout(
                        root_agent_id=root_agent_id,
                        tasks=tasks,
                        in_progress=in_progress,
                    )
                    break

            await asyncio.sleep(self._config.poll_interval)

        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

        await self._emit_run_completed(
            root_agent_id,
            start_time,
            status_override=run_status_override,
        )

    async def _run_agent_step_safe(
        self, agent_id: UUID, *, skip_llm_semaphore: bool = False,
    ) -> None:
        """Execute agent step with exception handling, jitter, and rate limiting.

        The step timeout and silence-watchdog clock both start AFTER the
        LLM semaphore is acquired — time spent queuing behind other
        assessments is not counted.  The silence watchdog (system-loop
        level) acts as a safety net: if asyncio.wait_for cannot cancel a
        hung C-level operation, the watchdog force-releases the
        semaphore and marks the agent failed.
        """
        try:
            if self._llm_jitter_max_ms > 0:
                jitter_ms = random.randint(0, self._llm_jitter_max_ms)
                await asyncio.sleep(jitter_ms / 1000.0)

            await self._run_step_with_semaphore(agent_id, skip_llm_semaphore=skip_llm_semaphore)
        except asyncio.CancelledError:
            logger.warning("Agent %s step cancelled", agent_id)
            raise
        except Exception:
            logger.exception("Agent %s step failed unexpectedly", agent_id)

    async def _run_step_with_semaphore(
        self, agent_id: UUID, *, skip_llm_semaphore: bool = False,
    ) -> None:
        """Acquire LLM semaphore (unless skipped), then run the step under a hard timeout.

        The timeout and silence-watchdog clock start AFTER the semaphore
        is acquired so that time spent queuing behind other agents is
        not counted.  Agents waiting for the semaphore are tracked in
        ``_llm_semaphore_waiters`` so the silence watchdog skips them.

        Worker execution steps set ``skip_llm_semaphore=True`` because
        they run inside Docker sandboxes that make their own LLM calls —
        our adapter is not involved.
        """
        if skip_llm_semaphore:
            if agent_id in self._task_started_at:
                self._task_started_at[agent_id] = time.monotonic()
            try:
                await asyncio.wait_for(
                    self.run_agent_step(agent_id),
                    timeout=self._config.step_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Agent %s step timed out after %.0fs — marking failed",
                    agent_id,
                    self._config.step_timeout_seconds,
                )
                await self._handle_step_timeout(agent_id)
            return

        self._llm_semaphore_waiters.add(agent_id)
        try:
            async with self._llm_semaphore:
                self._llm_semaphore_waiters.discard(agent_id)
                self._llm_semaphore_holders.add(agent_id)
                if agent_id in self._task_started_at:
                    self._task_started_at[agent_id] = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self.run_agent_step(agent_id),
                        timeout=self._config.step_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Agent %s step timed out after %.0fs — marking failed",
                        agent_id,
                        self._config.step_timeout_seconds,
                    )
                    await self._handle_step_timeout(agent_id)
                finally:
                    self._llm_semaphore_holders.discard(agent_id)
        finally:
            self._llm_semaphore_waiters.discard(agent_id)

    async def _emit_run_completed(
            self,
            root_agent_id: UUID,
            start_time: float,
            status_override: str | None = None,
    ) -> None:
        """Emit RunCompleted event on BOSS agent with run statistics."""
        duration_seconds = time.monotonic() - start_time
        stats = await self._query_service.get_statistics(root_id=root_agent_id)

        boss_agent = await self._repository.load(root_agent_id)
        current_version = boss_agent.version
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
            boss_agent, current_version, self._progress_callback
        )

    def _reap_completed_tasks(
            self,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
            task_started_at: dict[UUID, float] | None = None,
    ) -> int:
        """Remove completed tasks, log their terminal state, return count."""
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
        return len(completed)

    async def _reap_silent_workers(
            self,
            tasks: dict[UUID, asyncio.Task],
            in_progress: set[UUID],
            task_started_at: dict[UUID, float],
    ) -> int:
        """Cancel and fail tasks that have been running silently for too long.

        A worker is "silent" when its asyncio task has been alive longer than
        worker_silence_timeout_seconds without completing — meaning step_timeout
        either failed to fire or the failure handler hung. Cancels the task,
        force-marks the agent failed, and notifies the parent so the DAG can
        advance.
        """
        now = time.monotonic()
        timeout = self._config.worker_silence_timeout_seconds
        stale = [
            aid for aid, started in task_started_at.items()
            if (now - started) > timeout
            and not tasks[aid].done()
            and aid not in self._llm_semaphore_waiters
        ]
        if not stale:
            return 0

        for aid in stale:
            logger.error(
                "Agent %s silent for >%.0fs — cancelling and marking failed",
                aid, timeout,
            )
            tasks[aid].cancel()
            self._force_release_semaphores(aid)
            try:
                await self._mark_agent_silently_failed(aid, timeout)
            except Exception:
                logger.exception(
                    "Failed to mark silent agent %s as failed", aid,
                )
            tasks.pop(aid, None)
            task_started_at.pop(aid, None)
            in_progress.discard(aid)
        return len(stale)

    async def _mark_agent_silently_failed(
            self, agent_id: UUID, timeout_seconds: float,
    ) -> None:
        """Mark agent failed for silence and notify parent."""
        agent = await self._repository.load_if_exists(agent_id)
        if agent is None or agent.is_terminal():
            return
        agent.fail_with_reason(
            f"Worker silent for >{timeout_seconds:.0f}s with no events — "
            f"forcing failure to advance DAG",
        )
        await self._repository.persist_events(
            agent, agent.version, self._progress_callback,
        )
        await self._parent_notifier.notify_if_failed(agent)

    def _force_release_semaphores(self, agent_id: UUID) -> None:
        """Release semaphores held by a leaked (uncancellable) task.

        When asyncio.wait_for cannot cancel a task blocked on a C-level
        operation (Docker API, socket read), the task continues running
        and holds its semaphores indefinitely.  This starves subsequent
        tasks.  Force-releasing restores capacity so the DAG can advance.
        """
        if agent_id in self._llm_semaphore_holders:
            self._llm_semaphore.release()
            self._llm_semaphore_holders.discard(agent_id)
            logger.warning(
                "Force-released LLM semaphore for leaked task %s", agent_id,
            )
        if agent_id in self._worker_semaphore_holders:
            self._worker_semaphore.release()
            self._worker_semaphore_holders.discard(agent_id)
            logger.warning(
                "Force-released worker semaphore for leaked task %s", agent_id,
            )

    def _is_stalled(self, last_progress_time: float) -> bool:
        """Return True when no scheduling progress for stall_timeout_seconds."""
        return (
            time.monotonic() - last_progress_time
        ) > self._config.stall_timeout_seconds

    def _is_run_timed_out(self, start_time: float) -> bool:
        """Return True when the global orchestration deadline has elapsed."""
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
        """Cancel all in-flight tasks and wait for cancellation to settle."""
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
        """Persist a failure for a still-active agent and notify its parent."""
        if agent.is_terminal():
            return

        try:
            current_version = agent.version
            agent.fail_with_reason(reason)
            await self._repository.persist_events(
                agent,
                current_version,
                self._progress_callback,
            )
            await self._parent_notifier.notify_if_failed(agent)
        except ConcurrencyError:
            reloaded = await self._repository.load_if_exists(agent.agent_id)
            if reloaded is None or reloaded.is_terminal():
                return

            current_version = reloaded.version
            reloaded.fail_with_reason(reason)
            await self._repository.persist_events(
                reloaded,
                current_version,
                self._progress_callback,
            )
            await self._parent_notifier.notify_if_failed(reloaded)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        """Get agent execution result."""
        return await self._query_service.get_result(agent_id)

    async def get_system_statistics(
            self, root_id: UUID | None = None
    ) -> SystemStatisticsDTO:
        """Get agent statistics."""
        return await self._query_service.get_statistics(root_id=root_id)

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _reset_for_new_run(
            self, root_id: UUID, domain_context: object | None = None
    ) -> None:
        """Reset all state for a new execution run."""
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
        """Build JSON-safe run metadata from the active domain plugin."""
        if self._domain_plugin is None or domain_context is None:
            return None

        metadata = self._domain_plugin.get_run_metadata(domain_context)
        return metadata or None

    async def _setup_working_directory(
            self,
            root_id: UUID,
            domain_context: object | None = None,
    ) -> None:
        """Setup working directory for the run."""
        if not self._config.output_directory:
            return

        base_output = Path(self._config.output_directory).resolve()
        base_output.mkdir(parents=True, exist_ok=True)

        run_output_path = base_output / str(root_id)
        run_output_path.mkdir(parents=True, exist_ok=True)

        self._working_directory = run_output_path
        if self._domain_plugin is None:
            return

        prepared = await self._domain_plugin.prepare_run(
            root_id=root_id,
            run_output_path=run_output_path,
            domain_context=domain_context,
        )
        if prepared is not None and prepared.working_directory:
            self._working_directory = Path(prepared.working_directory)

        # Point recon tools at the workspace so thinker agents can read target code
        if self._recon_tool is not None and self._working_directory is not None:
            set_wd = getattr(self._recon_tool, "set_working_directory", None)
            if callable(set_wd):
                set_wd(str(self._working_directory))
                logger.info("Recon tools targeting: %s", self._working_directory)

    # Top-level directories that contain the target project source tree.
    # Workers access source code inside their container, not via the
    # workspace listing, so including these would bloat the prompt with
    # tens of thousands of irrelevant paths.
    _WORKSPACE_SKIP_DIRS: frozenset[str] = frozenset({"src"})

    def _get_workspace_context(self) -> str | None:
        """Return fresh file listing for workspace.

        Always scans fresh so workers can see files created by other workers.
        Skips source-tree directories (``src/``) that are accessed inside
        the container — only worker-produced artifacts matter here.
        """
        if self._working_directory is None or not self._working_directory.exists():
            return None

        try:
            files = []
            for item in self._working_directory.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                rel_path = item.relative_to(self._working_directory)
                if rel_path.parts and rel_path.parts[0] in self._WORKSPACE_SKIP_DIRS:
                    continue
                if item.is_file():
                    files.append(str(rel_path))

            if not files:
                return None

            files.sort()
            return "\n".join(f"- {f}" for f in files)
        except OSError:
            return None

    def _get_working_directory_str(self) -> str | None:
        """Get the current working directory as string."""
        return str(self._working_directory) if self._working_directory else None

    def _create_boss_session(self, root_id: UUID, task_description: str) -> AgentSession:
        """Create the root BOSS agent session."""
        boss_cfg = self._config.boss_config
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_cfg.model,
                "temperature": boss_cfg.temperature,
                "max_tokens": boss_cfg.max_tokens,
                "api_base": getattr(boss_cfg, "api_base", None),
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
        """Load agent and attach hierarchy limits with current agent counts."""
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
        """Get agent depth from hierarchy limits, defaulting to 0."""
        limits = self._limits_registry.get(agent.agent_id)
        return limits.current_depth if limits else 0

    async def _persist_agent_events(
            self, agent: AgentSession, base_version: int
    ) -> list[DomainEvent]:
        """Persist uncommitted events and return them."""
        return await self._repository.persist_events(
            agent, base_version, self._progress_callback
        )

    async def _handle_post_step(
            self, agent: AgentSession, events: list[DomainEvent]
    ) -> None:
        """Handle post-step operations after agent events are persisted."""
        child_events = [e for e in events if isinstance(e, ChildSpawned)]
        if child_events:
            await self._child_factory.create_children_from_events(
                child_events, agent.agent_id
            )

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

    _MAX_VERIFICATION_RETRIES = 2

    async def _maybe_retry_verification(self, agent: AgentSession) -> bool:
        """Retry a worker that failed verification, up to _MAX_VERIFICATION_RETRIES.

        Returns True if a retry was scheduled (caller should not notify parent).
        """
        if agent.role != AgentRole.WORKER:
            return False

        if not agent.verification_feedback:
            return False

        if agent.retry_count >= self._MAX_VERIFICATION_RETRIES:
            return False

        reason = agent.error_message or "Verification failed"
        agent.schedule_retry(reason=reason, escalated_model=None)

        await self._repository.persist_events(
            agent, agent.version - 1, self._progress_callback
        )
        logger.info(
            "Verification retry %d/%d for agent %s: %s",
            agent.retry_count,
            self._MAX_VERIFICATION_RETRIES,
            agent.agent_id,
            agent.verification_feedback,
        )
        return True

    async def _process_worker_context_updates(self, agent: AgentSession) -> None:
        """Extract and store context updates from worker result."""
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

    async def _emit_early_heartbeat(
            self, agent: AgentSession, base_version: int,
    ) -> int:
        """Persist an AgentExecutionStarted event before the step runs.

        This ensures the DB has recent activity so external stall detectors
        do not kill the process while a long-running step is in progress.
        Returns the updated version after persistence.
        """
        agent.emit_execution_started(
            role=agent.role.value,
            depth=self._get_agent_depth(agent),
        )
        events = await self._repository.persist_events(
            agent, base_version, self._progress_callback,
        )
        return base_version + len(events)

    async def _handle_step_failure(self, agent_id: UUID, error: Exception) -> None:
        """Handle execution failure by persisting error state and notifying parent."""
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                return

            agent.fail_with_reason(f"Execution error: {error!r}")
            await self._repository.persist_events(
                agent, agent.version, self._progress_callback
            )

            await self._parent_notifier.notify_if_failed(agent)
        except Exception as persist_error:
            raise RuntimeError(
                f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
            ) from error

    async def _handle_step_timeout(self, agent_id: UUID) -> None:
        """Mark agent failed after step timeout and notify parent.

        Loads the agent fresh from DB (the in-memory state was lost with the
        cancelled task), marks it FAILED, persists, and propagates so the
        system loop can schedule subsequent workers.
        """
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None or agent.is_terminal():
                return

            current_version = agent.version
            agent.fail_with_reason(
                f"Step timed out after {self._config.step_timeout_seconds:.0f}s "
                "(possible Ollama Cloud hang)"
            )
            await self._repository.persist_events(
                agent, current_version, self._progress_callback
            )
            await self._parent_notifier.notify_if_failed(agent)
        except Exception:
            logger.exception(
                "Failed to handle step timeout for agent %s", agent_id
            )
