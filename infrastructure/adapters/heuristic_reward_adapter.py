"""Heuristic-based reward mechanism adapter.

This module provides a concrete implementation of RewardMechanismPort using
heuristic-based ratio calculations. The ratios are placeholders designed to be
tuned based on system performance data.

Per the flowchart:
- SUCCESS: Supervisor increases budget by: reward_ratio * allocated_budget
- FAILURE: Supervisor decreases budget by: penalty_ratio * allocated_budget

The heuristics consider:
- Task complexity (higher complexity = higher reward/penalty stakes)
- Budget utilization efficiency (efficient use = higher reward)
- Historical success rates (lower success rate = higher penalty)
- Retry count (more retries = diminishing returns)
"""

from uuid import UUID

from core.ports.reward_mechanism_port import (
    RecollectionContext,
    RecollectionReason,
    RecollectionResult,
    RewardMechanismPort,
)


class HeuristicRewardMechanismAdapter:
    """Heuristic-based implementation of RewardMechanismPort.

    This adapter uses configurable base ratios and applies heuristic modifiers
    based on context. The default values are placeholders to be tuned.

    Default heuristic ratios:
    - reward_ratio: 0.2 (supervisor gains 20% of allocated budget on success)
    - penalty_ratio: 0.1 (supervisor loses 10% of allocated budget on failure)
    - termination_ratio: 0.0 (no reward/penalty for terminated siblings)

    Heuristic modifiers (applied multiplicatively):
    - complexity_factor: Higher complexity increases stakes (1.0 + complexity * 0.5)
    - efficiency_factor: Better budget efficiency increases reward (1.0 + (1 - utilization) * 0.3)
    - retry_factor: More retries decrease reward (1.0 / (1 + retry_count * 0.2))
    - success_rate_factor: Lower success rate increases penalty (1.0 + (1 - success_rate) * 0.4)
    """

    def __init__(
        self,
        base_reward_ratio: float = 0.2,
        base_penalty_ratio: float = 0.1,
        base_termination_ratio: float = 0.0,
        enable_adaptive_modifiers: bool = True,
    ) -> None:
        """Initialize the heuristic reward mechanism.

        Args:
            base_reward_ratio: Base ratio for successful completions (default: 0.2).
            base_penalty_ratio: Base ratio for failed completions (default: 0.1).
            base_termination_ratio: Base ratio for terminated siblings (default: 0.0).
            enable_adaptive_modifiers: Whether to apply context-based modifiers.
        """
        self._base_reward_ratio = base_reward_ratio
        self._base_penalty_ratio = base_penalty_ratio
        self._base_termination_ratio = base_termination_ratio
        self._enable_adaptive = enable_adaptive_modifiers

    def get_reward_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the reward ratio for a successful subordinate.

        Per flowchart: S Increases Budget by PLUS Reward_Ratio * X_Y2

        Args:
            agent_id: UUID of the supervisor agent (unused in base implementation).
            child_id: UUID of the successful child agent (unused in base implementation).
            context: Optional context for adaptive ratio calculation.

        Returns:
            The reward ratio to apply.
        """
        ratio = self._base_reward_ratio

        if self._enable_adaptive and context:
            # Higher complexity = higher reward potential
            complexity_factor = 1.0 + context.task_complexity * 0.5
            # Better budget efficiency = higher reward
            efficiency_factor = 1.0 + (1 - context.budget_utilization) * 0.3
            # More retries = diminishing returns
            retry_factor = 1.0 / (1 + context.retry_count * 0.2)

            ratio *= complexity_factor * efficiency_factor * retry_factor

        return ratio

    def get_penalty_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the penalty ratio for a failed subordinate.

        Per flowchart: S Decreases Budget by MINUS Penalty_Ratio * X_Y2

        Args:
            agent_id: UUID of the supervisor agent (unused in base implementation).
            child_id: UUID of the failed child agent (unused in base implementation).
            context: Optional context for adaptive ratio calculation.

        Returns:
            The penalty ratio to apply.
        """
        ratio = self._base_penalty_ratio

        if self._enable_adaptive and context:
            # Higher complexity = higher penalty potential
            complexity_factor = 1.0 + context.task_complexity * 0.5
            # Lower historical success rate = higher penalty
            success_rate_factor = 1.0 + (1 - context.historical_success_rate) * 0.4
            # More parallel siblings = lower individual penalty (risk was distributed)
            sibling_factor = 1.0 / max(1, context.sibling_count * 0.5)

            ratio *= complexity_factor * success_rate_factor * sibling_factor

        return ratio

    def get_termination_ratio(
        self,
        agent_id: UUID,
        child_id: UUID,
        context: RecollectionContext | None = None,
    ) -> float:
        """Get the ratio for a terminated (unfinished) subordinate.

        Default: 0.0 (no reward/penalty for terminated siblings)

        Args:
            agent_id: UUID of the supervisor agent (unused).
            child_id: UUID of the terminated child agent (unused).
            context: Optional context (unused for termination).

        Returns:
            The termination ratio (always 0.0 in this implementation).
        """
        return self._base_termination_ratio

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

        Per the flowchart:
        - SUCCESS: supervisor.budget += reward_ratio * allocated_budget
        - FAILURE: supervisor.budget -= penalty_ratio * allocated_budget
        - TERMINATED: no change (ratio = 0.0)

        The remaining_budget is always returned to the supervisor, then the
        reward/penalty is applied as an additional adjustment.

        Args:
            agent_id: UUID of the supervisor agent.
            child_id: UUID of the child agent.
            allocated_budget: Budget originally allocated to the child (X_Y2).
            remaining_budget: Child's remaining budget at completion.
            child_succeeded: Whether the child completed successfully.
            child_failed: Whether the child failed (distinct from terminated).
            context: Optional context for adaptive ratio calculation.

        Returns:
            RecollectionResult with:
            - amount_recollected: remaining_budget + (ratio * allocated_budget)
            - For success: positive bonus
            - For failure: negative penalty subtracted from remaining
        """
        if child_succeeded:
            ratio = self.get_reward_ratio(agent_id, child_id, context)
            reason = RecollectionReason.SUCCESS
            # Success: return remaining + bonus
            bonus = ratio * allocated_budget
            amount = remaining_budget + bonus
            reason_text = (
                f"Success: recollected {remaining_budget:.2f} remaining "
                f"+ {bonus:.2f} reward ({ratio:.2%} of {allocated_budget:.2f})"
            )
        elif child_failed:
            ratio = self.get_penalty_ratio(agent_id, child_id, context)
            reason = RecollectionReason.FAILURE
            # Failure: return remaining - penalty
            penalty = ratio * allocated_budget
            amount = remaining_budget - penalty
            reason_text = (
                f"Failure: recollected {remaining_budget:.2f} remaining "
                f"- {penalty:.2f} penalty ({ratio:.2%} of {allocated_budget:.2f})"
            )
        else:
            # Terminated (sibling succeeded first)
            ratio = self.get_termination_ratio(agent_id, child_id, context)
            reason = RecollectionReason.TERMINATED
            adjustment = ratio * allocated_budget
            amount = remaining_budget + adjustment
            reason_text = (
                f"Terminated: recollected {remaining_budget:.2f} remaining "
                f"(sibling succeeded first)"
            )

        return RecollectionResult(
            original_allocation=allocated_budget,
            remaining_budget=remaining_budget,
            ratio_applied=ratio,
            amount_recollected=amount,
            reason=reason_text,
            recollection_reason=reason,
        )


# Type assertion for Protocol compliance
def _assert_protocol_compliance() -> None:
    """Static assertion that adapter implements the port protocol."""
    _adapter: RewardMechanismPort = HeuristicRewardMechanismAdapter()
