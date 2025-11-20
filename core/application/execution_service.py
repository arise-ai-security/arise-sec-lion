"""Agent Execution Service - Application Layer orchestration.

This service coordinates the execution of agent steps by:
1. Loading aggregates from the event store
2. Dispatching to appropriate domain methods based on agent state
3. Persisting events with Optimistic Concurrency Control
4. Handling retries and errors
"""

from uuid import UUID

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

    Attributes:
        event_store: Port for loading/saving domain events.
        llm_port: Port for LLM interactions (complexity evaluation, task analysis).
        worker_tool_port: Port for worker tool execution (e.g., Claude Code CLI).
        max_retries: Maximum number of OCC retry attempts (default: 3).
    """

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        max_retries: int = 3,
    ) -> None:
        """Initialize the execution service with infrastructure ports.

        Args:
            event_store: Event store implementation for persistence.
            llm_port: LLM adapter for agent reasoning.
            worker_tool_port: Worker tool adapter for task execution.
            max_retries: Maximum OCC retry attempts (default: 3).
        """
        self.event_store = event_store
        self.llm_port = llm_port
        self.worker_tool_port = worker_tool_port
        self.max_retries = max_retries

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
                for event in agent.events:
                    await self.event_store.append(event, expected_version=current_version)
                    current_version += 1

                # Success - clear uncommitted changes
                agent.mark_changes_as_committed()
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
