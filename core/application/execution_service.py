"""Agent Execution Service - orchestrates agent lifecycle and execution.

This is the main orchestration service that coordinates:
- Agent step execution (load -> dispatch -> persist)
- Child agent spawning
- Parent notification on completion
- System loop for continuous execution
"""



import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.application.services.agent_repository import AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.context_registry import HierarchyLimitsRegistry
from core.application.services.parent_notifier import ParentNotificationService
from core.application.services.query_service import AgentQueryService
from core.application.services.workspace_context import WorkspaceContextProvider
from core.domain.services.context_update_parser import parse_context_update
from core.domain.events.events import ChildSpawned, DomainEvent
from core.domain.exceptions import ConcurrencyError
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus


if TYPE_CHECKING:
    from config import BossConfig, ManagerConfig, OrchestrationConfig
    from core.domain.values.cve_instance import CVEInstance
    from core.ports.event_store_port import EventStorePort
    from core.ports.shared_context_port import SharedContextPort
    from core.ports.sibling_context_port import SiblingViewPort
    from core.ports.task_registry_port import TaskRegistryPort

    SystemLimitsConfig = OrchestrationConfig.LimitsConfig


type ProgressCallback = Callable[[DomainEvent, Any], None]


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for AgentExecutionService.

    Immutable configuration object following the Parameter Object pattern.
    """

    max_retries: int
    poll_interval: float
    output_directory: str
    default_worker_tool: str
    boss_config: "BossConfig"
    manager_config: "ManagerConfig"


@dataclass(frozen=True)
class ExecutionServiceDependencies:
    """Collaborators for AgentExecutionService (Parameter Object pattern).

    Groups the injected dependencies to reduce constructor parameter count.
    All collaborators follow Dependency Inversion Principle (DIP).
    """

    repository: AgentRepository
    orchestrator: AgentOrchestrator
    limits_registry: HierarchyLimitsRegistry
    child_factory: ChildAgentFactory
    query_service: AgentQueryService
    workspace: WorkspaceContextProvider
    shared_context_port: "SharedContextPort"
    sibling_view_port: "SiblingViewPort"
    parent_notifier: ParentNotificationService
    task_registry: "TaskRegistryPort"


class AgentExecutionService:
    """Orchestrates agent workflow using composed services.

    This is a thin coordinator that delegates to specialized services:
    - AgentRepository: Load/persist agent state
    - ChildAgentFactory: Create child agents
    - AgentQueryService: Read operations (CQRS)
    - HierarchyLimitsRegistry: Manage hierarchy limits
    - WorkspaceContextProvider: Workspace file context
    - SharedContextPort: Shared execution context for budget and artifacts
    """

    def __init__(
        self,
        event_store: "EventStorePort",
        dependencies: ExecutionServiceDependencies,
        config: ServiceConfig,
        system_limits: "SystemLimitsConfig",
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Initialize execution service.

        Args:
            event_store: Event store port for persistence.
            dependencies: Grouped collaborators (repository, orchestrator, etc.).
            config: Service configuration (retries, intervals, etc.).
            system_limits: System resource limits (workers, depth, etc.).
            progress_callback: Optional callback for progress notifications.
        """
        # Core ports
        self._event_store = event_store
        self._shared_context_port = dependencies.shared_context_port
        self._sibling_view_port = dependencies.sibling_view_port

        # Configuration
        self._config = config
        self._system_limits = system_limits

        # Injected collaborators (unpacked from dependencies)
        self._repository = dependencies.repository
        self._orchestrator = dependencies.orchestrator
        self._limits_registry = dependencies.limits_registry
        self._child_factory = dependencies.child_factory
        self._query_service = dependencies.query_service
        self._workspace = dependencies.workspace
        self._parent_notifier = dependencies.parent_notifier
        self._task_registry = dependencies.task_registry

        # Worker concurrency control
        semaphore_limit = (
            system_limits.max_concurrent_workers
            if system_limits.is_workers_limited()
            else 10000  # Effectively unlimited
        )
        self._worker_semaphore = asyncio.Semaphore(semaphore_limit)

        # LLM concurrency control (prevents API rate limiting)
        llm_limit = (
            system_limits.max_concurrent_llm_calls
            if system_limits.is_llm_limited()
            else 10000  # Effectively unlimited
        )
        self._llm_semaphore = asyncio.Semaphore(llm_limit)
        self._llm_jitter_max_ms = system_limits.llm_jitter_max_ms

        # Progress callback
        self._progress_callback = progress_callback

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize infrastructure connections."""
        await self._event_store.connect()
        await self._event_store.initialize_schema()
        await self._task_registry.ensure_table_exists()

    async def cleanup(self) -> None:
        """Cleanup infrastructure connections."""
        await self._event_store.disconnect()

    async def create_boss_agent(
        self,
        task_description: str,
        cve_instance: "CVEInstance | None" = None,
    ) -> UUID:
        """Create root BOSS agent with task.

        Initializes execution context and shared context for the agent hierarchy.

        Args:
            task_description: The task to execute.
            cve_instance: Optional SEC-bench CVE instance for benchmark runs.

        Returns:
            The root agent UUID.
        """
        root_id = uuid4()
        self._reset_for_new_run(root_id, cve_instance=cve_instance)

        # Setup working directory
        self._setup_working_directory(root_id)

        # Create shared context for this execution run
        await self._create_shared_context(root_id)

        # Create and persist boss agent
        boss_agent = self._create_boss_session(root_id, task_description)
        await self._repository.save_new_agent(boss_agent)

        return root_id

    async def _create_shared_context(self, root_id: UUID) -> None:
        """Create shared context for execution run."""
        shared_context = await self._shared_context_port.get_or_create(
            root_id=root_id,
            config={},
        )
        # Persist the initial shared context
        await self._shared_context_port.save(shared_context, expected_version=0)

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one workflow step: load -> dispatch -> persist with OCC retry."""
        retry_count = 0

        while retry_count < self._config.max_retries:
            try:
                agent = await self._load_agent_with_context(agent_id)
                current_version = agent.version

                await self._dispatch_agent_action(agent)
                uncommitted = await self._persist_agent_events(agent, current_version)
                await self._handle_post_step(agent, uncommitted)
                return  # Success

            except ConcurrencyError as e:
                retry_count += 1
                if retry_count >= self._config.max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e
                # Will retry by reloading agent

            except Exception as e:
                await self._handle_step_failure(agent_id, e)
                raise

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents until all reach terminal state.

        Args:
            root_agent_id: Root agent ID to filter hierarchy. Only agents
                           in this hierarchy will be processed.

        Uses fire-and-forget pattern for non-blocking execution:
        - Continuously polls for new agents without waiting for LLM calls
        - Managers decompose in parallel, spawning children immediately
        - Workers execute one at a time in left-to-right tree order
        """
        in_progress: set[UUID] = set()
        tasks: dict[UUID, asyncio.Task] = {}

        while True:
            # Clean up completed tasks
            completed = [aid for aid, task in tasks.items() if task.done()]
            for aid in completed:
                in_progress.discard(aid)
                task = tasks.pop(aid)
                # Log any exceptions
                if task.exception():
                    print(f"Error executing agent {aid}: {task.exception()!r}")

            # Get active agents not already being processed
            active_agents = await self._query_service.get_active_agent_ids(
                root_id=root_agent_id,
                sequential_workers=True,
            )
            new_agents = [aid for aid in active_agents if aid not in in_progress]

            # Spawn tasks for new agents (fire-and-forget)
            for agent_id in new_agents:
                in_progress.add(agent_id)
                tasks[agent_id] = asyncio.create_task(
                    self._run_agent_step_safe(agent_id)
                )

            # Exit when no active agents and no in-progress tasks
            if not active_agents and not in_progress:
                break

            await asyncio.sleep(self._config.poll_interval)

        # Wait for any remaining tasks to complete
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def _run_agent_step_safe(self, agent_id: UUID) -> None:
        """Execute agent step with exception handling, jitter, and rate limiting."""
        try:
            # Apply jitter to spread out API calls
            if self._llm_jitter_max_ms > 0:
                jitter_ms = random.randint(0, self._llm_jitter_max_ms)
                await asyncio.sleep(jitter_ms / 1000.0)

            # Use LLM semaphore to limit concurrent API calls
            async with self._llm_semaphore:
                await self.run_agent_step(agent_id)
        except Exception as e:
            print(f"Error executing agent {agent_id}: {e!r}")

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        """Get agent execution result."""
        return await self._query_service.get_result(agent_id)

    async def get_system_statistics(self) -> SystemStatisticsDTO:
        """Get system-wide agent statistics."""
        return await self._query_service.get_statistics()

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _reset_for_new_run(
        self,
        root_id: UUID,
        cve_instance: "CVEInstance | None" = None,
    ) -> None:
        """Reset all state for a new execution run."""
        self._workspace.reset()
        self._limits_registry.reset()
        self._child_factory.reset(initial_count=1)  # Count the boss agent

        # Create root hierarchy limits
        self._limits_registry.create_root(
            root_id=root_id,
            max_depth=self._system_limits.max_depth,
            max_children_per_node=self._system_limits.max_children_per_node,
            max_retries=self._config.max_retries,
            max_total_agents=self._system_limits.max_total_agents,
            cve_instance=cve_instance,
        )

    def _setup_working_directory(self, root_id: UUID) -> None:
        """Setup working directory for the run."""
        if not self._config.output_directory:
            return

        base_output = Path(self._config.output_directory).resolve()
        base_output.mkdir(parents=True, exist_ok=True)

        run_output_path = base_output / str(root_id)
        run_output_path.mkdir(parents=True, exist_ok=True)

        self._workspace.set_working_directory(run_output_path)

    def _create_boss_session(self, root_id: UUID, task_description: str) -> AgentSession:
        """Create the root BOSS agent session."""
        boss_cfg = self._config.boss_config
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_cfg.model,
                "temperature": boss_cfg.temperature,
                "max_tokens": boss_cfg.max_tokens,
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
            # Update limits with current agent counts for limit enforcement
            limits = limits.with_agent_counts(
                current_total=self._child_factory.total_created,
                max_total=self._child_factory.max_total_agents,
            )
            agent.set_hierarchy_limits(limits)

        return agent

    async def _dispatch_agent_action(self, agent: AgentSession) -> None:
        """Dispatch based on role: PENDING->complexity, BOSS/MANAGER->decompose, WORKER->execute.

        Uses AgentOrchestrator to handle LLM/worker interactions, keeping domain pure.
        """
        if agent.status != AgentStatus.ANALYZING:
            return

        if agent.role == AgentRole.PENDING:
            await self._orchestrator.evaluate_complexity(agent)

        elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
            await self._orchestrator.evaluate_task(agent)

        elif agent.role == AgentRole.WORKER:
            async with self._worker_semaphore:
                root_id = self._limits_registry.get_root_id(agent.agent_id)
                sibling_view = await self._sibling_view_port.build_view(
                    agent_id=agent.agent_id,
                    parent_id=agent.parent_id,
                    root_id=root_id,
                )

                await self._orchestrator.execute_task(
                    agent,
                    working_directory=self._workspace.working_directory,
                    workspace_context=self._workspace.get_context(),
                    sibling_view=sibling_view,
                )

        else:
            raise ValueError(f"Unknown agent role: {agent.role}")

    async def _persist_agent_events(
        self,
        agent: AgentSession,
        base_version: int,
    ) -> list[DomainEvent]:
        """Persist uncommitted events and return them.

        Delegates to repository for batch optimization and progress notification.
        """
        return await self._repository.persist_events(
            agent, base_version, self._progress_callback
        )

    async def _handle_post_step(
        self,
        agent: AgentSession,
        events: list[DomainEvent],
    ) -> None:
        """Handle post-step operations: child spawning, context updates, and parent notification."""
        # Spawn children (limits are enforced pre-spawn in AgentOrchestrator)
        child_events = [e for e in events if isinstance(e, ChildSpawned)]
        if child_events:
            await self._child_factory.create_children_from_events(
                child_events,
                agent.agent_id,
            )

        # Process context updates from completed workers (Event Sourcing)
        if agent.role == AgentRole.WORKER and agent.status == AgentStatus.COMPLETED:
            await self._process_worker_context_updates(agent)

        # Notify parent
        await self._notify_parent_if_complete(agent)

    async def _notify_parent_if_complete(self, agent: AgentSession) -> None:
        """Notify parent when child completes.

        Delegates to ParentNotificationService for the full workflow.
        """
        await self._parent_notifier.notify_if_complete(agent)

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

        # Store decisions via existing DecisionRecorded event
        for decision in parsed.decisions:
            context.record_decision(
                decision_key=decision.key,
                decision_value=decision.value,
                rationale=decision.rationale,
                decided_by=agent.agent_id,
            )

        # Store outputs via existing ArtifactStored event
        for output in parsed.outputs:
            context.store_artifact(
                key=output.key,
                content_type="text/plain",
                stored_by=agent.agent_id,
                content=output.description,
            )

        # Persist if any updates (Event Sourcing)
        if context.events:
            await self._shared_context_port.save(context, expected_version=current_version)
            context.mark_changes_as_committed()

    async def _handle_step_failure(self, agent_id: UUID, error: Exception) -> None:
        """Handle execution failure by persisting error state."""
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                return

            agent.fail_with_reason(f"Execution error: {error!r}")
            await self._repository.persist_events(
                agent, agent.version, self._progress_callback
            )
        except Exception as persist_error:
            raise RuntimeError(
                f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
            ) from error
