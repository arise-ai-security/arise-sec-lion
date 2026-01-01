"""Complexity budget enforcement steps (Design Choices 2-3: Budgeted Tree).

These steps control tree growth via budget allocation:
- CheckBudgetThreshold: Force WORKER if budget below threshold (soft enforcement)
- ProbabilisticWorkerShortcut: Russian Roulette - random chance to become WORKER

Note: This is complexity budget (tree growth control), NOT financial cost.
"""

import random
from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from config.settings import OrchestrationConfig


class CheckBudgetThreshold:
    """Check if agent's complexity budget is below threshold (Design Choice 2).

    If enabled and budget < initial_amount * threshold_ratio, forces WORKER role.
    This is soft enforcement - the agent becomes a worker instead of spawning children.

    This step should be placed AFTER validation but BEFORE the LLM call.
    When budget is below threshold, it sets complexity result and the remaining
    steps (BuildPrompt, QueryLLM, etc.) are skipped via the complexity result.

    Note: The ApplyComplexityResult step checks if complexity is already set
    and skips re-application to avoid duplicate events.
    """

    def __init__(self, budget_config: "OrchestrationConfig.ComplexityBudgetConfig") -> None:
        """Initialize with budget configuration.

        Args:
            budget_config: Configuration for complexity budget thresholds.
        """
        self._config = budget_config

    async def execute(self, state: PipelineState) -> StepResult:
        """Check if budget is below threshold, force WORKER if so."""
        if not self._config.is_enabled():
            # Complexity budget disabled, use structural limits instead
            return StepResult.ok(state)

        agent = state.agent

        # Check if budget was allocated
        if agent.complexity_budget <= 0:
            # No budget allocated, use structural limits
            return StepResult.ok(state)

        # Check threshold
        if agent.is_below_budget_threshold(
            initial_boss_budget=self._config.initial_amount,
            threshold_ratio=self._config.threshold_ratio,
        ):
            # Budget below threshold - force WORKER role
            agent.emit_limit_enforced(
                limit_type="complexity_budget",
                limit_value=int(self._config.initial_amount * self._config.threshold_ratio),
                attempted_value=int(agent.complexity_budget),
                action_taken="forced_worker_role",
            )

            # Apply complexity result directly as WORKER (short-circuit LLM call)
            agent.apply_complexity_result(
                complexity="simple",
                reasoning="Budget below threshold - forced WORKER role",
                determined_role=AgentRole.WORKER,
            )

            # Set the complexity result in state so ApplyComplexityResult knows to skip
            return StepResult.ok(
                state.with_complexity_result(
                    complexity="simple",
                    reasoning="Budget below threshold - forced WORKER role",
                )
            )

        return StepResult.ok(state)


class ProbabilisticWorkerShortcut:
    """Russian Roulette: random chance to become WORKER (Design Choice 3).

    With configured probability, forces agent to become WORKER without
    evaluating complexity. This adds randomness to tree growth, preventing
    over-expansion while maintaining exploration capability.

    This step should run BEFORE CheckBudgetThreshold to provide an
    early shortcut opportunity.
    """

    def __init__(self, budget_config: "OrchestrationConfig.ComplexityBudgetConfig") -> None:
        """Initialize with budget configuration.

        Args:
            budget_config: Configuration containing shortcut_probability.
        """
        self._config = budget_config

    async def execute(self, state: PipelineState) -> StepResult:
        """Roll dice to potentially shortcut agent to WORKER role."""
        if not self._config.is_enabled():
            # Complexity budget disabled, skip shortcut
            return StepResult.ok(state)

        # Skip shortcut for BOSS (root agent should always decompose)
        agent = state.agent
        if agent.parent_id is None:
            return StepResult.ok(state)

        # Russian Roulette: random chance to become WORKER
        if random.random() < self._config.shortcut_probability:
            # Shortcut triggered - force WORKER role
            agent.emit_limit_enforced(
                limit_type="probabilistic_shortcut",
                limit_value=int(self._config.shortcut_probability * 100),  # As percentage
                attempted_value=0,
                action_taken="forced_worker_role",
            )

            # Apply complexity result directly as WORKER (short-circuit LLM call)
            agent.apply_complexity_result(
                complexity="simple",
                reasoning="Russian Roulette shortcut - randomly selected as WORKER",
                determined_role=AgentRole.WORKER,
            )

            # Set the complexity result in state so ApplyComplexityResult knows to skip
            return StepResult.ok(
                state.with_complexity_result(
                    complexity="simple",
                    reasoning="Russian Roulette shortcut - randomly selected as WORKER",
                )
            )

        return StepResult.ok(state)
