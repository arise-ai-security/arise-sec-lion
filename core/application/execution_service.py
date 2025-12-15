"""Agent Execution Service - orchestrates agent lifecycle and execution."""

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events import (
    ChildSpawned,
    DomainEvent,
    SubordinatesSpawned,
    VerifierSpawned,
)
from core.domain.exceptions import ConcurrencyError
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.prompt_builder import PromptBuilder
from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.reward_mechanism_port import RewardMechanismPort
from core.ports.verification_heuristics_port import VerificationHeuristicsPort
from core.ports.worker_port import WorkerToolPort


# Default models for multi-model strategy (3 different models)
DEFAULT_SUBORDINATE_MODELS = [
    "claude-sonnet-4-5-20250514",  # Anthropic
    "gemini-1.5-pro",  # Google
    "gpt-4o",  # OpenAI
]


ProgressCallback = Callable[[DomainEvent, Any], None]


class AgentExecutionService:
    """Application service for executing agent workflow steps.

    This service implements the orchestration logic (The Brain) that:
    - Loads aggregate state from event store
    - Dispatches to domain methods based on agent role/status
    - Persists uncommitted events with OCC
    - Handles concurrency conflicts with retry logic
    - Manages child agent lifecycle (creation and parent notifications)
    - Orchestrates the entire multi-agent system loop
    - Implements Task Assignment Workflow with verification and retry

    Task Assignment Workflow:
    1. Pop first task from queue
    2. Spawn 3 subordinates with different models (diversity for success)
    3. Wait for first success (terminate others) or all failures
    4. On success: trigger verification heuristics, recollect budget
    5. On failure: decide to retry (re-insert revised task) or end lifecycle
    6. If verification injected: add verification task to head of queue

    Attributes:
        event_store: Port for loading/saving domain events.
        llm_port: Port for LLM interactions (complexity evaluation, task analysis).
        worker_tool_port: Port for worker tool execution (e.g., Claude Code CLI).
        prompt_builder: Service for building hierarchical prompts.
        reward_mechanism: Port for budget recollection ratio calculations.
        verification_heuristics: Port for verification trigger decisions.
        model_config: Role-to-model mapping for agent configuration.
        subordinate_models: List of models to use for parallel subordinates.
        max_retries: Maximum number of OCC retry attempts (default: 3).
        poll_interval: Interval in seconds for polling active agents (default: 0.5).
    """

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        prompt_builder: PromptBuilder | None = None,
        reward_mechanism: RewardMechanismPort | None = None,
        verification_heuristics: VerificationHeuristicsPort | None = None,
        model_config: dict[str, str] | None = None,
        subordinate_models: list[str] | None = None,
        max_retries: int = 3,
        poll_interval: float = 0.5,
        output_directory: str | None = None,
        progress_callback: ProgressCallback | None = None,
        default_worker_tool: str = "claude_code",
    ) -> None:
        """Initialize the execution service with infrastructure ports.

        Args:
            event_store: Event store implementation for persistence.
            llm_port: LLM adapter for agent reasoning.
            worker_tool_port: Worker tool adapter for task execution.
            prompt_builder: Prompt builder service. If None, creates default instance.
            model_config: Role-to-model mapping (keys: "boss", "manager", "worker", "pending").
                         If None, uses default gpt-4o-mini for all roles.
            max_retries: Maximum OCC retry attempts (default: 3).
            poll_interval: Interval in seconds for polling active agents (default: 0.5).
        """
        self.event_store = event_store
        self.llm_port = llm_port
        self.worker_tool_port = worker_tool_port
        self.prompt_builder = prompt_builder or PromptBuilder(default_tool=default_worker_tool)
        self.reward_mechanism = reward_mechanism
        self.verification_heuristics = verification_heuristics
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.output_directory = output_directory
        self.working_directory: str | None = None
        self.progress_callback = progress_callback
        self._workspace_context_cache: str | None = None
        self._workspace_context_scanned: bool = False

        # Set subordinate models for multi-model strategy
        self.subordinate_models = subordinate_models or DEFAULT_SUBORDINATE_MODELS

        if model_config is None:
            model_config = {
                "boss": "gpt-4o-mini",
                "manager": "gpt-4o-mini",
                "worker": "gpt-4o-mini",
                "pending": "gpt-4o-mini",
            }
        self.model_config = model_config

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
        """Dispatch based on role: PENDING→complexity, BOSS/MANAGER→decompose, WORKER→execute."""
        if agent.status != AgentStatus.ANALYZING:
            return

        if agent.role == AgentRole.PENDING:
            await agent.evaluate_complexity(self.llm_port, self.prompt_builder)

        elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
            await agent.evaluate_task(self.llm_port, self.prompt_builder)

        elif agent.role == AgentRole.WORKER:
            workspace_context = self._get_workspace_context()
            await agent.execute_task(
                self.worker_tool_port,
                working_directory=self.working_directory,
                workspace_context=workspace_context,
            )

        else:
            raise ValueError(f"Unknown agent role: {agent.role}")

    async def _handle_child_spawning(self, parent: AgentSession, events: list) -> None:
        """Create child AgentSessions from ChildSpawned events."""
        child_spawned_events = [e for e in events if isinstance(e, ChildSpawned)]

        for child_event in child_spawned_events:
            child_config = child_event.child_config
            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_config,
                parent_id=parent.session_id,
            )
            child.assign_task(child_event.subtask.description)

            for idx, child_evt in enumerate(child.events):
                await self.event_store.append(child_evt, expected_version=idx)
                self._notify_progress(child_evt, child)

            child.mark_changes_as_committed()

        # Handle SubordinatesSpawned events (parallel subordinates for same subtask)
        subordinate_events = [e for e in events if isinstance(e, SubordinatesSpawned)]

        for sub_event in subordinate_events:
            for config in sub_event.subordinate_configs:
                if "child_id" not in config:
                    continue

                child_id = config["child_id"]
                if isinstance(child_id, str):
                    child_id = UUID(child_id)

                child_config = config.get("config", {})
                child_role = config.get("role", AgentRole.PENDING.value)

                # Create new subordinate agent
                child = AgentSession.create(
                    session_id=child_id,
                    role=AgentRole(child_role),
                    config=child_config,
                    parent_id=parent.session_id,
                )

                # Assign the subtask to the child
                child.assign_task(sub_event.subtask.description)

                # Allocate budget if specified
                if "budget" in config:
                    child.allocate_budget(config["budget"], source="parent")

                # Persist child's creation events
                for idx, child_evt in enumerate(child.events):
                    await self.event_store.append(child_evt, expected_version=idx)

                child.mark_changes_as_committed()

        # Handle VerifierSpawned events
        verifier_events = [e for e in events if isinstance(e, VerifierSpawned)]

        for verifier_event in verifier_events:
            # Create new verifier agent
            verifier = AgentSession.create(
                session_id=verifier_event.verifier_id,
                role=AgentRole.PENDING,  # Verifier starts as PENDING
                config=verifier_event.verifier_config,
                parent_id=parent.session_id,
            )

            # Assign verification task
            verification_task = (
                f"Verify the following completed task: {verifier_event.target_subtask.description}"
            )
            verifier.assign_task(verification_task)

            # Persist verifier's creation events
            for idx, v_evt in enumerate(verifier.events):
                await self.event_store.append(v_evt, expected_version=idx)

            verifier.mark_changes_as_committed()

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
        """Create root BOSS agent with task. Returns agent UUID."""
        root_id = uuid4()

        self._workspace_context_cache = None
        self._workspace_context_scanned = False

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
