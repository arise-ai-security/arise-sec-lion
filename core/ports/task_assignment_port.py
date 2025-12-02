"""Task assignment mechanism port definition.

This module defines the abstract interface for task assignment strategies.
Implementations determine when to retry failed tasks, terminate unproductive
work, or reassign tasks to different agents.
"""

from enum import Enum
from typing import Protocol
from uuid import UUID

from core.domain.subtask import Subtask


class TaskDecision(str, Enum):
    """Decision about how to handle a task.

    Attributes:
        EXECUTE: Execute the task immediately.
        RETRY: Retry the task (possibly with different configuration).
        TERMINATE: Terminate the task (stop working on it).
        REASSIGN: Reassign the task to a different agent.
        DEFER: Defer the task to later (put it back in queue).
    """

    EXECUTE = "execute"
    RETRY = "retry"
    TERMINATE = "terminate"
    REASSIGN = "reassign"
    DEFER = "defer"


class TaskAssignmentPort(Protocol):
    """Abstract interface for task assignment decisions.

    The task assignment mechanism determines whether a task should be executed,
    retried, terminated, or reassigned based on various factors like:
    - Current agent budget
    - Task complexity
    - Previous failure count
    - Time elapsed
    - Agent performance history

    Implementations can provide different strategies:
    - Simple: Always execute unless out of budget
    - Retry-based: Retry failed tasks up to N times
    - Budget-aware: Only execute if sufficient budget remains
    - Performance-based: Consider agent's historical success rate
    """

    def decide_task_action(
        self,
        agent_id: UUID,
        subtask: Subtask,
        current_budget: float,
        failure_count: int = 0,
    ) -> TaskDecision:
        """Decide what action to take for a given task.

        Args:
            agent_id: UUID of the agent that would execute the task.
            subtask: The Subtask to be assigned.
            current_budget: Current budget available to the agent.
            failure_count: Number of times this task has failed previously.

        Returns:
            TaskDecision indicating what to do with the task.
        """
        ...

    def should_retry(
        self,
        agent_id: UUID,
        subtask: Subtask,
        failure_reason: str,
        retry_count: int,
    ) -> bool:
        """Determine if a failed task should be retried.

        Args:
            agent_id: UUID of the agent that failed the task.
            subtask: The Subtask that failed.
            failure_reason: Reason for the failure.
            retry_count: Number of times already retried.

        Returns:
            True if the task should be retried, False to terminate.
        """
        ...
