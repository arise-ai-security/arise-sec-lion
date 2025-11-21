"""Agent Execution Service - Application Layer orchestration.

This service coordinates the execution of agent steps by:
1. Loading aggregates from the event store
2. Dispatching to appropriate domain methods based on agent state
3. Persisting events with Optimistic Concurrency Control
4. Handling retries and errors
5. Managing child agent lifecycle (creation, completion notifications)
6. Orchestrating the entire multi-agent system
"""

import asyncio
from uuid import UUID, uuid4

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events import ChildSpawned
from core.domain.exceptions import ConcurrencyError
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort


class AgentExecutionService:
    """Application service for executing agent workflow steps.

    This service implements the orchestration logic (The Brain) that:
    - Loads aggregate state from event store
    - Dispatches to domain methods based on agent role/status
    - Persists uncommitted events with OCC
    - Handles concurrency conflicts with retry logic
    - Manages child agent lifecycle (creation and parent notifications)
    - Orchestrates the entire multi-agent system loop

    Attributes:
        event_store: Port for loading/saving domain events.
        llm_port: Port for LLM interactions (complexity evaluation, task analysis).
        worker_tool_port: Port for worker tool execution (e.g., Claude Code CLI).
        model_config: Role-to-model mapping for agent configuration.
        max_retries: Maximum number of OCC retry attempts (default: 3).
        poll_interval: Interval in seconds for polling active agents (default: 0.5).
    """

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        model_config: dict[str, str] | None = None,
        max_retries: int = 3,
        poll_interval: float = 0.5,
    ) -> None:
        """Initialize the execution service with infrastructure ports.

        Args:
            event_store: Event store implementation for persistence.
            llm_port: LLM adapter for agent reasoning.
            worker_tool_port: Worker tool adapter for task execution.
            model_config: Role-to-model mapping (keys: "boss", "manager", "worker", "pending").
                         If None, uses default gpt-4o-mini for all roles.
            max_retries: Maximum OCC retry attempts (default: 3).
            poll_interval: Interval in seconds for polling active agents (default: 0.5).
        """
        self.event_store = event_store
        self.llm_port = llm_port
        self.worker_tool_port = worker_tool_port
        self.max_retries = max_retries
        self.poll_interval = poll_interval

        # Set default model_config if not provided
        if model_config is None:
            model_config = {
                "boss": "gpt-4o-mini",
                "manager": "gpt-4o-mini",
                "worker": "gpt-4o-mini",
                "pending": "gpt-4o-mini",
            }
        self.model_config = model_config

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one step of the agent workflow based on current state.

        This method implements the core orchestration logic:
        1. Load aggregate from event store
        2. Dispatch to appropriate domain method based on state
        3. Persist uncommitted events with OCC retry logic

        Dispatch Logic (The Brain):
        - If status is ANALYZING:
          - If role is PENDING: evaluate_complexity()
          - If role is BOSS/MANAGER: evaluate_task()
          - If role is WORKER: execute_task()

        Args:
            agent_id: UUID of the agent to execute.

        Raises:
            ConcurrencyError: If OCC fails after max retries.
            Exception: Other unexpected errors are caught and marked as failures.
        """
        retry_count = 0

        while retry_count < self.max_retries:
            try:
                # Step 1: Load aggregate from event store
                events = await self.event_store.get_events(agent_id)

                if not events:
                    raise ValueError(f"Agent {agent_id} not found in event store")

                agent = AgentSession.load_from_history(events)
                current_version = agent.version

                # Step 2: Dispatch to domain logic based on state
                await self._dispatch_agent_action(agent)

                # Step 3: Persist uncommitted events with OCC
                uncommitted_events = list(agent.events)  # Copy before clearing
                for event in uncommitted_events:
                    await self.event_store.append(event, expected_version=current_version)
                    current_version += 1

                # Success - clear uncommitted changes
                agent.mark_changes_as_committed()

                # Step 4: Handle child spawning (create child agent records)
                await self._handle_child_spawning(agent, uncommitted_events)

                # Step 5: Handle parent notification (if agent completed)
                await self._handle_parent_notification(agent)

                return

            except ConcurrencyError as e:
                # OCC conflict - retry
                retry_count += 1
                if retry_count >= self.max_retries:
                    # Max retries exceeded - give up
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e
                # Retry loop will reload fresh state

            except Exception as e:
                # Unexpected error - mark agent as failed
                try:
                    # Reload agent and mark as failed
                    events = await self.event_store.get_events(agent_id)
                    if events:
                        agent = AgentSession.load_from_history(events)
                        current_version = agent.version

                        # Call domain method to create failure event
                        agent.fail_with_reason(f"Execution error: {e!r}")

                        # Persist failure event
                        for event in agent.events:
                            await self.event_store.append(event, expected_version=current_version)
                            current_version += 1

                except Exception as persist_error:
                    # Could not even save the failure - log and re-raise original error
                    raise RuntimeError(
                        f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
                    ) from e

                # Re-raise original error after marking as failed
                raise

    async def _dispatch_agent_action(self, agent: AgentSession) -> None:
        """Dispatch to appropriate domain method based on agent state.

        This implements the state machine logic:
        - ANALYZING + PENDING -> evaluate_complexity()
        - ANALYZING + BOSS/MANAGER -> evaluate_task()
        - ANALYZING + WORKER -> execute_task()

        Args:
            agent: The agent aggregate to execute.
        """
        if agent.status != AgentStatus.ANALYZING:
            # Agent not in executable state
            return

        # Dispatch based on role
        if agent.role == AgentRole.PENDING:
            # Child agent needs to evaluate task complexity
            await agent.evaluate_complexity(self.llm_port)

        elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
            # Manager-type agent needs to decompose task into subtasks
            await agent.evaluate_task(self.llm_port)

        elif agent.role == AgentRole.WORKER:
            # Worker agent needs to execute task using tools
            await agent.execute_task(self.worker_tool_port)

        else:
            # Unknown role - should not happen
            raise ValueError(f"Unknown agent role: {agent.role}")

    async def _handle_child_spawning(self, parent: AgentSession, events: list) -> None:
        """Handle creation of child agents from ChildSpawned events.

        When a parent agent spawns children (via evaluate_task), this method:
        1. Creates a new AgentSession for each child
        2. Assigns the subtask to the child
        3. Persists the child's events to the event store

        Note: We do NOT recursively call run_agent_step here. The main loop
        will pick up these new agents and execute them.

        Args:
            parent: The parent agent that spawned children.
            events: List of events that were just persisted for the parent.
        """
        # Extract ChildSpawned events
        child_spawned_events = [e for e in events if isinstance(e, ChildSpawned)]

        for child_event in child_spawned_events:
            # Get child config from event (parent's decision)
            # Parent specifies models, hyperparameters, and tool for child
            child_config = child_event.child_config

            # Create new child agent
            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_config,  # Use parent's config decision
                parent_id=parent.session_id,
            )

            # Assign the subtask to the child
            child.assign_task(child_event.subtask.description)

            # Persist child's creation events
            for idx, child_evt in enumerate(child.events):
                await self.event_store.append(child_evt, expected_version=idx)

            # Clear uncommitted changes
            child.mark_changes_as_committed()

    async def _handle_parent_notification(self, agent: AgentSession) -> None:
        """Handle notifying parent when child agent completes.

        When a child agent reaches COMPLETED status, this method:
        1. Loads the parent agent
        2. Calls parent.handle_child_update() to record the result
        3. Persists the parent's new events

        Args:
            agent: The agent that may have completed and needs to notify its parent.
        """
        # Only notify parent if agent is completed and has a parent
        if agent.status != AgentStatus.COMPLETED or agent.parent_id is None:
            return

        # Load parent agent
        parent_events = await self.event_store.get_events(agent.parent_id)
        if not parent_events:
            # Parent not found - this shouldn't happen
            raise ValueError(f"Parent agent {agent.parent_id} not found in event store")

        parent = AgentSession.load_from_history(parent_events)
        parent_version = parent.version

        # Notify parent of child completion
        parent.handle_child_update(agent.session_id, agent.result or "")

        # Persist parent's new events
        for event in parent.events:
            await self.event_store.append(event, expected_version=parent_version)
            parent_version += 1

        # Clear uncommitted changes
        parent.mark_changes_as_committed()

    async def _get_active_agent_ids(self) -> list[UUID]:
        """Get list of agent IDs that are not in terminal state.

        An agent is "active" if its status is not COMPLETED or FAILED.
        This is used by the system loop to determine which agents need processing.

        Returns:
            List of UUIDs for agents in non-terminal states.
        """
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
        """Run the main orchestration loop for the multi-agent system.

        This method implements the core system loop that:
        1. Polls for active agents (not in terminal state)
        2. Executes one step for each active agent
        3. Repeats until all agents reach terminal state

        The loop continues until there are no more active agents, meaning
        the entire task hierarchy has been completed or failed.

        Args:
            root_agent_id: UUID of the root (BOSS) agent.

        Note:
            In a production system, this would be replaced by a job queue
            (e.g., Celery, Redis Queue, AWS SQS) for scalability and
            distributed processing. This is an MVP implementation for
            demonstration purposes.
        """
        while True:
            # Get list of active agents
            active_agents = await self._get_active_agent_ids()

            if not active_agents:
                # No more active agents - system is done
                break

            # Execute one step for each active agent
            for agent_id in active_agents:
                try:
                    await self.run_agent_step(agent_id)
                except Exception as e:
                    # Log error but continue with other agents
                    # In production, this would go to proper logging
                    print(f"Error executing agent {agent_id}: {e!r}")

            # Brief pause before next iteration
            await asyncio.sleep(self.poll_interval)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        """Query the final state of an agent.

        This is a query method for the presentation layer to retrieve
        agent state without directly accessing the event store.

        IMPORTANT: Returns a DTO (not domain aggregate) to prevent domain
        objects from leaking to presentation layer. This follows strict
        layered architecture where Presentation → Application (DTOs only).

        Args:
            agent_id: UUID of the agent to query.

        Returns:
            AgentResultDTO with agent status and result (no domain logic).

        Raises:
            ValueError: If agent not found.
        """
        events = await self.event_store.get_events(agent_id)

        if not events:
            raise ValueError(f"Agent {agent_id} not found")

        # Reconstruct aggregate from events (internal to application)
        agent = AgentSession.load_from_history(events)

        # Convert to DTO for presentation layer
        return AgentResultDTO(
            agent_id=str(agent.session_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_system_statistics(self) -> SystemStatisticsDTO:
        """Get statistics about the multi-agent system.

        This is a query method for the presentation layer to retrieve
        system-wide statistics without directly accessing the event store.

        IMPORTANT: Returns a DTO (not dict) for type safety and to prevent
        presentation from depending on internal data structures.

        Returns:
            SystemStatisticsDTO with aggregate counts.
        """
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
        """Initialize infrastructure dependencies.

        This method handles infrastructure lifecycle management (connection
        setup, schema initialization, etc.) and should be called during
        application startup.

        Note:
            This is an application-level operation that coordinates
            infrastructure initialization. In DDD, application services
            can manage infrastructure lifecycle as part of use cases.
        """
        await self.event_store.connect()
        await self.event_store.initialize_schema()

    async def cleanup(self) -> None:
        """Cleanup infrastructure dependencies.

        This method handles infrastructure lifecycle cleanup (closing
        connections, releasing resources, etc.) and should be called
        during application shutdown.
        """
        await self.event_store.disconnect()

    async def create_boss_agent(self, task_description: str) -> UUID:
        """Create and persist a new BOSS agent with a task.

        This is a command method that creates the root agent for the system.
        Used by the presentation layer to bootstrap a new agent hierarchy.

        Args:
            task_description: The task to assign to the BOSS agent.

        Returns:
            UUID of the created BOSS agent.

        Raises:
            Exception: If agent creation or persistence fails.
        """
        # Generate unique ID for root agent
        root_id = uuid4()

        # Get model for BOSS role from settings
        boss_model = self.model_config.get("boss", "gpt-4o-mini")

        # Create config for BOSS agent using heuristic strategy
        # BOSS is root agent, so application layer decides config (not parent)
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_model,
                "temperature": 0.7,
                "max_tokens": 1000,
            },
            "tool": "claude_code",  # Default tool for workers spawned by BOSS
        }

        # Create BOSS agent (top of hierarchy)
        boss_agent = AgentSession.create(
            session_id=root_id,
            role=AgentRole.BOSS,
            config=boss_config,
            parent_id=None,  # BOSS has no parent
        )

        # Assign the user's task to the BOSS
        boss_agent.assign_task(task_description)

        # Persist initial events to event store
        for idx, event in enumerate(boss_agent.events):
            await self.event_store.append(event, expected_version=idx)

        # Mark changes as committed
        boss_agent.mark_changes_as_committed()

        return root_id
