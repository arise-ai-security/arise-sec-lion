"""Core domain models for the multi-agent system.

This module contains the pure business logic and data structures for agent
sessions. It has NO external dependencies (except Pydantic) per Hexagonal
Architecture constraints.
"""

from enum import Enum
from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from core.domain.agent_config import AgentConfig
from core.domain.config_resolver import ConfigResolver
from core.domain.events import (
    AgentCreated,
    AgentTerminated,
    AllChildrenFailed,
    AllSubordinatesFailed,
    BudgetAdjusted,
    BudgetAllocated,
    BudgetRecollected,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    FirstSuccessRecorded,
    StatusChanged,
    SubordinatesSpawned,
    SubtaskRetried,
    SubtasksDefined,
    SubtreeAborted,
    TaskAssigned,
    TaskDequeued,
    TaskEnqueued,
    TaskReinjected,
    TerminationReason,
    ThoughtCaptured,
    VerificationCompleted,
    VerificationHeuristicEvaluated,
    VerificationInjected,
    VerifierSpawned,
    WorkCompleted,
    WorkFailed,
)
from core.domain.prompt_builder import PromptBuilder
from core.domain.services import SubtaskParser
from core.domain.subtask import Subtask
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort


class AgentRole(str, Enum):
    """The role of an agent in the hierarchical system.

    Dynamic Role Determination:
        - BOSS is the root agent (only ONE exists in the system)
        - When an agent (BOSS or MANAGER) spawns children:
          1. Each child is created with role=PENDING
          2. Each child evaluates task complexity via LLM
          3. If SIMPLE → child role becomes WORKER (executes task)
          4. If COMPLEX → child role becomes MANAGER (decomposes further)
        - This enables recursive decomposition of arbitrary depth
        - Children can NEVER be BOSS role (enforced by ChildSpawned event handler)

    Attributes:
        BOSS: Root agent that initiates task decomposition.
        PENDING: Child agent awaiting complexity evaluation.
        MANAGER: Agent that decomposes complex tasks and spawns children.
        WORKER: Leaf agent that executes simple tasks using tools.
    """

    BOSS = "boss"
    PENDING = "pending"
    MANAGER = "manager"
    WORKER = "worker"


class AgentStatus(str, Enum):
    """Current execution status of an agent session.

    Attributes:
        PENDING: Agent has been created but not yet started.
        ANALYZING: Agent is analyzing the task (planning, decomposing).
        IN_PROGRESS: Agent is actively working on its task.
        WAITING: Agent is waiting for child agents to complete.
        COMPLETED: Agent has successfully completed its task.
        FAILED: Agent encountered an error and cannot proceed.
        BLOCKED: Agent is blocked waiting for external input or resolution.
        TERMINATED: Agent has been terminated (budget depleted, aborted, etc.).
        VERIFYING: Agent is performing or awaiting verification of completed work.
    """

    PENDING = "pending"
    ANALYZING = "analyzing"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TERMINATED = "terminated"
    VERIFYING = "verifying"


class AgentSession:
    """Event-sourced aggregate representing an agent session.

    AgentSession is the core aggregate in our event-sourced system. Its state
    is derived by replaying domain events. All state changes occur by applying
    events, never by direct mutation.

    Event Sourcing Pattern:
        - All state changes create events (stored in self._changes)
        - Events are applied via _apply() method
        - State can be reconstructed by replaying events via load_from_history()

    Agent Node Structure:
        - Agent Model: Configuration containing LLM model name and parameters
        - Current Budget: Numeric resource allocation for task execution
        - Objective: Task description assigned to this agent
        - Task Queue: FIFO queue of subtasks to be processed
        - Supervisor Node: Reference to parent agent (parent_id)
        - Subordinate Nodes: List of child agents (child_ids)

    Attributes:
        session_id: Unique identifier for this agent session.
        role: The hierarchical role (BOSS, MANAGER, or WORKER).
        status: Current execution status of the agent.
        parent_id: ID of the parent agent session (None for BOSS).
        child_ids: List of child agent session IDs spawned by this agent.
        task_description: The task assigned to this agent.
        result: The final result/output when status is COMPLETED.
        error_message: Error details when status is FAILED.
        config: Configuration for this agent (LLM model and parameters).
        current_budget: Numeric resource allocation for this agent.
        task_queue: FIFO queue of Subtask objects to be processed.
        version: Event stream version (for Optimistic Concurrency Control).
    """

    def __init__(self, session_id: UUID) -> None:
        """Initialize instance attributes (internal use only).

        Note: Do not call directly. Use AgentSession.create() or
        AgentSession.load_from_history() instead.

        Args:
            session_id: Unique identifier for this agent session.
        """
        self._initialize_defaults(session_id)

    @classmethod
    def create(
        cls,
        session_id: UUID,
        role: AgentRole,
        config: dict[str, Any],
        parent_id: UUID | None = None,
    ) -> "AgentSession":
        """Factory method to create a new AgentSession.

        This is the primary way to create new agent aggregates. It initializes
        the agent and emits an AgentCreated event.

        Args:
            session_id: Unique identifier for this agent session.
            role: The hierarchical role (BOSS, MANAGER, or WORKER).
            config: Configuration dict for this agent.
            parent_id: ID of the parent agent (None for BOSS).

        Returns:
            A new AgentSession with one AgentCreated event.
        """
        # Create instance and initialize defaults
        instance = cls(session_id)

        # Create the AgentCreated event
        event = AgentCreated(
            aggregate_id=session_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
        )

        # Apply the event and track it
        instance._apply(event)
        instance._changes.append(event)

        return instance

    @property
    def events(self) -> list[DomainEvent]:
        """Get the list of uncommitted events (changes).

        Returns:
            List of domain events that have been created but not yet persisted.
        """
        return self._changes

    def mark_changes_as_committed(self) -> None:
        """Clear the uncommitted changes after successful persistence.

        This method is called by the application layer after successfully
        persisting all uncommitted events to the event store. It clears
        the _changes list to prevent duplicate event persistence.

        Note: Only call this after ensuring all events in _changes have
        been successfully written to the event store.
        """
        self._changes.clear()

    def assign_task(self, task_description: str) -> None:
        """Assign a task to this agent.

        This creates a TaskAssigned event and transitions the agent to ANALYZING status.

        Args:
            task_description: The task to be executed by this agent.
        """
        # Create TaskAssigned event (status transition handled in _apply)
        task_event = TaskAssigned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
        )
        self._apply(task_event)
        self._changes.append(task_event)

    async def evaluate_complexity(self, llm_port: LLMPort, prompt_builder: PromptBuilder) -> None:
        """Evaluate task complexity and determine agent role.

        Child agents (spawned with role=PENDING) use this method to determine
        whether their task is SIMPLE (can be executed directly) or COMPLEX
        (requires further decomposition).

        Based on the evaluation:
            - SIMPLE → role becomes WORKER
            - COMPLEX → role becomes MANAGER

        Preconditions:
            - Agent must be in PENDING role
            - Agent must be in ANALYZING status
            - Task must be assigned

        Args:
            llm_port: LLM port to use for complexity evaluation.
            prompt_builder: PromptBuilder service for hierarchical prompt construction.
        """
        # Preconditions: Fail fast if called incorrectly
        assert self.role == AgentRole.PENDING, (
            f"evaluate_complexity requires PENDING role, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, (
            f"evaluate_complexity requires ANALYZING status, got {self.status}"
        )
        assert self.task_description, "evaluate_complexity requires assigned task"

        # Build hierarchical prompt (System + Strategy + Task + Output Format)
        prompt = prompt_builder.build_complexity_evaluation_prompt(
            task_description=self.task_description,
            agent_id=self.session_id,
            parent_task=None,  # TODO: Pass parent context if available
        )

        # Resolve operation-specific config using strategy-agnostic resolver
        llm_config = ConfigResolver.resolve(self.config, operation="complexity_evaluation")

        # Query LLM with resolved config
        response = await llm_port.query(prompt, llm_config.model_dump())

        # Parse response to determine complexity
        try:
            import json

            data = json.loads(response)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            # Validate complexity value
            if complexity not in ("simple", "complex"):
                raise ValueError(f"Invalid complexity value: {complexity}")

            # Determine role based on complexity
            determined_role = AgentRole.WORKER if complexity == "simple" else AgentRole.MANAGER

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            # LLM response violated expectations - record as business fact
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"Failed to evaluate complexity: {e}",
            )
            self._apply(failed_event)
            self._changes.append(failed_event)
            return

        # Create ComplexityEvaluated event
        complexity_event = ComplexityEvaluated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            complexity=complexity,
            determined_role=determined_role.value,
            reasoning=reasoning,
        )
        self._apply(complexity_event)
        self._changes.append(complexity_event)

    async def evaluate_task(self, llm_port: LLMPort, prompt_builder: PromptBuilder) -> None:
        """Evaluate task and decompose into subtasks.

        Uses an LLM to analyze the task and break it down into subtasks.
        For each subtask, the LLM also determines complexity (SIMPLE or COMPLEX)
        to decide whether to spawn a WORKER or MANAGER child.

        System Behavior (Recursive Decomposition):
            1. Parent (BOSS or MANAGER) decomposes task into subtasks
            2. For each subtask, LLM evaluates complexity
            3. If SIMPLE → spawn WORKER child (executes task)
            4. If COMPLEX → spawn MANAGER child (decomposes further)
            5. This enables arbitrary-depth recursive decomposition

        Preconditions:
            - Agent must be BOSS or MANAGER role
            - Agent must be in ANALYZING status

        Args:
            llm_port: LLM port to use for task decomposition.
            prompt_builder: PromptBuilder service for hierarchical prompt construction.
        """
        # Preconditions: Fail fast if called on wrong agent type/status
        assert self.role in (AgentRole.BOSS, AgentRole.MANAGER), (
            f"evaluate_task requires BOSS or MANAGER agent, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, (
            f"evaluate_task requires ANALYZING status, got {self.status}"
        )

        # Build hierarchical prompt based on role (BOSS or MANAGER)
        if self.role == AgentRole.BOSS:
            prompt = prompt_builder.build_boss_delegation_prompt(
                task_description=self.task_description,
                agent_id=self.session_id,
                parent_task=None,  # BOSS is root, no parent
            )
        else:  # MANAGER
            prompt = prompt_builder.build_manager_decomposition_prompt(
                task_description=self.task_description,
                agent_id=self.session_id,
                agent_role="MANAGER",
                parent_task=None,  # TODO: Pass parent context if available
            )

        # Resolve operation-specific config using strategy-agnostic resolver
        llm_config = ConfigResolver.resolve(self.config, operation="task_decomposition")

        # Query LLM with resolved config
        response = await llm_port.query(prompt, llm_config.model_dump())

        # Parse and validate subtasks using domain service
        # Failure to parse is a business event (MANAGER failed decomposition)
        try:
            subtasks = SubtaskParser.parse_from_llm_response(response)
        except ValueError as e:
            # LLM response violated domain invariants - record as business fact
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"Failed to parse subtasks: {e}",
            )
            self._apply(failed_event)
            self._changes.append(failed_event)
            return  # Exit gracefully, agent is now in FAILED state

        # Create SubtasksDefined event
        subtasks_event = SubtasksDefined(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtasks=subtasks,
        )
        self._apply(subtasks_event)
        self._changes.append(subtasks_event)

        # Create ChildSpawned events for each subtask
        # Children start with PENDING role and will evaluate complexity themselves
        # Parent specifies child config (models, hyperparameters, tool) per subtask
        for subtask in subtasks:
            child_id = uuid4()
            child_event = ChildSpawned(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=AgentRole.PENDING.value,  # Role determined by child
                subtask=subtask,
                child_config=subtask.config,  # Parent's config decision for child
            )
            self._apply(child_event)
            self._changes.append(child_event)

        # Transition to WAITING status
        status_event = StatusChanged(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            old_status=self.status.value,
            new_status=AgentStatus.WAITING.value,
            reason="Decomposed task, waiting for child agents",
        )
        self._apply(status_event)
        self._changes.append(status_event)

    async def execute_task(self, tool_port: WorkerToolPort) -> None:
        """Execute task using an external worker tool (for WORKER agents).

        Uses a worker tool (Claude Code, OpenHands) to execute the task and
        captures its thinking process in real-time. Creates CodeGenerationStarted,
        ThoughtCaptured, and WorkCompleted/WorkFailed events.

        Preconditions:
            - Agent must be WORKER role
            - Agent must be in ANALYZING status

        Args:
            tool_port: Worker tool port to use for task execution.
        """
        # Preconditions: Fail fast if called on wrong agent type/status
        assert self.role == AgentRole.WORKER, f"execute_task requires WORKER agent, got {self.role}"
        assert self.status == AgentStatus.ANALYZING, (
            f"execute_task requires ANALYZING status, got {self.status}"
        )

        # Get tool name from config (all config strategies have 'tool' field)
        tool_name = self.config.tool

        # Create CodeGenerationStarted event
        started_event = CodeGenerationStarted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            tool_name=tool_name,
        )
        self._apply(started_event)
        self._changes.append(started_event)

        # Prepare task context for worker tool
        task_context = {
            "session_id": self.session_id,
            "task_description": self.task_description,
            "tool_name": tool_name,
            "config": self.config,
        }

        # Stream events from worker tool execution
        async for tool_event in tool_port.run_session(task_context):
            # Create a new event with correct aggregate_id and sequence_number
            # (tool doesn't know these values, so we recreate the event)
            event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
            event_data["aggregate_id"] = self.session_id
            event_data["sequence_number"] = self._next_sequence()

            # Recreate event with correct values
            corrected_event = type(tool_event)(**event_data)

            # Apply and record the event
            self._apply(corrected_event)
            self._changes.append(corrected_event)

    def handle_child_update(self, child_id: UUID, result: str) -> None:
        """Handle completion notification from a child agent.

        Records the child's result and checks if all children have completed.
        If all children are done, aggregates their results and completes this agent.

        This method enables parent agents (BOSS or MANAGER) to track completion
        of their children. Children can be either WORKER (simple tasks) or
        MANAGER (complex tasks requiring further decomposition).

        Preconditions:
            - Agent must be MANAGER or BOSS role (only they have children)
            - Agent must have spawned children
            - Agent must be in WAITING status
            - child_id must be in child_ids list

        System Invariants:
            - Children can be MANAGER or WORKER role (NEVER BOSS)
            - Only ONE BOSS exists in the entire system
            - BOSS and MANAGER can both spawn WORKER or MANAGER children

        Args:
            child_id: UUID of the child agent that completed.
            result: The result produced by the child agent.
        """
        # Preconditions: Fail fast if called incorrectly
        assert self.role in (AgentRole.MANAGER, AgentRole.BOSS), (
            f"handle_child_update requires MANAGER or BOSS role, got {self.role}"
        )
        assert len(self.child_ids) > 0, "handle_child_update requires agent with children"
        assert self.status == AgentStatus.WAITING, (
            f"handle_child_update requires WAITING status, got {self.status}"
        )
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        # Create ChildCompleted event
        child_completed_event = ChildCompleted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            result=result,
        )
        self._apply(child_completed_event)
        self._changes.append(child_completed_event)

        # Check if all children have completed
        if len(self.child_results) == len(self.child_ids):
            # All children done - aggregate results and complete
            aggregated_result = self._aggregate_child_results()

            work_completed_event = WorkCompleted(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                result=aggregated_result,
            )
            self._apply(work_completed_event)
            self._changes.append(work_completed_event)

    def _aggregate_child_results(self) -> str:
        """Aggregate results from all completed children.

        Returns:
            A formatted string containing all child results.
        """
        lines = ["All subtasks completed successfully:", ""]
        for child_id in self.child_ids:
            child_result = self.child_results.get(child_id, "No result")
            lines.append(f"- {child_result}")
        return "\n".join(lines)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply an event to update the agent's state (default handler).

        This is the ONLY place where state mutations should occur.
        Uses singledispatchmethod for polymorphic dispatch based on event type.

        Args:
            event: The domain event to apply.

        Raises:
            TypeError: If no handler is registered for event type (programmer error).

        Note:
            Follows "Offensive Programming" principle: fail fast when contract violated.
            If you see this error, register a handler with @_apply.register decorator.
        """
        # Fail fast: unknown event type is a programmer error
        raise TypeError(
            f"No handler registered for event type {type(event).__name__}. "
            f"Register handler with @_apply.register decorator. "
            f"This is a programmer error, not a runtime condition."
        )

    @_apply.register
    def _(self, event: AgentCreated) -> None:
        """Apply AgentCreated event to initialize agent state.

        Args:
            event: AgentCreated event with role, parent_id, and config.
        """
        self.role = AgentRole(event.role)
        self.parent_id = event.parent_id

        # Deserialize config dict to typed AgentConfig using TypeAdapter
        # This validates the config and provides type-safe access
        adapter = TypeAdapter(AgentConfig)
        self.config = adapter.validate_python(event.config)

        self.status = AgentStatus.PENDING
        self.version += 1

    @_apply.register
    def _(self, event: TaskAssigned) -> None:
        """Apply TaskAssigned event and transition to ANALYZING status.

        Args:
            event: TaskAssigned event with task_description.
        """
        self.task_description = event.task_description
        self.status = AgentStatus.ANALYZING
        self.version += 1

    @_apply.register
    def _(self, event: StatusChanged) -> None:
        """Apply StatusChanged event to update agent status.

        Args:
            event: StatusChanged event with new_status.
        """
        self.status = AgentStatus(event.new_status)
        self.version += 1

    @_apply.register
    def _(self, event: SubtasksDefined) -> None:
        """Apply SubtasksDefined event (no state change, event is recorded).

        Args:
            event: SubtasksDefined event with subtasks list.
        """
        # Subtasks are stored in the event itself, not in aggregate state
        # This event serves as an audit trail for decomposition decisions
        self.version += 1

    @_apply.register
    def _(self, event: ChildSpawned) -> None:
        """Apply ChildSpawned event to track child agent IDs.

        System Invariant: Children can only be PENDING, MANAGER, or WORKER (never BOSS).
        Only ONE BOSS exists in the system.

        Args:
            event: ChildSpawned event with child_id and child_role.

        Raises:
            AssertionError: If child_role is BOSS (system invariant violation).
        """
        # Enforce system invariant: children cannot be BOSS
        assert event.child_role != AgentRole.BOSS.value, (
            f"System invariant violated: Cannot spawn BOSS child. "
            f"Children must be PENDING, MANAGER, or WORKER. Got: {event.child_role}"
        )

        self.child_ids.append(event.child_id)
        self.version += 1

    @_apply.register
    def _(self, event: WorkFailed) -> None:
        """Apply WorkFailed event to transition to FAILED status.

        Args:
            event: WorkFailed event with failure reason.
        """
        self.status = AgentStatus.FAILED
        self.error_message = event.reason
        self.version += 1

    @_apply.register
    def _(self, event: CodeGenerationStarted) -> None:
        """Apply CodeGenerationStarted event to transition to IN_PROGRESS.

        Args:
            event: CodeGenerationStarted event with tool_name.
        """
        self.status = AgentStatus.IN_PROGRESS
        self.version += 1

    @_apply.register
    def _(self, event: ThoughtCaptured) -> None:
        """Apply ThoughtCaptured event (no state change, audit trail only).

        Args:
            event: ThoughtCaptured event with captured thought content.
        """
        # Thoughts are stored in events, not in aggregate state
        # This event serves as audit trail for worker thinking process
        self.version += 1

    @_apply.register
    def _(self, event: WorkCompleted) -> None:
        """Apply WorkCompleted event to transition to COMPLETED status.

        Args:
            event: WorkCompleted event with result.
        """
        self.status = AgentStatus.COMPLETED
        self.result = event.result
        self.version += 1

    @_apply.register
    def _(self, event: ChildCompleted) -> None:
        """Apply ChildCompleted event to track child completion.

        Args:
            event: ChildCompleted event with child_id and result.
        """
        self.child_results[event.child_id] = event.result
        self.version += 1

    @_apply.register
    def _(self, event: ComplexityEvaluated) -> None:
        """Apply ComplexityEvaluated event to set agent role based on task complexity.

        Args:
            event: ComplexityEvaluated event with determined role.
        """
        self.role = AgentRole(event.determined_role)
        self.version += 1

    @_apply.register
    def _(self, event: BudgetAllocated) -> None:
        """Apply BudgetAllocated event to set initial budget.

        Args:
            event: BudgetAllocated event with amount and source.
        """
        self.current_budget = event.amount
        self.version += 1

    @_apply.register
    def _(self, event: BudgetAdjusted) -> None:
        """Apply BudgetAdjusted event to update budget balance.

        Args:
            event: BudgetAdjusted event with adjustment and new balance.
        """
        self.current_budget = event.new_balance
        self.version += 1

    @_apply.register
    def _(self, event: TaskEnqueued) -> None:
        """Apply TaskEnqueued event to add subtask to queue.

        Args:
            event: TaskEnqueued event with subtask.
        """
        self.task_queue.append(event.subtask)
        self.version += 1

    @_apply.register
    def _(self, event: TaskDequeued) -> None:
        """Apply TaskDequeued event to remove subtask from queue.

        Args:
            event: TaskDequeued event with subtask that was removed.
        """
        # Remove the first occurrence of the subtask from queue
        if event.subtask in self.task_queue:
            self.task_queue.remove(event.subtask)
        self.version += 1

    # ========================================================================
    # Termination Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: AgentTerminated) -> None:
        """Apply AgentTerminated event to mark agent as terminated.

        Args:
            event: AgentTerminated event with termination reason.
        """
        self.status = AgentStatus.TERMINATED
        self.termination_reason = event.reason
        self.version += 1

    @_apply.register
    def _(self, event: SubtreeAborted) -> None:
        """Apply SubtreeAborted event (audit trail for subtree abortion).

        Args:
            event: SubtreeAborted event with affected child IDs.
        """
        # This event is primarily for audit trail
        # Actual child termination is handled by execution service
        self.version += 1

    # ========================================================================
    # Budget Recollection Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: BudgetRecollected) -> None:
        """Apply BudgetRecollected event to update budget after child completion.

        Args:
            event: BudgetRecollected event with recollection details.
        """
        self.current_budget += event.amount_recollected
        self.version += 1

    @_apply.register
    def _(self, event: ChildFailed) -> None:
        """Apply ChildFailed event to track child failure.

        Args:
            event: ChildFailed event with failure details.
        """
        self.child_failures[event.child_id] = event.failure_reason
        self.version += 1

    @_apply.register
    def _(self, event: AllChildrenFailed) -> None:
        """Apply AllChildrenFailed event (audit trail for total failure).

        Args:
            event: AllChildrenFailed event with penalty information.
        """
        # This event is for audit trail and analytics
        self.version += 1

    # ========================================================================
    # Verification Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: VerificationInjected) -> None:
        """Apply VerificationInjected event to track pending verification.

        Args:
            event: VerificationInjected event with verification target.
        """
        self.verification_pending.append(event.target_child_id)
        self.status = AgentStatus.VERIFYING
        self.version += 1

    @_apply.register
    def _(self, event: VerifierSpawned) -> None:
        """Apply VerifierSpawned event to track verifier agent.

        Args:
            event: VerifierSpawned event with verifier details.
        """
        self.child_ids.append(event.verifier_id)
        self.version += 1

    @_apply.register
    def _(self, event: VerificationCompleted) -> None:
        """Apply VerificationCompleted event to process verification result.

        Args:
            event: VerificationCompleted event with verification outcome.
        """
        if event.target_child_id in self.verification_pending:
            self.verification_pending.remove(event.target_child_id)
        import time
        self.last_verification_time = time.time()
        self.version += 1

    @_apply.register
    def _(self, event: TaskReinjected) -> None:
        """Apply TaskReinjected event to add task back to front of queue.

        Args:
            event: TaskReinjected event with task to redo.
        """
        # Insert at the front of the queue (priority redo)
        self.task_queue.insert(0, event.subtask)
        self.version += 1

    @_apply.register
    def _(self, event: VerificationHeuristicEvaluated) -> None:
        """Apply VerificationHeuristicEvaluated event (audit trail only).

        Args:
            event: VerificationHeuristicEvaluated event with decision details.
        """
        # This event is for audit trail and tuning heuristics
        self.version += 1

    # ========================================================================
    # Multi-Model Strategy Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: SubordinatesSpawned) -> None:
        """Apply SubordinatesSpawned event to track parallel subordinates.

        Args:
            event: SubordinatesSpawned event with subordinate configurations.
        """
        # Track current subtask and its subordinates
        self.current_subtask = event.subtask
        self.current_subtask_subordinates = []

        for config in event.subordinate_configs:
            if "child_id" in config:
                child_id = config["child_id"]
                if isinstance(child_id, str):
                    child_id = UUID(child_id)
                self.child_ids.append(child_id)
                self.current_subtask_subordinates.append(child_id)
                if "budget" in config:
                    self.child_budgets[child_id] = config["budget"]
        self.version += 1

    @_apply.register
    def _(self, event: FirstSuccessRecorded) -> None:
        """Apply FirstSuccessRecorded event when first parallel child succeeds.

        Args:
            event: FirstSuccessRecorded event with winning child details.
        """
        # Record the winning result
        self.child_results[event.winning_child_id] = event.result or event.method_used
        # Update budget with recollected amounts
        self.current_budget += event.budget_recollected_from_winner
        self.current_budget += event.budget_recollected_from_siblings
        self.version += 1

    @_apply.register
    def _(self, event: AllSubordinatesFailed) -> None:
        """Apply AllSubordinatesFailed event when all parallel children fail.

        Args:
            event: AllSubordinatesFailed event with failure details.
        """
        # Record all failures
        for child_id in event.failed_child_ids:
            reason = event.failure_reasons.get(str(child_id), "Unknown failure")
            self.child_failures[child_id] = reason
        self.version += 1

    @_apply.register
    def _(self, event: SubtaskRetried) -> None:
        """Apply SubtaskRetried event to re-insert revised subtask to queue.

        Args:
            event: SubtaskRetried event with revised subtask.
        """
        # Insert revised subtask at the head of the queue
        self.task_queue.insert(0, event.revised_subtask)
        self.version += 1

    def _initialize_defaults(self, session_id: UUID) -> None:
        """Initialize all instance attributes to default values.

        Args:
            session_id: The session identifier.
        """
        self.session_id: UUID = session_id
        self.role: AgentRole = AgentRole.BOSS  # Will be set by _apply
        self.status: AgentStatus = AgentStatus.PENDING
        self.parent_id: UUID | None = None
        self.child_ids: list[UUID] = []
        self.child_results: dict[UUID, str] = {}  # Track completed children
        self.child_failures: dict[UUID, str] = {}  # Track failed children
        self.child_budgets: dict[UUID, float] = {}  # Track budget allocated to each child
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.termination_reason: str | None = None  # Reason for termination if terminated
        self.config: AgentConfig  # Type-safe config (deserialized from events)
        self.current_budget: float = 0.0  # Resource allocation
        self.initial_budget: float = 0.0  # Budget at allocation (for recollection calc)
        self.task_queue: list[Subtask] = []  # FIFO queue of subtasks
        self.verification_pending: list[UUID] = []  # Child IDs awaiting verification
        self.last_verification_time: float = 0.0  # Timestamp of last verification
        # Track subordinates spawned for current subtask (for multi-model strategy)
        self.current_subtask_subordinates: list[UUID] = []
        self.current_subtask: Subtask | None = None
        self.subtask_retry_counts: dict[str, int] = {}  # subtask.description -> retry count
        self.version: int = 0
        self._changes: list[DomainEvent] = []
        self._sequence: int = 0

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "AgentSession":
        """Reconstruct an AgentSession from its event history.

        This factory method rehydrates an aggregate from its event stream.
        Used when loading aggregates from the event store.

        Preconditions (invariants):
            - events must not be empty
            - First event must be AgentCreated

        Args:
            events: List of domain events in sequence order.

        Returns:
            Reconstructed AgentSession with state derived from events.

        Raises:
            AssertionError: If invariants are violated (system corruption).
        """
        # Precondition: non-empty event stream
        assert events, "System invariant violated: Cannot load from empty event history"

        # Invariant: first event is always AgentCreated
        first_event = events[0]
        assert isinstance(first_event, AgentCreated), (
            f"System invariant violated: First event must be AgentCreated, "
            f"got {type(first_event).__name__}"
        )

        # Create instance via __init__ and replay events
        instance = cls(first_event.aggregate_id)

        # Replay all events to rebuild state
        for event in events:
            instance._apply(event)
            instance._sequence = max(instance._sequence, event.sequence_number)

        return instance

    def _next_sequence(self) -> int:
        """Get the next sequence number for an event.

        Returns:
            The next sequence number.
        """
        self._sequence += 1
        return self._sequence

    def is_terminal(self) -> bool:
        """Check if the agent is in a terminal state.

        Returns:
            True if status is COMPLETED, FAILED, or TERMINATED.
        """
        return self.status in (
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.TERMINATED,
        )

    def is_leaf(self) -> bool:
        """Check if this agent is a leaf node (WORKER with no children).

        Returns:
            True if role is WORKER and child_ids is empty.
        """
        return self.role == AgentRole.WORKER and len(self.child_ids) == 0

    def fail_with_reason(self, reason: str) -> None:
        """Mark this agent as failed with a reason.

        This method allows the application layer to mark an agent as failed
        when infrastructure failures (LLM errors, tool errors) cannot be recovered.
        It creates a WorkFailed event.

        Use cases:
            - LLM authentication failures (permanent)
            - Rate limits exhausted after retries
            - External tool unavailable after retries

        Args:
            reason: Human-readable explanation of why the agent failed.
        """
        failed_event = WorkFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            reason=reason,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

    def allocate_budget(self, amount: float, source: str = "initial") -> None:
        """Allocate budget to this agent.

        Budget represents the numeric resource allocation that an agent can use
        to perform its tasks. This method is typically called when creating a
        new agent or when a parent allocates resources to a child.

        Args:
            amount: The budget amount to allocate (must be positive).
            source: Source of the budget allocation (e.g., "initial", "parent", "reward").

        Raises:
            ValueError: If amount is negative.
        """
        if amount < 0:
            raise ValueError(f"Budget amount must be non-negative, got {amount}")

        budget_event = BudgetAllocated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            amount=amount,
            source=source,
        )
        self._apply(budget_event)
        self._changes.append(budget_event)

    def adjust_budget(self, adjustment: float, reason: str) -> None:
        """Adjust the agent's budget by a positive or negative amount.

        This method is used by the reward mechanism to increase budget when
        subordinates succeed or decrease budget when subordinates fail.

        Args:
            adjustment: Amount to adjust (positive for increase, negative for decrease).
            reason: Explanation for the budget adjustment.

        Note:
            Budget can go negative if penalties exceed current budget.
            This is allowed to track "debt" or over-allocation scenarios.
        """
        new_balance = self.current_budget + adjustment

        adjustment_event = BudgetAdjusted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            adjustment=adjustment,
            reason=reason,
            new_balance=new_balance,
        )
        self._apply(adjustment_event)
        self._changes.append(adjustment_event)

    def enqueue_task(self, subtask: Subtask) -> None:
        """Add a subtask to the end of the task queue.

        The task queue is a FIFO queue where agents store subtasks that need
        to be processed. Tasks are dequeued in order for execution.

        Args:
            subtask: The Subtask to add to the queue.
        """
        enqueue_event = TaskEnqueued(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
        )
        self._apply(enqueue_event)
        self._changes.append(enqueue_event)

    def dequeue_task(self) -> Subtask | None:
        """Remove and return the next task from the task queue.

        Returns:
            The next Subtask in the queue, or None if queue is empty.
        """
        if not self.task_queue:
            return None

        subtask = self.task_queue[0]
        dequeue_event = TaskDequeued(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
        )
        self._apply(dequeue_event)
        self._changes.append(dequeue_event)

        return subtask

    def peek_next_task(self) -> Subtask | None:
        """Look at the next task in the queue without removing it.

        Returns:
            The next Subtask in the queue, or None if queue is empty.
        """
        if not self.task_queue:
            return None
        return self.task_queue[0]

    def has_pending_tasks(self) -> bool:
        """Check if there are tasks remaining in the queue.

        Returns:
            True if task_queue is not empty, False otherwise.
        """
        return len(self.task_queue) > 0

    # ========================================================================
    # Termination Methods
    # ========================================================================

    def terminate(
        self,
        reason: str,
        detail: str = "",
        cascade: bool = False,
    ) -> None:
        """Terminate this agent.

        Agent termination occurs in three main scenarios:
        1. Budget Depletion: Agent's budget drops to zero or below
        2. Objective Completion: Agent successfully completes and reports to supervisor
        3. Failed Subtask: Agent fails a critical subtask and supervisor decides not to reassign

        Args:
            reason: The termination reason (use TerminationReason constants).
            detail: Additional human-readable details about the termination.
            cascade: Whether this termination should cascade to child agents.
        """
        terminated_event = AgentTerminated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            reason=reason,
            detail=detail,
            final_budget=self.current_budget,
            cascade=cascade,
        )
        self._apply(terminated_event)
        self._changes.append(terminated_event)

    def abort_subtree(self, child_ids: list[UUID], reason: str) -> None:
        """Abort an entire subtree of child agents.

        When a supervisor determines that a subtask cannot be completed,
        it aborts the subtask and all agents in that subtree.

        Args:
            child_ids: List of child agent IDs to terminate.
            reason: Why the subtree is being aborted.
        """
        abort_event = SubtreeAborted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_ids_to_terminate=child_ids,
            reason=reason,
        )
        self._apply(abort_event)
        self._changes.append(abort_event)

    def check_budget_depletion(self) -> bool:
        """Check if budget is depleted and terminate if so.

        Returns:
            True if agent was terminated due to budget depletion.
        """
        if self.current_budget <= 0:
            self.terminate(
                reason=TerminationReason.BUDGET_DEPLETED,
                detail=f"Budget depleted: {self.current_budget}",
                cascade=True,
            )
            return True
        return False

    # ========================================================================
    # Budget Recollection Methods (Reward/Penalty Mechanism)
    # ========================================================================

    def recollect_budget_from_child(
        self,
        child_id: UUID,
        original_allocation: float,
        remaining_budget: float,
        child_succeeded: bool,
        reward_ratio: float = 1.2,
        penalty_ratio: float = 0.0,
    ) -> float:
        """Recollect budget from a completed child with reward/penalty applied.

        When a child agent completes (success or failure), the supervisor recollects
        the remaining budget with a ratio applied:
        - Success: remaining_budget * reward_ratio (e.g., 1.2x)
        - Failure: remaining_budget * penalty_ratio (e.g., 0.0)

        Args:
            child_id: The ID of the child agent.
            original_allocation: The budget originally allocated to the child.
            remaining_budget: The child's remaining budget at completion.
            child_succeeded: Whether the child completed successfully.
            reward_ratio: Multiplier for successful completion (default 1.2).
            penalty_ratio: Multiplier for failed completion (default 0.0).

        Returns:
            The amount of budget recollected.
        """
        ratio = reward_ratio if child_succeeded else penalty_ratio
        amount_recollected = remaining_budget * ratio

        recollect_event = BudgetRecollected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            original_allocation=original_allocation,
            remaining_budget=remaining_budget,
            ratio_applied=ratio,
            amount_recollected=amount_recollected,
            child_succeeded=child_succeeded,
        )
        self._apply(recollect_event)
        self._changes.append(recollect_event)

        return amount_recollected

    def handle_child_failure(
        self,
        child_id: UUID,
        failure_reason: str,
        budget_at_failure: float,
        retry_attempted: bool = False,
    ) -> None:
        """Handle a child agent's failure.

        This is distinct from handle_child_update (success) and is used for
        tracking failure analytics and determining penalty ratios.

        Args:
            child_id: UUID of the child agent that failed.
            failure_reason: The reason for the failure.
            budget_at_failure: The child's remaining budget when it failed.
            retry_attempted: Whether a retry was attempted before recording failure.
        """
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        failed_event = ChildFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            failure_reason=failure_reason,
            budget_at_failure=budget_at_failure,
            retry_attempted=retry_attempted,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

    def record_all_children_failed(
        self,
        subtask_description: str,
        child_ids: list[UUID],
        penalty_ratio: float = 0.0,
    ) -> None:
        """Record that all children failed a subtask.

        When all subordinate nodes fail, the supervisor creates a penalty on all.

        Args:
            subtask_description: Description of the failed subtask.
            child_ids: List of child agent IDs that all failed.
            penalty_ratio: The penalty ratio applied to budget recollection.
        """
        all_failed_event = AllChildrenFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask_description=subtask_description,
            child_ids=child_ids,
            penalty_ratio=penalty_ratio,
        )
        self._apply(all_failed_event)
        self._changes.append(all_failed_event)

    # ========================================================================
    # Verification Methods
    # ========================================================================

    def inject_verification_task(
        self,
        target_subtask: Subtask,
        target_child_id: UUID,
        injection_reason: str,
        estimated_cost: float = 0.0,
    ) -> None:
        """Inject a verification task into the queue.

        The supervisor injects verification tasks based on heuristics to validate
        completed work and catch potential false negatives.

        Args:
            target_subtask: The subtask being verified (just completed).
            target_child_id: The child that completed the subtask being verified.
            injection_reason: Why this verification was injected.
            estimated_cost: Expected budget cost for verification.
        """
        inject_event = VerificationInjected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            injection_reason=injection_reason,
            estimated_verification_cost=estimated_cost,
        )
        self._apply(inject_event)
        self._changes.append(inject_event)

    def spawn_verifier(
        self,
        verifier_id: UUID,
        target_subtask: Subtask,
        target_child_id: UUID,
        verifier_config: dict[str, Any],
    ) -> None:
        """Spawn an independent verifier sub-agent.

        The verifier agent is expected to be different from all original
        subordinate nodes (often a more advanced model).

        Args:
            verifier_id: UUID for the new verifier agent.
            target_subtask: The subtask being verified.
            target_child_id: The child whose work is being verified.
            verifier_config: Configuration for the verifier agent.
        """
        spawn_event = VerifierSpawned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            verifier_id=verifier_id,
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            verifier_config=verifier_config,
        )
        self._apply(spawn_event)
        self._changes.append(spawn_event)

    def complete_verification(
        self,
        verifier_id: UUID,
        target_subtask: Subtask,
        target_child_id: UUID,
        verification_passed: bool,
        verification_report: str = "",
        issues_found: list[str] | None = None,
    ) -> None:
        """Record completion of a verification task.

        Args:
            verifier_id: UUID of the verifier agent.
            target_subtask: The subtask that was verified.
            target_child_id: The child whose work was verified.
            verification_passed: Whether the original work passed verification.
            verification_report: Detailed report from the verifier.
            issues_found: List of issues found during verification.
        """
        complete_event = VerificationCompleted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            verifier_id=verifier_id,
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            verification_passed=verification_passed,
            verification_report=verification_report,
            issues_found=issues_found or [],
        )
        self._apply(complete_event)
        self._changes.append(complete_event)

    def reinject_failed_task(
        self,
        subtask: Subtask,
        verification_context: str,
        original_child_id: UUID,
        retry_count: int = 1,
    ) -> None:
        """Re-inject a task that failed verification for redo.

        If the verifier reports that the previous task was not correctly finished,
        the supervisor re-injects the same task with additional context.

        Args:
            subtask: The subtask being re-injected for redo.
            verification_context: Context from the verifier to help redo.
            original_child_id: The child that originally failed this task.
            retry_count: Number of times this task has been reinjected.
        """
        reinject_event = TaskReinjected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            verification_context=verification_context,
            original_child_id=original_child_id,
            retry_count=retry_count,
        )
        self._apply(reinject_event)
        self._changes.append(reinject_event)

    def record_verification_heuristic_evaluation(
        self,
        subtask_completed: Subtask,
        child_id: UUID,
        complexity_score: float,
        subtree_size: int,
        random_roll: float,
        random_threshold: float,
        time_since_last: float,
        edits_count: int,
        expected_edits_range: tuple[int, int],
        suspicious: bool,
        available_budget: float,
        verification_decided: bool,
        decision_reasoning: str,
    ) -> None:
        """Record the verification heuristic evaluation for audit trail.

        This provides an audit trail for debugging and tuning the heuristics.

        Args:
            subtask_completed: The subtask that just completed.
            child_id: The child that completed the subtask.
            complexity_score: Estimated complexity of the subtask (0.0-1.0).
            subtree_size: Number of agents in the subtree.
            random_roll: Random value used for probability check (0.0-1.0).
            random_threshold: Threshold for random verification injection.
            time_since_last: Seconds since last verification task.
            edits_count: Number of edits reported by the child.
            expected_edits_range: Expected range of edits for task complexity.
            suspicious: Whether the completion is flagged as suspicious.
            available_budget: Budget available for verification.
            verification_decided: Whether a verification task was injected.
            decision_reasoning: Human-readable explanation of the decision.
        """
        heuristic_event = VerificationHeuristicEvaluated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask_completed=subtask_completed,
            child_id=child_id,
            complexity_score=complexity_score,
            subtree_size=subtree_size,
            random_roll=random_roll,
            random_threshold=random_threshold,
            time_since_last_verification=time_since_last,
            edits_count=edits_count,
            expected_edits_range=expected_edits_range,
            suspicious=suspicious,
            available_budget=available_budget,
            verification_decided=verification_decided,
            decision_reasoning=decision_reasoning,
        )
        self._apply(heuristic_event)
        self._changes.append(heuristic_event)

    # ========================================================================
    # Multi-Model Strategy Methods
    # ========================================================================

    def spawn_parallel_subordinates(
        self,
        subtask: Subtask,
        subordinate_configs: list[dict[str, Any]],
        total_budget: float = 0.0,
    ) -> None:
        """Spawn multiple subordinate nodes for the same subtask in parallel.

        Default behavior: Spawn 3 agents with different LLM models, each with
        equal budget split. Only one needs to succeed.

        Args:
            subtask: The subtask assigned to all subordinates.
            subordinate_configs: List of configs with child_id, model, method_hint, budget.
            total_budget: Total budget allocated for this subtask.
        """
        spawn_event = SubordinatesSpawned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            total_budget_allocated=total_budget,
            subordinate_configs=subordinate_configs,
        )
        self._apply(spawn_event)
        self._changes.append(spawn_event)

    def record_first_success(
        self,
        winning_child_id: UUID,
        subtask: Subtask,
        sibling_ids_terminated: list[UUID],
        result: str = "",
        method_used: str = "",
        budget_recollected_from_winner: float = 0.0,
        budget_recollected_from_siblings: float = 0.0,
    ) -> None:
        """Record when the first parallel subordinate succeeds.

        When multiple subordinates work on the same subtask, the first to succeed
        triggers termination of all siblings to save budget.

        Args:
            winning_child_id: The child that succeeded first.
            subtask: The subtask that was completed.
            sibling_ids_terminated: List of sibling IDs that were terminated.
            result: The result produced by the winning child.
            method_used: The method/approach used by the winning child.
            budget_recollected_from_winner: Budget recollected with reward ratio.
            budget_recollected_from_siblings: Budget recollected from terminated siblings.
        """
        success_event = FirstSuccessRecorded(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            winning_child_id=winning_child_id,
            subtask=subtask,
            sibling_ids_terminated=sibling_ids_terminated,
            result=result,
            method_used=method_used,
            budget_recollected_from_winner=budget_recollected_from_winner,
            budget_recollected_from_siblings=budget_recollected_from_siblings,
        )
        self._apply(success_event)
        self._changes.append(success_event)

        # Clear current subtask tracking
        self.current_subtask = None
        self.current_subtask_subordinates = []

    def record_all_subordinates_failed(
        self,
        subtask: Subtask,
        failed_child_ids: list[UUID],
        failure_reasons: dict[str, str],
        total_budget_lost: float = 0.0,
    ) -> None:
        """Record when all parallel subordinates fail a subtask.

        This is a terminal condition for the subtask. The supervisor must decide
        whether to retry or report failure.

        Args:
            subtask: The subtask that all subordinates failed.
            failed_child_ids: List of all subordinates that failed.
            failure_reasons: Map of child_id (as string) -> failure reason.
            total_budget_lost: Total budget consumed by all failed subordinates.
        """
        failed_event = AllSubordinatesFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            failed_child_ids=failed_child_ids,
            failure_reasons=failure_reasons,
            total_budget_lost=total_budget_lost,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

        # Clear current subtask tracking
        self.current_subtask = None
        self.current_subtask_subordinates = []

    def retry_subtask(
        self,
        original_subtask: Subtask,
        revised_subtask: Subtask,
        revision_reason: str,
        additional_context: str = "",
    ) -> None:
        """Retry a failed subtask with a revised version.

        Re-inserts the revised subtask at the head of the task queue.

        Args:
            original_subtask: The original subtask that failed.
            revised_subtask: The revised subtask with additional context.
            revision_reason: Why the subtask was revised.
            additional_context: Context from failures/verification to help retry.
        """
        # Track retry count
        retry_count = self.subtask_retry_counts.get(original_subtask.description, 0) + 1
        self.subtask_retry_counts[original_subtask.description] = retry_count

        retry_event = SubtaskRetried(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            original_subtask=original_subtask,
            revised_subtask=revised_subtask,
            retry_count=retry_count,
            revision_reason=revision_reason,
            additional_context=additional_context,
        )
        self._apply(retry_event)
        self._changes.append(retry_event)
