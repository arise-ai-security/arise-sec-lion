"""Core domain models for the multi-agent system.

This module contains the pure business logic and data structures for agent
sessions. It has NO external dependencies (except Pydantic) per Hexagonal
Architecture constraints.
"""

from enum import Enum
from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid4

from core.domain.events import (
    AgentCreated,
    ChildSpawned,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    WorkFailed,
)
from core.domain.prompt_loader import render_prompt
from core.domain.services import SubtaskParser
from core.ports.llm_port import LLMPort


class AgentRole(str, Enum):
    """The role of an agent in the hierarchical system.

    Attributes:
        BOSS: Top-level agent that delegates to managers.
        MANAGER: Mid-level agent that decomposes tasks and delegates to workers.
        WORKER: Leaf-level agent that executes tasks using tools (Claude Code, OpenHands).
    """

    BOSS = "boss"
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
    def events(self) -> list[Any]:
        """Get the list of uncommitted events (changes).

        Returns:
            List of domain events that have been created but not yet persisted.
        """
        return self._changes

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

    async def evaluate_task(self, llm_port: LLMPort) -> None:
        """Evaluate task and decompose into subtasks (for MANAGER agents).

        Uses an LLM to analyze the task and break it down into subtasks.
        Creates SubtasksDefined and ChildSpawned events for each subtask.

        Preconditions:
            - Agent must be MANAGER role
            - Agent must be in ANALYZING status

        Args:
            llm_port: LLM port to use for task decomposition.
        """
        # Preconditions: Fail fast if called on wrong agent type/status
        assert self.role == AgentRole.MANAGER, (
            f"evaluate_task requires MANAGER agent, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, (
            f"evaluate_task requires ANALYZING status, got {self.status}"
        )

        # Construct prompt from Jinja2 template
        prompt = render_prompt(
            "manager/task_decomposition.j2",
            task_description=self.task_description,
        )

        # Query LLM
        response = await llm_port.query(prompt, self.config)

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
        for subtask in subtasks:
            child_id = uuid4()
            child_event = ChildSpawned(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=AgentRole.WORKER.value,
                subtask=subtask,
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
        self.config = event.config
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

        Args:
            event: ChildSpawned event with child_id and child_role.
        """
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
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: dict[str, Any] = {}
        self.version: int = 0
        self._changes: list[Any] = []
        self._sequence: int = 0

    @classmethod
    def load_from_history(cls, events: list[Any]) -> "AgentSession":
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
