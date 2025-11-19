"""Domain events for the multi-agent system.

This module defines the base event class and all domain events used in the
event sourcing architecture. All events are immutable (frozen) Pydantic models.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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
        subtasks: List of subtask definitions (each contains description, priority, etc.).
    """

    subtasks: list[dict[str, Any]]


class ChildSpawned(DomainEvent):
    """Event raised when an agent spawns a child agent.

    Attributes:
        child_id: UUID of the newly created child agent.
        child_role: Role of the child agent (MANAGER or WORKER).
    """

    child_id: UUID
    child_role: str


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
