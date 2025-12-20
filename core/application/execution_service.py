"""Agent Execution Service - orchestrates agent lifecycle and execution."""

import asyncio
import contextlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


@dataclass
class BudgetConfig:
    """Configuration for cost budget tracking."""

    max_total_cost_usd: float = 100.0
    cost_warning_threshold: float = 0.8
    cost_tracking_enabled: bool = True

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
from core.query.projections.hierarchy_collector import HierarchyCollector


# Default models for multi-model strategy (3 different models)
DEFAULT_SUBORDINATE_MODELS = [
    "claude-sonnet-4-5-20250514",  # Anthropic
    "gemini-1.5-pro",  # Google
    "gpt-4o",  # OpenAI
]


ProgressCallback = Callable[[DomainEvent, Any], None]
StatusCallback = Callable[[list[dict[str, Any]]], None]


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
        status_callback: StatusCallback | None = None,
        default_worker_tool: str = "claude_code",
        budget_config: BudgetConfig | None = None,
        system_limits: Any | None = None,
        worker_shortcut_probability: float = 0.3,
        budget_threshold_ratio: float = 0.02,
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
        self.status_callback = status_callback
        self.budget_config = budget_config or BudgetConfig()
        self.system_limits = system_limits
        self.worker_shortcut_probability = worker_shortcut_probability
        self.budget_threshold_ratio = budget_threshold_ratio
        self.initial_boss_budget: float = 0.0
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
            # Shortcut to worker if: budget below threshold OR random chance
            budget_threshold = self.initial_boss_budget * self.budget_threshold_ratio
            if agent.current_budget < budget_threshold:
                pct = self.budget_threshold_ratio * 100
                agent.shortcut_to_worker(f"budget below {pct:.0f}% of initial allocation")
            elif random.random() < self.worker_shortcut_probability:
                agent.shortcut_to_worker("randomly selected to skip complexity evaluation")
            else:
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

        # Calculate weighted budget allocation based on subtask complexity/importance/time
        total_weight = sum(e.subtask.budget_weight for e in child_spawned_events)
        available_budget = parent.current_budget

        for child_event in child_spawned_events:
            child_config = child_event.child_config
            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_config,
                parent_id=parent.session_id,
            )
            child.assign_task(child_event.subtask.description)

            # Allocate budget proportionally based on subtask weight
            if total_weight > 0 and available_budget > 0:
                weight_ratio = child_event.subtask.budget_weight / total_weight
                child_budget = available_budget * weight_ratio
                child.allocate_budget(child_budget, source="parent")

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
        """Notify parent when child reaches terminal state (COMPLETED or FAILED)."""
        if agent.parent_id is None:
            return
        if agent.status not in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            return

        parent_events = await self.event_store.get_events(agent.parent_id)
        if not parent_events:
            raise ValueError(f"Parent agent {agent.parent_id} not found in event store")

        parent = AgentSession.load_from_history(parent_events)
        parent_version = parent.version

        if agent.status == AgentStatus.COMPLETED:
            parent.handle_child_update(agent.session_id, agent.result or "")
        else:
            parent.handle_child_failure(
                child_id=agent.session_id,
                failure_reason=agent.error_message or "Unknown failure",
                budget_at_failure=agent.current_budget,
            )

        for event in parent.events:
            await self.event_store.append(event, expected_version=parent_version)
            parent_version += 1
            self._notify_progress(event, parent)

        parent.mark_changes_as_committed()

    async def _get_active_agent_ids(self, root_id: UUID | None = None) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED).

        Args:
            root_id: If provided, only return agents within this hierarchy.
                    If None, returns all active agents (legacy behavior).
        """
        if root_id is not None:
            # Get only agents in this hierarchy
            collector = HierarchyCollector(self.event_store)
            all_agent_ids = await collector.collect_agent_ids(root_id)
        else:
            # Legacy: get all agents
            all_agent_ids = set(await self.event_store.get_all_aggregate_ids())

        active_ids = []
        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                if not agent.is_terminal():
                    active_ids.append(agent_id)
        return active_ids

    async def _get_agents_status(self, root_id: UUID | None = None) -> list[dict[str, Any]]:
        """Return status information for agents.

        Args:
            root_id: If provided, only return agents within this hierarchy.
                    If None, returns all agents (legacy behavior).
        """
        if root_id is not None:
            collector = HierarchyCollector(self.event_store)
            all_agent_ids = await collector.collect_agent_ids(root_id)
        else:
            all_agent_ids = set(await self.event_store.get_all_aggregate_ids())

        agents_status = []
        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                agents_status.append({
                    "agent_id": str(agent_id)[:8],
                    "role": agent.role.value,
                    "status": agent.status.value,
                    "budget": agent.current_budget,
                    "task": (agent.task_description[:40] + "...")
                    if agent.task_description and len(agent.task_description) > 40
                    else agent.task_description,
                    "queue_size": len(agent.task_queue),
                })
        return agents_status

    def _notify_status(self, agents_status: list[dict[str, Any]]) -> None:
        """Notify status callback if configured."""
        if self.status_callback is not None:
            with contextlib.suppress(Exception):
                self.status_callback(agents_status)

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents in the hierarchy until all reach terminal state.

        Only processes agents within the hierarchy rooted at root_agent_id.
        This ensures that running a new task doesn't process agents from previous runs.
        """
        while True:
            # Only get active agents within this hierarchy
            active_agents = await self._get_active_agent_ids(root_id=root_agent_id)
            if not active_agents:
                break

            # Notify status callback with current agents status (filtered to hierarchy)
            if self.status_callback:
                agents_status = await self._get_agents_status(root_id=root_agent_id)
                self._notify_status(agents_status)

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

    async def get_system_statistics(self, root_agent_id: UUID) -> SystemStatisticsDTO:
        """Get statistics for agents in a specific run hierarchy.

        Args:
            root_agent_id: The root BOSS agent ID to scope statistics to.

        Returns:
            Statistics for only the agents in this run's hierarchy.
        """
        collector = HierarchyCollector(self.event_store)
        hierarchy_agent_ids = await collector.collect_agent_ids(root_agent_id)

        completed = 0
        failed = 0
        active = 0

        for agent_id in hierarchy_agent_ids:
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
            total_agents=len(hierarchy_agent_ids),
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

        # Allocate initial budget to BOSS agent (default: 1000.0)
        initial_budget = 1000.0
        self.initial_boss_budget = initial_budget
        boss_agent.allocate_budget(initial_budget, source="initial")

        for idx, event in enumerate(boss_agent.events):
            await self.event_store.append(event, expected_version=idx)
            self._notify_progress(event, boss_agent)

        boss_agent.mark_changes_as_committed()
        return root_id
