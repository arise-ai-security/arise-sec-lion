"""Reward mechanism port definition.

This module defines the abstract interface for budget adjustment calculations
based on subordinate agent outcomes. Infrastructure implementations can provide
different reward strategies (linear, exponential, adaptive, etc.).
"""

from typing import Protocol
from uuid import UUID


class RewardMechanismPort(Protocol):
    """Abstract interface for budget adjustment calculations.

    The reward mechanism determines how an agent's budget changes based on
    the success or failure of its subordinate nodes. This enables adaptive
    resource allocation where successful agents receive more budget to continue
    their work, while failing agents may have budget reduced or reallocated.

    Implementations can provide different strategies:
    - Linear: Fixed reward/penalty amounts
    - Proportional: Rewards proportional to task complexity or child count
    - Adaptive: Dynamic adjustments based on historical performance
    - Hierarchical: Different rewards based on agent role (BOSS, MANAGER, WORKER)
    """

    def calculate_success_reward(
        self,
        agent_id: UUID,
        child_id: UUID,
        current_budget: float,
        child_result: str,
    ) -> float:
        """Calculate budget increase when a subordinate succeeds.

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

        Args:
            agent_id: UUID of the parent agent receiving the penalty.
            child_id: UUID of the child agent that failed.
            current_budget: Current budget of the parent agent.
            failure_reason: Reason for the child's failure.

        Returns:
            The amount to decrease the budget by (positive float, will be subtracted).
        """
        ...
