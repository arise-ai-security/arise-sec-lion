"""Agent Execution Service - orchestrates agent lifecycle and execution."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events import ChildSpawned, DomainEvent


if TYPE_CHECKING:
    from config import OrchestrationConfig
    from core.ports.event_store_port import EventStorePort
    from core.ports.llm_port import LLMPort
    from core.ports.worker_port import WorkerToolPort

    SystemLimitsConfig = OrchestrationConfig.LimitsConfig

from core.domain.exceptions import ConcurrencyError
from core.domain.execution_context import ExecutionContext
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.prompt_builder import PromptBuilder


ProgressCallback = Callable[[DomainEvent, Any], None]


class AgentExecutionService:
    """Orchestrates agent workflow: load → dispatch → persist → propagate.

    Supports system limits and agent hierarchy management.
    Note: Budget tracking will be added via SharedExecutionContext (context-passing feature).
    """

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        system_limits: SystemLimitsConfig,
        model_config: dict[str, str],
        max_retries: int,
        poll_interval: float,
        output_directory: str,
        default_worker_tool: str,
        prompt_builder: PromptBuilder | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.event_store = event_store
        self.llm_port = llm_port
        self.worker_tool_port = worker_tool_port
        self.prompt_builder = prompt_builder or PromptBuilder(default_tool=default_worker_tool)
        self.model_config = model_config
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.output_directory = output_directory
        self.working_directory: str | None = None
        self.progress_callback = progress_callback
        self._workspace_context_cache: str | None = None
        self._workspace_context_scanned: bool = False

        # System limits (required, -1 means unlimited)
        self.system_limits = system_limits
        # Semaphore: use large number for "unlimited" when max_concurrent_workers is -1
        semaphore_limit = (
            system_limits.max_concurrent_workers
            if system_limits.is_workers_limited()
            else 10000  # Effectively unlimited
        )
        self._worker_semaphore = asyncio.Semaphore(semaphore_limit)
        self._total_agents_created: int = 0

        # Execution context tracking (maps agent_id -> context)
        self._execution_contexts: dict[UUID, ExecutionContext] = {}

    def _notify_progress(self, event: DomainEvent, agent: AgentSession) -> None:
        if self.progress_callback is not None:
            with contextlib.suppress(Exception):
                self.progress_callback(event, agent)

    def _get_workspace_context(self) -> str | None:
        """Return cached file listing for workspace. Enables workers to see each other's files."""
        if self._workspace_context_scanned:
            return self._workspace_context_cache

        self._workspace_context_scanned = True

        if not self.working_directory:
            return None

        workspace_path = Path(self.working_directory)
        if not workspace_path.exists():
            return None

        try:
            files = []
            for item in workspace_path.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                if item.is_file():
                    rel_path = item.relative_to(workspace_path)
                    files.append(str(rel_path))

            if not files:
                return None

            files.sort()
            self._workspace_context_cache = "\n".join(f"- {f}" for f in files)
            return self._workspace_context_cache

        except OSError:
            return None

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one workflow step: load → dispatch → persist with OCC retry."""
        retry_count = 0

        while retry_count < self.max_retries:
            try:
                events = await self.event_store.get_events(agent_id)
                if not events:
                    raise ValueError(f"Agent {agent_id} not found in event store")

                agent = AgentSession.load_from_history(events)
                current_version = agent.version

                # Set execution context on agent for limit enforcement
                if agent_id in self._execution_contexts:
                    agent.set_execution_context(self._execution_contexts[agent_id])

                await self._dispatch_agent_action(agent)

                uncommitted_events = list(agent.events)
                for event in uncommitted_events:
                    await self.event_store.append(event, expected_version=current_version)
                    current_version += 1
                    self._notify_progress(event, agent)

                agent.mark_changes_as_committed()

                await self._handle_child_spawning(agent, uncommitted_events)
                await self._handle_parent_notification(agent)
                return

            except ConcurrencyError as e:
                retry_count += 1
                if retry_count >= self.max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e

            except Exception as e:
                try:
                    events = await self.event_store.get_events(agent_id)
                    if events:
                        agent = AgentSession.load_from_history(events)
                        current_version = agent.version
                        agent.fail_with_reason(f"Execution error: {e!r}")

                        for event in agent.events:
                            await self.event_store.append(event, expected_version=current_version)
                            current_version += 1

                except Exception as persist_error:
                    raise RuntimeError(
                        f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
                    ) from e
                raise

    async def _dispatch_agent_action(self, agent: AgentSession) -> None:
        """Dispatch based on role: PENDING→complexity, BOSS/MANAGER→decompose, WORKER→execute.

        Worker execution is rate-limited by max_concurrent_workers semaphore.
        """
        if agent.status != AgentStatus.ANALYZING:
            return

        if agent.role == AgentRole.PENDING:
            await agent.evaluate_complexity(self.llm_port, self.prompt_builder)

        elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
            await agent.evaluate_task(self.llm_port, self.prompt_builder)

        elif agent.role == AgentRole.WORKER:
            # Use semaphore to limit concurrent worker executions
            async with self._worker_semaphore:
                workspace_context = self._get_workspace_context()
                await agent.execute_task(
                    self.worker_tool_port,
                    working_directory=self.working_directory,
                    workspace_context=workspace_context,
                )

        else:
            raise ValueError(f"Unknown agent role: {agent.role}")

    async def _handle_child_spawning(self, parent: AgentSession, events: list) -> None:
        """Create child AgentSessions from ChildSpawned events.

        Propagates execution context to children with incremented depth.
        Enforces max_total_agents limit.
        """
        child_spawned_events = [e for e in events if isinstance(e, ChildSpawned)]

        # Get parent's execution context for propagation
        parent_context = self._execution_contexts.get(parent.session_id)

        for child_event in child_spawned_events:
            # Check max_total_agents limit (if enabled)
            if (
                self.system_limits.is_agents_limited()
                and self._total_agents_created >= self.system_limits.max_total_agents
            ):
                # Skip creating this child - limit exceeded
                # The parent will be notified of failure when children don't complete
                continue

            child_config = child_event.child_config
            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_config,
                parent_id=parent.session_id,
            )
            child.assign_task(child_event.subtask.description)

            # Propagate execution context to child (with incremented depth)
            if parent_context is not None:
                child_context = parent_context.for_child()
                self._execution_contexts[child_event.child_id] = child_context

            for idx, child_evt in enumerate(child.events):
                await self.event_store.append(child_evt, expected_version=idx)
                self._notify_progress(child_evt, child)

            child.mark_changes_as_committed()
            self._total_agents_created += 1

    async def _handle_parent_notification(self, agent: AgentSession) -> None:
        """Notify parent when child completes, triggering aggregation if all done."""
        if agent.status != AgentStatus.COMPLETED or agent.parent_id is None:
            return

        parent_events = await self.event_store.get_events(agent.parent_id)
        if not parent_events:
            raise ValueError(f"Parent agent {agent.parent_id} not found in event store")

        parent = AgentSession.load_from_history(parent_events)
        parent_version = parent.version

        parent.handle_child_update(agent.session_id, agent.result or "")

        for event in parent.events:
            await self.event_store.append(event, expected_version=parent_version)
            parent_version += 1
            self._notify_progress(event, parent)

        parent.mark_changes_as_committed()

    async def _get_active_agent_ids(self) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED)."""
        all_agent_ids = await self.event_store.get_all_aggregate_ids()
        active_ids = []
        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                if not agent.is_terminal():
                    active_ids.append(agent_id)
        return active_ids

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents until all reach terminal state."""
        while True:
            active_agents = await self._get_active_agent_ids()
            if not active_agents:
                break

            for agent_id in active_agents:
                try:
                    await self.run_agent_step(agent_id)
                except Exception as e:
                    print(f"Error executing agent {agent_id}: {e!r}")

            await asyncio.sleep(self.poll_interval)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        events = await self.event_store.get_events(agent_id)
        if not events:
            raise ValueError(f"Agent {agent_id} not found")

        agent = AgentSession.load_from_history(events)
        return AgentResultDTO(
            agent_id=str(agent.session_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_system_statistics(self) -> SystemStatisticsDTO:
        all_agent_ids = await self.event_store.get_all_aggregate_ids()

        completed = 0
        failed = 0
        active = 0

        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                if agent.status == AgentStatus.COMPLETED:
                    completed += 1
                elif agent.status == AgentStatus.FAILED:
                    failed += 1
                else:
                    active += 1

        return SystemStatisticsDTO(
            total_agents=len(all_agent_ids),
            completed=completed,
            failed=failed,
            active=active,
        )

    async def initialize(self) -> None:
        await self.event_store.connect()
        await self.event_store.initialize_schema()

    async def cleanup(self) -> None:
        await self.event_store.disconnect()

    async def create_boss_agent(self, task_description: str) -> UUID:
        """Create root BOSS agent with task. Returns agent UUID.

        Initializes execution context for the agent hierarchy with system limits.
        """
        root_id = uuid4()

        # Reset state for new run
        self._workspace_context_cache = None
        self._workspace_context_scanned = False
        self._total_agents_created = 1  # Count the boss agent
        self._execution_contexts.clear()

        # Create root execution context from system limits
        root_context = ExecutionContext.create_root(
            max_depth=self.system_limits.max_depth,
            max_children_per_node=self.system_limits.max_children_per_node,
            max_retries=self.max_retries,
        )
        self._execution_contexts[root_id] = root_context

        # Use .resolve() for absolute path - worker tools may run in sandboxed environments
        if self.output_directory:
            base_output = Path(self.output_directory).resolve()
            base_output.mkdir(parents=True, exist_ok=True)
            run_output_path = base_output / str(root_id)
            run_output_path.mkdir(parents=True, exist_ok=True)
            self.working_directory = str(run_output_path)

        boss_model = self.model_config.get("boss", "gpt-4o-mini")
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
            session_id=root_id,
            role=AgentRole.BOSS,
            config=boss_config,
            parent_id=None,
        )
        boss_agent.assign_task(task_description)

        for idx, event in enumerate(boss_agent.events):
            await self.event_store.append(event, expected_version=idx)
            self._notify_progress(event, boss_agent)

        boss_agent.mark_changes_as_committed()
        return root_id
