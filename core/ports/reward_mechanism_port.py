"""Reward mechanism port definition.

This module defines the abstract interface for budget adjustment calculations
based on subordinate agent outcomes. Infrastructure implementations can provide
different reward strategies (linear, exponential, adaptive, etc.).

Budget Recollection Ratios (from Agent Lifecycle description):
- Success: Supervisor recollects remaining budget with reward ratio (e.g., 1.2x)
- Unfinished/Terminated: No reward or penalty ratio (e.g., 1.0x)
- Failure: Return remaining budget with penalty ratio (e.g., 0.8x)
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RecollectionResult:
    """Result of budget recollection calculation.

    Attributes:
        original_allocation: Budget originally allocated to the child.
        remaining_budget: Child's remaining budget at completion.
        ratio_applied: The ratio applied to remaining budget.
        amount_recollected: The actual amount recollected (remaining * ratio).
        reason: Human-readable explanation of the recollection.
    """

    original_allocation: float
    remaining_budget: float
    ratio_applied: float
    amount_recollected: float
    reason: str


class RewardMechanismPort(Protocol):
    """Abstract interface for budget recollection calculations.

    The reward mechanism determines how budget is recollected from subordinate
    nodes based on their completion status. This follows the Agent Lifecycle:

    1. Success: First successful subordinate returns remaining budget * reward_ratio
    2. Unfinished: Other subordinates (terminated early) return remaining * neutral_ratio
    3. Failure: Failed subordinates return remaining budget * penalty_ratio

    Example from description:
    - Subordinate 2 succeeds with 111 budget remaining, reward_ratio=1.2
    - Supervisor receives: 111 * 1.2 = 133.2
    - Other subordinates terminated, lose their allocated budget (1.0x = neutral)

    Implementations can provide different strategies:
    - Fixed: Use constant ratios (1.2, 1.0, 0.8)
    - Adaptive: Dynamic ratios based on historical performance
    - Complexity-based: Adjust ratios based on task complexity
    """

    def get_success_ratio(self, agent_id: UUID, child_id: UUID) -> float:
        """Get the reward ratio for a successful subordinate.

        Default: 1.2 (120% of remaining budget returned)

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the successful child agent.

        Returns:
            The ratio to apply to remaining budget (e.g., 1.2).
        """
        ...

    def get_failure_ratio(self, agent_id: UUID, child_id: UUID) -> float:
        """Get the penalty ratio for a failed subordinate.

        Default: 0.8 (80% of remaining budget returned)

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the failed child agent.

        Returns:
            The ratio to apply to remaining budget (e.g., 0.8).
        """
        ...

    def get_termination_ratio(self, agent_id: UUID, child_id: UUID) -> float:
        """Get the ratio for a terminated (unfinished) subordinate.

        Default: 1.0 (100% of remaining budget returned, no reward/penalty)

        This is applied when a subordinate is terminated early because
        another sibling completed the task first.

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the terminated child agent.

        Returns:
            The ratio to apply to remaining budget (e.g., 1.0).
        """
        ...

    def calculate_recollection(
        self,
        agent_id: UUID,
        child_id: UUID,
        original_allocation: float,
        remaining_budget: float,
        child_succeeded: bool,
        child_failed: bool,
    ) -> RecollectionResult:
        """Calculate budget recollection from a completed/terminated child.

        This is the main calculation method that determines how much budget
        the supervisor gets back from a subordinate.

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the child agent.
            original_allocation: Budget originally allocated to the child.
            remaining_budget: Child's remaining budget at completion.
            child_succeeded: Whether the child completed successfully.
            child_failed: Whether the child failed (distinct from terminated).

        Returns:
            RecollectionResult with the amount to recollect and details.
        """
        ...

    def calculate_success_reward(
        self,
        agent_id: UUID,
        child_id: UUID,
        current_budget: float,
        child_result: str,
    ) -> float:
        """Calculate budget increase when a subordinate succeeds.

        DEPRECATED: Use calculate_recollection() instead.

        Args:
            agent_id: UUID of the parent agent receiving the reward.
            child_id: UUID of the child agent that succeeded.
            current_budget: Current budget of the parent agent.
            child_result: Result produced by the successful child.

        Returns:
            The amount to increase the budget by (positive float).
        """
        ...

    def calculate_failure_penalty(
        self,
        agent_id: UUID,
        child_id: UUID,
        current_budget: float,
        failure_reason: str,
    ) -> float:
        """Calculate budget decrease when a subordinate fails.

        DEPRECATED: Use calculate_recollection() instead.

        Args:
            agent_id: UUID of the parent agent receiving the penalty.
            child_id: UUID of the child agent that failed.
            current_budget: Current budget of the parent agent.
            failure_reason: Reason for the child's failure.

        Returns:
            The amount to decrease the budget by (positive float, will be subtracted).
        """
        ...
