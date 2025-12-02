"""Tests for HeuristicRewardMechanismAdapter.

These tests verify the heuristic-based reward mechanism implementation
following the flowchart semantics:
- SUCCESS: supervisor.budget += remaining + (reward_ratio * allocated)
- FAILURE: supervisor.budget += remaining - (penalty_ratio * allocated)
"""

from uuid import uuid4

import pytest

from core.ports.reward_mechanism_port import RecollectionContext, RecollectionReason
from infrastructure.adapters.heuristic_reward_adapter import HeuristicRewardMechanismAdapter


class TestHeuristicRewardMechanismAdapter:
    """Test suite for HeuristicRewardMechanismAdapter."""

    @pytest.fixture
    def adapter(self) -> HeuristicRewardMechanismAdapter:
        """Create adapter with default settings."""
        return HeuristicRewardMechanismAdapter()

    @pytest.fixture
    def adapter_fixed(self) -> HeuristicRewardMechanismAdapter:
        """Create adapter with fixed (non-adaptive) ratios."""
        return HeuristicRewardMechanismAdapter(enable_adaptive_modifiers=False)

    @pytest.fixture
    def agent_id(self) -> uuid4:
        """Create unique agent ID."""
        return uuid4()

    @pytest.fixture
    def child_id(self) -> uuid4:
        """Create unique child ID."""
        return uuid4()

    # ========================================================================
    # Basic Ratio Tests (Fixed Mode)
    # ========================================================================

    def test_default_reward_ratio(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test default reward ratio is 0.2 (20% of allocated budget)."""
        ratio = adapter_fixed.get_reward_ratio(agent_id, child_id)
        assert ratio == 0.2

    def test_default_penalty_ratio(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test default penalty ratio is 0.1 (10% of allocated budget)."""
        ratio = adapter_fixed.get_penalty_ratio(agent_id, child_id)
        assert ratio == 0.1

    def test_default_termination_ratio(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test default termination ratio is 0.0 (no reward/penalty)."""
        ratio = adapter_fixed.get_termination_ratio(agent_id, child_id)
        assert ratio == 0.0

    def test_custom_base_ratios(self, agent_id: uuid4, child_id: uuid4) -> None:
        """Test custom base ratios are respected."""
        adapter = HeuristicRewardMechanismAdapter(
            base_reward_ratio=0.3,
            base_penalty_ratio=0.15,
            base_termination_ratio=0.05,
            enable_adaptive_modifiers=False,
        )

        assert adapter.get_reward_ratio(agent_id, child_id) == 0.3
        assert adapter.get_penalty_ratio(agent_id, child_id) == 0.15
        assert adapter.get_termination_ratio(agent_id, child_id) == 0.05

    # ========================================================================
    # Budget Recollection Calculation Tests
    # ========================================================================

    def test_success_recollection_returns_remaining_plus_bonus(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test success recollection: remaining + (reward_ratio * allocated).

        Per flowchart: S Increases Budget by PLUS Reward_Ratio * X_Y2
        """
        allocated = 100.0
        remaining = 60.0  # 40 consumed
        reward_ratio = 0.2  # Default

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated,
            remaining_budget=remaining,
            child_succeeded=True,
            child_failed=False,
        )

        # Expected: 60 + (0.2 * 100) = 60 + 20 = 80
        expected_amount = remaining + (reward_ratio * allocated)
        assert result.amount_recollected == expected_amount
        assert result.ratio_applied == reward_ratio
        assert result.recollection_reason == RecollectionReason.SUCCESS
        assert "Success" in result.reason
        assert "reward" in result.reason.lower()

    def test_failure_recollection_returns_remaining_minus_penalty(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test failure recollection: remaining - (penalty_ratio * allocated).

        Per flowchart: S Decreases Budget by MINUS Penalty_Ratio * X_Y2
        """
        allocated = 100.0
        remaining = 60.0  # 40 consumed
        penalty_ratio = 0.1  # Default

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated,
            remaining_budget=remaining,
            child_succeeded=False,
            child_failed=True,
        )

        # Expected: 60 - (0.1 * 100) = 60 - 10 = 50
        expected_amount = remaining - (penalty_ratio * allocated)
        assert result.amount_recollected == expected_amount
        assert result.ratio_applied == penalty_ratio
        assert result.recollection_reason == RecollectionReason.FAILURE
        assert "Failure" in result.reason
        assert "penalty" in result.reason.lower()

    def test_termination_recollection_returns_remaining_only(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test terminated sibling recollection: remaining only (no bonus/penalty)."""
        allocated = 100.0
        remaining = 80.0  # 20 consumed before termination

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated,
            remaining_budget=remaining,
            child_succeeded=False,
            child_failed=False,  # Not failed, just terminated
        )

        # Expected: 80 + (0.0 * 100) = 80
        assert result.amount_recollected == remaining
        assert result.ratio_applied == 0.0
        assert result.recollection_reason == RecollectionReason.TERMINATED
        assert "Terminated" in result.reason

    def test_failure_with_zero_remaining_results_in_net_loss(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test failure when child depleted budget results in negative recollection."""
        allocated = 100.0
        remaining = 0.0  # Fully depleted
        penalty_ratio = 0.1

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated,
            remaining_budget=remaining,
            child_succeeded=False,
            child_failed=True,
        )

        # Expected: 0 - (0.1 * 100) = -10 (net loss to supervisor)
        expected_amount = remaining - (penalty_ratio * allocated)
        assert result.amount_recollected == expected_amount
        assert result.amount_recollected == -10.0
        assert result.recollection_reason == RecollectionReason.FAILURE

    # ========================================================================
    # Adaptive Modifier Tests
    # ========================================================================

    def test_high_complexity_increases_reward_ratio(
        self, adapter: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test that higher task complexity increases reward ratio."""
        base_ratio = adapter.get_reward_ratio(agent_id, child_id, context=None)

        high_complexity_context = RecollectionContext(task_complexity=1.0)
        high_ratio = adapter.get_reward_ratio(agent_id, child_id, context=high_complexity_context)

        # complexity_factor = 1.0 + 1.0 * 0.5 = 1.5
        # With default efficiency (1.0) and retry (1.0): ratio * 1.5
        assert high_ratio > base_ratio

    def test_low_efficiency_increases_reward(
        self, adapter: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test that lower budget utilization (more efficient) increases reward."""
        # Low utilization = efficient (didn't consume much budget)
        efficient_context = RecollectionContext(budget_utilization=0.2)  # Only used 20%
        efficient_ratio = adapter.get_reward_ratio(agent_id, child_id, context=efficient_context)

        # High utilization = inefficient
        inefficient_context = RecollectionContext(budget_utilization=0.9)  # Used 90%
        inefficient_ratio = adapter.get_reward_ratio(agent_id, child_id, context=inefficient_context)

        assert efficient_ratio > inefficient_ratio

    def test_retries_diminish_reward(
        self, adapter: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test that more retries result in lower reward ratio."""
        no_retry_context = RecollectionContext(retry_count=0)
        no_retry_ratio = adapter.get_reward_ratio(agent_id, child_id, context=no_retry_context)

        many_retry_context = RecollectionContext(retry_count=5)
        many_retry_ratio = adapter.get_reward_ratio(agent_id, child_id, context=many_retry_context)

        assert no_retry_ratio > many_retry_ratio

    def test_low_success_rate_increases_penalty(
        self, adapter: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test that lower historical success rate increases penalty."""
        high_success_context = RecollectionContext(historical_success_rate=0.9)
        high_success_penalty = adapter.get_penalty_ratio(agent_id, child_id, context=high_success_context)

        low_success_context = RecollectionContext(historical_success_rate=0.1)
        low_success_penalty = adapter.get_penalty_ratio(agent_id, child_id, context=low_success_context)

        assert low_success_penalty > high_success_penalty

    def test_more_siblings_reduce_penalty(
        self, adapter: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test that more parallel siblings reduces individual penalty (distributed risk)."""
        single_sibling_context = RecollectionContext(sibling_count=1)
        single_penalty = adapter.get_penalty_ratio(agent_id, child_id, context=single_sibling_context)

        many_siblings_context = RecollectionContext(sibling_count=3)
        many_penalty = adapter.get_penalty_ratio(agent_id, child_id, context=many_siblings_context)

        assert many_penalty < single_penalty

    # ========================================================================
    # Flowchart Scenario Tests
    # ========================================================================

    def test_flowchart_scenario_success_increases_supervisor_budget(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test the flowchart scenario: SUCCESS increases supervisor budget.

        Scenario from flowchart:
        - Supervisor S has budget X
        - Allocates X_Y2 to subordinate for task Y2
        - Subordinate succeeds
        - S increases budget by: PLUS Reward_Ratio * X_Y2
        """
        supervisor_initial_budget = 1000.0
        allocated_to_child = 333.0
        remaining_in_child = 200.0  # Child used 133

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated_to_child,
            remaining_budget=remaining_in_child,
            child_succeeded=True,
            child_failed=False,
        )

        # After allocation, supervisor had: 1000 - 333 = 667
        # On success, supervisor gets: 200 + (0.2 * 333) = 200 + 66.6 = 266.6
        # Final budget: 667 + 266.6 = 933.6
        supervisor_after_allocation = supervisor_initial_budget - allocated_to_child
        supervisor_final = supervisor_after_allocation + result.amount_recollected

        # Verify supervisor gained net budget
        expected_reward_bonus = 0.2 * allocated_to_child  # 66.6
        assert result.amount_recollected == remaining_in_child + expected_reward_bonus
        assert supervisor_final > supervisor_after_allocation

    def test_flowchart_scenario_failure_decreases_supervisor_budget(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test the flowchart scenario: FAILURE decreases supervisor budget.

        Scenario from flowchart:
        - Supervisor S has budget X
        - Allocates X_Y2 to subordinate for task Y2
        - Subordinate fails
        - S decreases budget by: MINUS Penalty_Ratio * X_Y2
        """
        supervisor_initial_budget = 1000.0
        allocated_to_child = 333.0
        remaining_in_child = 50.0  # Child used most of budget before failing

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated_to_child,
            remaining_budget=remaining_in_child,
            child_succeeded=False,
            child_failed=True,
        )

        # After allocation, supervisor had: 1000 - 333 = 667
        # On failure, supervisor gets: 50 - (0.1 * 333) = 50 - 33.3 = 16.7
        # Final budget: 667 + 16.7 = 683.7
        supervisor_after_allocation = supervisor_initial_budget - allocated_to_child
        supervisor_final = supervisor_after_allocation + result.amount_recollected

        expected_penalty = 0.1 * allocated_to_child  # 33.3
        assert result.amount_recollected == remaining_in_child - expected_penalty
        # Supervisor still lost money overall compared to initial
        assert supervisor_final < supervisor_initial_budget

    def test_flowchart_scenario_budget_depleted_ends_lifecycle(
        self, adapter_fixed: HeuristicRewardMechanismAdapter, agent_id: uuid4, child_id: uuid4
    ) -> None:
        """Test scenario where failure penalty pushes budget to negative.

        Per flowchart: Budget <= 0 → END of Supervisor Node S Lifecycle
        """
        supervisor_budget = 100.0  # Small budget
        allocated_to_child = 100.0  # All budget allocated
        remaining_in_child = 0.0  # Child depleted all

        result = adapter_fixed.calculate_recollection(
            agent_id=agent_id,
            child_id=child_id,
            allocated_budget=allocated_to_child,
            remaining_budget=remaining_in_child,
            child_succeeded=False,
            child_failed=True,
        )

        # After allocation: 100 - 100 = 0
        # On failure: 0 - (0.1 * 100) = -10
        # Final: 0 + (-10) = -10 → Budget <= 0 → END
        supervisor_after_allocation = supervisor_budget - allocated_to_child
        supervisor_final = supervisor_after_allocation + result.amount_recollected

        assert result.amount_recollected == -10.0
        assert supervisor_final <= 0, "Budget should be depleted, ending lifecycle"


class TestRecollectionContext:
    """Test suite for RecollectionContext dataclass."""

    def test_default_values(self) -> None:
        """Test RecollectionContext has sensible defaults."""
        context = RecollectionContext()

        assert context.task_complexity == 0.0
        assert context.subtree_depth == 0
        assert context.time_elapsed == 0.0
        assert context.budget_utilization == 0.0
        assert context.retry_count == 0
        assert context.sibling_count == 1
        assert context.historical_success_rate == 1.0
        assert context.metadata == {}

    def test_custom_values(self) -> None:
        """Test RecollectionContext accepts custom values."""
        context = RecollectionContext(
            task_complexity=0.8,
            subtree_depth=3,
            time_elapsed=120.5,
            budget_utilization=0.75,
            retry_count=2,
            sibling_count=3,
            historical_success_rate=0.6,
            metadata={"custom": "value"},
        )

        assert context.task_complexity == 0.8
        assert context.subtree_depth == 3
        assert context.time_elapsed == 120.5
        assert context.budget_utilization == 0.75
        assert context.retry_count == 2
        assert context.sibling_count == 3
        assert context.historical_success_rate == 0.6
        assert context.metadata == {"custom": "value"}

    def test_frozen_immutability(self) -> None:
        """Test RecollectionContext is frozen (immutable)."""
        context = RecollectionContext(task_complexity=0.5)

        with pytest.raises(Exception):  # FrozenInstanceError or similar
            context.task_complexity = 0.9  # type: ignore[misc]
