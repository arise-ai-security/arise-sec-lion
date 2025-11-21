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
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.prompt_loader import render_prompt
from core.domain.services import SubtaskParser
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
    """

    PENDING = "pending"
    ANALYZING = "analyzing"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentSession:
    """Event-sourced aggregate representing an agent session.

    AgentSession is the core aggregate in our event-sourced system. Its state
    is derived by replaying domain events. All state changes occur by applying
    events, never by direct mutation.

    Event Sourcing Pattern:
        - All state changes create events (stored in self._changes)
        - Events are applied via _apply() method
        - State can be reconstructed by replaying events via load_from_history()

    Attributes:
        session_id: Unique identifier for this agent session.
        role: The hierarchical role (BOSS, MANAGER, or WORKER).
        status: Current execution status of the agent.
        parent_id: ID of the parent agent session (None for BOSS).
        child_ids: List of child agent session IDs spawned by this agent.
        task_description: The task assigned to this agent.
        result: The final result/output when status is COMPLETED.
        error_message: Error details when status is FAILED.
        config: Configuration for this agent.
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

    async def evaluate_complexity(self, llm_port: LLMPort) -> None:
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
        """
        # Preconditions: Fail fast if called incorrectly
        assert self.role == AgentRole.PENDING, (
            f"evaluate_complexity requires PENDING role, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, (
            f"evaluate_complexity requires ANALYZING status, got {self.status}"
        )
        assert self.task_description, "evaluate_complexity requires assigned task"

        # Construct prompt from Jinja2 template
        prompt = render_prompt(
            "child/complexity_evaluation.j2",
            task_description=self.task_description,
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

    async def evaluate_task(self, llm_port: LLMPort) -> None:
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
        """
        # Preconditions: Fail fast if called on wrong agent type/status
        assert self.role in (AgentRole.BOSS, AgentRole.MANAGER), (
            f"evaluate_task requires BOSS or MANAGER agent, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, (
            f"evaluate_task requires ANALYZING status, got {self.status}"
        )

        # Construct prompt from Jinja2 template
        prompt = render_prompt(
            "manager/task_decomposition.j2",
            task_description=self.task_description,
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
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: AgentConfig  # Type-safe config (deserialized from events)
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
            True if status is COMPLETED or FAILED, False otherwise.
        """
        return self.status in (AgentStatus.COMPLETED, AgentStatus.FAILED)

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
