"""Reward mechanism port definition.

This module defines the abstract interface for budget adjustment calculations
based on subordinate agent outcomes. Infrastructure implementations can provide
different reward strategies (linear, exponential, adaptive, etc.).

Budget Recollection Ratios (from Agent Lifecycle flowchart):
- SUCCESS: Supervisor increases budget by: PLUS Reward_Ratio * X_Y2
  where X_Y2 is the budget allocated to the subordinate for task Y2
- FAILURE: Supervisor decreases budget by: MINUS Penalty_Ratio * X_Y2

The ratio values are heuristics to be tuned based on:
- Task complexity
- Historical success rates
- Resource utilization efficiency
- Time to completion

Note: Current implementation uses placeholder ratios. Production systems should
implement adaptive strategies based on system learning.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import UUID


class RecollectionReason(Enum):
    """Reason for budget recollection from a subordinate."""

    SUCCESS = "success"  # Child completed task successfully
    FAILURE = "failure"  # Child failed the task
    TERMINATED = "terminated"  # Child was terminated (sibling succeeded)
    BUDGET_DEPLETED = "budget_depleted"  # Child ran out of budget


@dataclass(frozen=True)
class RecollectionContext:
    """Context information for heuristic-based ratio calculation.

    This provides all the information an adaptive reward mechanism needs
    to calculate appropriate reward/penalty ratios.

    Attributes:
        task_complexity: Estimated complexity score (0.0-1.0).
        subtree_depth: Depth of the agent in the hierarchy.
        time_elapsed: Time spent on the task in seconds.
        budget_utilization: Ratio of budget consumed (consumed/allocated).
        retry_count: Number of times this task was retried.
        sibling_count: Number of parallel subordinates for this task.
        historical_success_rate: Agent's historical success rate (0.0-1.0).
        metadata: Additional context for custom heuristics.
    """

    task_complexity: float = 0.0
    subtree_depth: int = 0
    time_elapsed: float = 0.0
    budget_utilization: float = 0.0
    retry_count: int = 0
    sibling_count: int = 1
    historical_success_rate: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecollectionResult:
    """Result of budget recollection calculation.

    Per the flowchart:
    - SUCCESS: supervisor.budget += reward_ratio * allocated_budget
    - FAILURE: supervisor.budget -= penalty_ratio * allocated_budget

    Attributes:
        original_allocation: Budget originally allocated to the child (X_Y2).
        remaining_budget: Child's remaining budget at completion.
        ratio_applied: The ratio applied (reward_ratio or penalty_ratio).
        amount_recollected: The budget change amount (can be positive or negative).
        reason: Human-readable explanation of the recollection.
        recollection_reason: Enum indicating why recollection occurred.
    """

    original_allocation: float
    remaining_budget: float
    ratio_applied: float
    amount_recollected: float
    reason: str
    recollection_reason: RecollectionReason = RecollectionReason.SUCCESS


class RewardMechanismPort(Protocol):
    """Abstract interface for budget recollection calculations.

    The reward mechanism determines how budget flows back to supervisor nodes
    based on subordinate completion status. Per the flowchart:

    Supervisor Node S with Budget X allocates budget X_Y2 to subordinate for task Y2.

    On completion:
    - SUCCESS: S increases budget by: reward_ratio * X_Y2
    - FAILURE: S decreases budget by: penalty_ratio * X_Y2

    The ratios are heuristics that can be tuned based on:
    - Task complexity and depth in hierarchy
    - Historical agent performance
    - Budget utilization efficiency
    - System-wide resource constraints

    Implementations can provide different strategies:
    - Fixed: Use constant ratios (default: 0.2 reward, 0.1 penalty)
    - Adaptive: Dynamic ratios based on historical performance
    - Complexity-based: Adjust ratios based on task complexity
    - Learning-based: ML models trained on outcome data

    Default heuristic ratios (placeholder values to be tuned):
    - reward_ratio: 0.2 (supervisor gains 20% of allocated budget on success)
    - penalty_ratio: 0.1 (supervisor loses 10% of allocated budget on failure)
    - termination_ratio: 0.0 (no reward/penalty for terminated siblings)
    """

    def get_reward_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the reward ratio for a successful subordinate.

        Per flowchart: S Increases Budget by PLUS Reward_Ratio * X_Y2

        Default heuristic: 0.2 (gain 20% of allocated budget)

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the successful child agent.
            context: Optional context for adaptive ratio calculation.

        Returns:
            The reward ratio to apply (e.g., 0.2 means +20% of allocation).
        """
        ...

    def get_penalty_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the penalty ratio for a failed subordinate.

        Per flowchart: S Decreases Budget by MINUS Penalty_Ratio * X_Y2

        Default heuristic: 0.1 (lose 10% of allocated budget)

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the failed child agent.
            context: Optional context for adaptive ratio calculation.

        Returns:
            The penalty ratio to apply (e.g., 0.1 means -10% of allocation).
        """
        ...

    def get_termination_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the ratio for a terminated (unfinished) subordinate.

        Default heuristic: 0.0 (no reward/penalty for terminated siblings)

        This is applied when a subordinate is terminated early because
        another sibling completed the task first.

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the terminated child agent.
            context: Optional context for adaptive ratio calculation.

        Returns:
            The ratio to apply (e.g., 0.0 means no change).
        """
        ...

    def calculate_recollection(
        self,
        agent_id: UUID,
        child_id: UUID,
        allocated_budget: float,
        remaining_budget: float,
        child_succeeded: bool,
        child_failed: bool,
        context: RecollectionContext | None = None,
    ) -> RecollectionResult:
        """Calculate budget adjustment from a completed/terminated child.

        Per the flowchart, this calculates the budget change for the supervisor:
        - SUCCESS: +reward_ratio * allocated_budget
        - FAILURE: -penalty_ratio * allocated_budget
        - TERMINATED: ±termination_ratio * allocated_budget (usually 0)

        The remaining_budget is recollected separately (always returned to
        supervisor), and then the reward/penalty is applied on top.

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the child agent.
            allocated_budget: Budget originally allocated to the child (X_Y2).
            remaining_budget: Child's remaining budget at completion.
            child_succeeded: Whether the child completed successfully.
            child_failed: Whether the child failed (distinct from terminated).
            context: Optional context for adaptive ratio calculation.

        Returns:
            RecollectionResult with the budget adjustment and details.
            - amount_recollected: The net budget change (positive = increase, negative = decrease)
            - ratio_applied: The reward/penalty ratio used
        """
        ...
