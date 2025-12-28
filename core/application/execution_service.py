"""Agent Execution Service - orchestrates agent lifecycle and execution.

This is the main orchestration service that coordinates:
- Agent step execution (load -> dispatch -> persist)
- Child agent spawning
- Parent notification on completion
- System loop for continuous execution
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.application.services.agent_repository import AgentNotFoundError, AgentRepository
from core.application.services.child_factory import ChildAgentFactory
from core.application.services.context_registry import ExecutionContextRegistry
from core.application.services.query_service import AgentQueryService
from core.application.services.workspace_context import WorkspaceContextProvider
from core.domain.events import ChildSpawned, DomainEvent
from core.domain.exceptions import ConcurrencyError
from core.domain.model import AgentRole, AgentSession, AgentStatus


if TYPE_CHECKING:
    from config import OrchestrationConfig
    from core.ports.event_store_port import EventStorePort
    from core.ports.shared_context_port import SharedContextPort

    SystemLimitsConfig = OrchestrationConfig.LimitsConfig


ProgressCallback = Callable[[DomainEvent, Any], None]


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for AgentExecutionService.

    Immutable configuration object following the Parameter Object pattern.
    """

    max_retries: int
    poll_interval: float
    output_directory: str
    default_worker_tool: str
    model_config: dict[str, str]


@dataclass(frozen=True)
class ExecutionServiceDependencies:
    """Collaborators for AgentExecutionService (Parameter Object pattern).

    Groups the injected dependencies to reduce constructor parameter count.
    All collaborators follow Dependency Inversion Principle (DIP).
    """

    repository: AgentRepository
    orchestrator: AgentOrchestrator
    context_registry: ExecutionContextRegistry
    child_factory: ChildAgentFactory
    query_service: AgentQueryService
    workspace: WorkspaceContextProvider
    shared_context_port: SharedContextPort | None = None


class AgentExecutionService:
    """Orchestrates agent workflow using composed services.

    This is a thin coordinator that delegates to specialized services:
    - AgentRepository: Load/persist agent state
    - ChildAgentFactory: Create child agents
    - AgentQueryService: Read operations (CQRS)
    - ExecutionContextRegistry: Manage execution contexts
    - WorkspaceContextProvider: Workspace file context
    - SharedContextPort: Shared execution context for budget and artifacts
    """

    def __init__(
        self,
        event_store: EventStorePort,
        dependencies: ExecutionServiceDependencies,
        config: ServiceConfig,
        system_limits: SystemLimitsConfig,
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

        # Configuration
        self._config = config
        self._system_limits = system_limits

        # Injected collaborators (unpacked from dependencies)
        self._repository = dependencies.repository
        self._orchestrator = dependencies.orchestrator
        self._context_registry = dependencies.context_registry
        self._child_factory = dependencies.child_factory
        self._query_service = dependencies.query_service
        self._workspace = dependencies.workspace

        # Worker concurrency control
        semaphore_limit = (
            system_limits.max_concurrent_workers
            if system_limits.is_workers_limited()
            else 10000  # Effectively unlimited
        )
        self._worker_semaphore = asyncio.Semaphore(semaphore_limit)

        # Progress callback
        self._progress_callback = progress_callback

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

    async def create_boss_agent(self, task_description: str) -> UUID:
        """Create root BOSS agent with task.

        Initializes execution context and shared context for the agent hierarchy.
        Returns the agent UUID.
        """
        root_id = uuid4()
        self._reset_for_new_run(root_id)

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
        if self._shared_context_port is None:
            return

        # TODO: Get initial budget from config when budget system is fully integrated
        initial_budget_usd = 0.0  # 0 = unlimited for now
        shared_context = await self._shared_context_port.get_or_create(
            root_id=root_id,
            initial_budget_usd=initial_budget_usd,
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
        """
        while True:
            active_agents = await self._query_service.get_active_agent_ids(
                root_id=root_agent_id
            )
            if not active_agents:
                break

            for agent_id in active_agents:
                try:
                    await self.run_agent_step(agent_id)
                except Exception as e:
                    print(f"Error executing agent {agent_id}: {e!r}")

            await asyncio.sleep(self._config.poll_interval)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        """Get agent execution result."""
        return await self._query_service.get_result(agent_id)

    async def get_system_statistics(self) -> SystemStatisticsDTO:
        """Get system-wide agent statistics."""
        return await self._query_service.get_statistics()

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _reset_for_new_run(self, root_id: UUID) -> None:
        """Reset all state for a new execution run."""
        self._workspace.reset()
        self._context_registry.reset()
        self._child_factory.reset(initial_count=1)  # Count the boss agent

        # Create root execution context
        self._context_registry.create_root(
            root_id=root_id,
            max_depth=self._system_limits.max_depth,
            max_children_per_node=self._system_limits.max_children_per_node,
            max_retries=self._config.max_retries,
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
        boss_model = self._config.model_config.get("boss", "gpt-4o-mini")
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_model,
                "temperature": 0.7,
                "max_tokens": 1000,
            },
            "tool": "claude_code",
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
        """Load agent and attach execution context."""
        agent = await self._repository.load(agent_id)

        context = self._context_registry.get(agent_id)
        if context is not None:
            agent.set_execution_context(context)

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
                await self._orchestrator.execute_task(
                    agent,
                    working_directory=self._workspace.working_directory,
                    workspace_context=self._workspace.get_context(),
                )

        else:
            raise ValueError(f"Unknown agent role: {agent.role}")

    async def _persist_agent_events(
        self,
        agent: AgentSession,
        base_version: int,
    ) -> list[DomainEvent]:
        """Persist uncommitted events and return them.

        Uses batch persistence for efficiency when there are multiple events.
        """
        uncommitted = list(agent.events)
        if not uncommitted:
            return uncommitted

        # Use batch append for multiple events (much faster for workers)
        if len(uncommitted) > 1:
            await self._event_store.append_batch(uncommitted, expected_version=base_version)
        else:
            await self._event_store.append(uncommitted[0], expected_version=base_version)

        # Notify progress for each event after successful persistence
        for event in uncommitted:
            self._notify_progress(event, agent)

        agent.mark_changes_as_committed()
        return uncommitted

    async def _handle_post_step(
        self,
        agent: AgentSession,
        events: list[DomainEvent],
    ) -> None:
        """Handle post-step operations: child spawning and parent notification."""
        # Spawn children
        child_events = [e for e in events if isinstance(e, ChildSpawned)]
        if child_events:
            await self._child_factory.create_children_from_events(
                child_events,
                agent.agent_id,
            )

        # Notify parent
        await self._notify_parent_if_complete(agent)

    async def _notify_parent_if_complete(self, agent: AgentSession) -> None:
        """Notify parent when child completes."""
        if agent.status != AgentStatus.COMPLETED or agent.parent_id is None:
            return

        try:
            parent = await self._repository.load(agent.parent_id)
        except AgentNotFoundError:
            raise ValueError(f"Parent agent {agent.parent_id} not found")

        parent_version = parent.version

        # Build structured child result
        child_result = agent.build_child_result()

        parent.handle_child_update(
            child_id=agent.agent_id,
            result=agent.result or "",
            child_result=child_result,
        )

        uncommitted = list(parent.events)
        if uncommitted:
            if len(uncommitted) > 1:
                await self._event_store.append_batch(uncommitted, expected_version=parent_version)
            else:
                await self._event_store.append(uncommitted[0], expected_version=parent_version)

            for event in uncommitted:
                self._notify_progress(event, parent)

        parent.mark_changes_as_committed()

    async def _handle_step_failure(self, agent_id: UUID, error: Exception) -> None:
        """Handle execution failure by persisting error state."""
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                return

            current_version = agent.version
            agent.fail_with_reason(f"Execution error: {error!r}")

            uncommitted = list(agent.events)
            if uncommitted:
                if len(uncommitted) > 1:
                    await self._event_store.append_batch(uncommitted, expected_version=current_version)
                else:
                    await self._event_store.append(uncommitted[0], expected_version=current_version)

        except Exception as persist_error:
            raise RuntimeError(
                f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
            ) from error

    def _notify_progress(self, event: DomainEvent, agent: AgentSession) -> None:
        """Notify progress callback if set."""
        if self._progress_callback is not None:
            try:
                self._progress_callback(event, agent)
            except Exception:
                pass
