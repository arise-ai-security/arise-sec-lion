"""Core domain models for the multi-agent system.

This module contains the pure business logic and data structures for agent
sessions. It has NO external dependencies (except Pydantic) per Hexagonal
Architecture constraints.
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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
        IN_PROGRESS: Agent is actively working on its task.
        WAITING: Agent is waiting for child agents to complete.
        COMPLETED: Agent has successfully completed its task.
        FAILED: Agent encountered an error and cannot proceed.
        BLOCKED: Agent is blocked waiting for external input or resolution.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentSession(BaseModel):
    """Represents the state of a single agent session in the system.

    AgentSession is the core aggregate in our event-sourced system. Its state
    is derived by replaying domain events. This class holds the current
    snapshot of an agent's execution state.

    Attributes:
        session_id: Unique identifier for this agent session.
        role: The hierarchical role (BOSS, MANAGER, or WORKER).
        status: Current execution status of the agent.
        parent_id: ID of the parent agent session (None for BOSS).
        child_ids: List of child agent session IDs spawned by this agent.
        task_description: The task assigned to this agent.
        result: The final result/output when status is COMPLETED.
        error_message: Error details when status is FAILED.
        thinking_logs: Captured stdout/stderr from worker tools (for WORKER role).
        created_at: When this session was created.
        updated_at: When this session was last modified.
        version: Event stream version (for Optimistic Concurrency Control).
        metadata: Additional context and configuration.
    """

    session_id: UUID
    role: AgentRole
    status: AgentStatus = AgentStatus.PENDING
    parent_id: UUID | None = None
    child_ids: list[UUID] = Field(default_factory=list)
    task_description: str
    result: str | None = None
    error_message: str | None = None
    thinking_logs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    version: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

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
