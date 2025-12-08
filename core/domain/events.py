"""Domain events for the multi-agent system.

This module defines the base event class and all domain events used in the
event sourcing architecture. All events are immutable (frozen) Pydantic models.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from core.domain.subtask import Subtask


def _utc_now() -> datetime:
    """Get current UTC datetime (Python 3.12+ compatible)."""
    return datetime.now(UTC)


class DomainEvent(BaseModel):
    """Base class for all domain events.

    Domain events represent facts that have occurred in the system. They are
    immutable and used to reconstruct aggregate state via event sourcing.

    Attributes:
        event_id: Unique identifier for this event instance.
        aggregate_id: ID of the aggregate (AgentSession) this event belongs to.
        sequence_number: Order of this event in the aggregate's event stream.
        occurred_at: Timestamp when the event occurred.
        metadata: Additional context about the event (e.g., user_id, correlation_id).
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# Specific Domain Events


class AgentCreated(DomainEvent):
    """Event raised when a new agent session is created.

    Attributes:
        role: The role of the agent (BOSS, MANAGER, or WORKER).
        parent_id: ID of the parent agent (None for BOSS).
        config: Configuration settings for this agent.
    """

    role: str
    parent_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class TaskAssigned(DomainEvent):
    """Event raised when a task is assigned to an agent.

    Attributes:
        task_description: The task to be executed.
        constraints: Constraints or requirements for task execution.
    """

    task_description: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class StatusChanged(DomainEvent):
    """Event raised when an agent's status changes.

    Attributes:
        old_status: The previous status.
        new_status: The new status.
        reason: Explanation for the status change.
    """

    old_status: str
    new_status: str
    reason: str = ""


class SubtasksDefined(DomainEvent):
    """Event raised when a MANAGER agent decomposes a task into subtasks.

    Attributes:
        subtasks: List of Subtask value objects defining atomic units of work.
    """

    subtasks: list[Subtask]


class ChildSpawned(DomainEvent):
    """Event raised when an agent spawns a child agent.

    This event captures the parent's decision about:
    1. What task to assign (subtask.description)
    2. How the child should operate (child_config)

    The child_config field allows the parent to specify which models/hyperparameters
    the child uses for different operations (complexity evaluation, task decomposition).
    This makes the system "super flexible" - parents control all child behavior.

    Attributes:
        child_id: UUID of the newly created child agent.
        child_role: Role of the child agent (PENDING initially, becomes MANAGER or WORKER).
        subtask: The specific Subtask value object assigned to this child.
        child_config: Serialized AgentConfig dict for the child agent.
                     Specifies models, hyperparameters, and tool selection.
    """

    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]


class WorkCompleted(DomainEvent):
    """Event raised when an agent successfully completes its work.

    Attributes:
        result: The final result or output of the work.
    """

    result: str


class WorkFailed(DomainEvent):
    """Event raised when an agent fails to complete its work.

    Attributes:
        reason: Explanation of why the work failed.
    """

    reason: str


class CodeGenerationStarted(DomainEvent):
    """Event raised when a WORKER agent starts code generation/execution.

    Attributes:
        tool_name: Name of the worker tool being used (e.g., "claude-code", "openhands").
    """

    tool_name: str


class ThoughtCaptured(DomainEvent):
    """Event raised when a WORKER tool emits thinking/logging output.

    This event captures real-time stdout/stderr from worker tools (Claude Code, OpenHands)
    to provide visibility into the agent's reasoning and execution process.

    Attributes:
        content: The captured thought/log content.
        stream: Which stream this came from ("stdout", "stderr", "tool", "openhands").
        output_type: Classification of output type:
            - "thinking": Internal reasoning (Claude's thinking, OpenHands agent thoughts)
            - "progress": Task progress updates (subtask completion, file operations)
            - "output": Actual command/tool output (default)
            - "debug": Diagnostic information (verbose logs if needed)
    """

    content: str
    stream: str = "tool"
    output_type: str = "output"


class ChildCompleted(DomainEvent):
    """Event raised when a child agent completes its work.

    This event is used by parent agents (MANAGER, BOSS) to track the completion
    status of their child agents and aggregate results.

    Attributes:
        child_id: UUID of the child agent that completed.
        result: The result produced by the child agent.
    """

    child_id: UUID
    result: str


class ComplexityEvaluated(DomainEvent):
    """Event raised when an agent evaluates its task complexity.

    After receiving a task assignment, child agents evaluate whether their
    task is SIMPLE (can be executed directly) or COMPLEX (requires decomposition).
    This determines whether the agent will act as WORKER or MANAGER.

    Attributes:
        complexity: The evaluated complexity ("simple" or "complex").
        determined_role: The role determined based on complexity (worker or manager).
        reasoning: LLM's reasoning for the complexity decision.
    """

    complexity: str  # "simple" or "complex"
    determined_role: str  # "worker" or "manager"
    reasoning: str = ""
