"""Verification heuristics port definition.

This module defines the abstract interface for verification task injection decisions.
Infrastructure implementations provide heuristic strategies for when to trigger
verification of completed work to catch potential false negatives.

Verification Task Strategy (from description):
The supervisor injects verification tasks based on heuristics:
1. Complexity of Previous Task: if the sub-tree is large and task is complex
2. Random Probability: a small probability to randomly inject verification tasks
3. Suspicious Reports: if reported edits don't match task complexity
4. Time Since Last Verification: if too long since last verification task
5. Budget Availability: if sufficient budget to verify without jeopardizing main tasks

Note: Recursive verification is possible but the heuristics make it less likely.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from core.domain.subtask import Subtask


@dataclass(frozen=True)
class VerificationDecision:
    """Result of verification heuristic evaluation.

    Attributes:
        should_verify: Whether to inject a verification task.
        reason: Human-readable explanation of the decision.
        estimated_cost: Expected budget cost for verification.
        complexity_score: Estimated complexity of the completed subtask (0.0-1.0).
        suspicion_score: How suspicious the completion appears (0.0-1.0).
        random_triggered: Whether random probability triggered verification.
    """

    should_verify: bool
    reason: str
    estimated_cost: float = 0.0
    complexity_score: float = 0.0
    suspicion_score: float = 0.0
    random_triggered: bool = False


@dataclass(frozen=True)
class CompletionContext:
    """Context about a completed subtask for verification decision.

    Attributes:
        subtask: The subtask that was completed.
        child_id: The child agent that completed it.
        child_role: Role of the child agent (worker/manager).
        reported_result: The result reported by the child.
        edits_count: Number of edits/changes made by the child.
        execution_time: Time taken to complete (seconds).
        subtree_size: Number of agents in the child's subtree.
        child_budget_used: Budget consumed by the child.
        child_budget_remaining: Budget remaining for the child.
    """

    subtask: Subtask
    child_id: UUID
    child_role: str
    reported_result: str
    edits_count: int = 0
    execution_time: float = 0.0
    subtree_size: int = 1
    child_budget_used: float = 0.0
    child_budget_remaining: float = 0.0


class VerificationHeuristicsPort(Protocol):
    """Abstract interface for verification task injection decisions.

    The verification heuristics determine whether to inject a verification task
    after a subtask is completed. This helps catch false negatives where an
    agent reports success but the work is actually incomplete or incorrect.

    Verification is expensive (uses advanced models like claude-sonnet-4-5),
    so heuristics balance thoroughness against budget consumption.

    Implementations can provide different strategies:
    - Aggressive: High probability of verification for critical tasks
    - Conservative: Only verify when highly suspicious
    - Random: Small random chance on all completions
    - Adaptive: Learn from past verification results
    """

    def should_verify(
        self,
        supervisor_id: UUID,
        context: CompletionContext,
        available_budget: float,
        time_since_last_verification: float,
    ) -> VerificationDecision:
        """Decide whether to inject a verification task.

        This is the main decision method that evaluates heuristics to determine
        if a completed subtask should be verified.

        Args:
            supervisor_id: UUID of the supervisor making the decision.
            context: Context about the completed subtask.
            available_budget: Budget available for verification.
            time_since_last_verification: Seconds since last verification.

        Returns:
            VerificationDecision with the decision and reasoning.
        """
        ...

    def estimate_verification_cost(
        self,
        subtask: Subtask,
        subtree_size: int,
    ) -> float:
        """Estimate the budget cost of verifying a subtask.

        Args:
            subtask: The subtask to be verified.
            subtree_size: Size of the subtree for context.

        Returns:
            Estimated budget cost for verification.
        """
        ...

    def get_random_verification_probability(self) -> float:
        """Get the base probability for random verification injection.

        Default: 0.1 (10% random chance)

        Returns:
            Probability between 0.0 and 1.0.
        """
        ...

    def get_min_budget_for_verification(self) -> float:
        """Get the minimum budget required to consider verification.

        Verification won't be triggered if available budget is below this.

        Returns:
            Minimum budget threshold.
        """
        ...

    def get_max_time_between_verifications(self) -> float:
        """Get the maximum time (seconds) allowed between verifications.

        If more time has passed, verification becomes more likely.

        Returns:
            Maximum time in seconds.
        """
        ...

    def calculate_suspicion_score(
        self,
        context: CompletionContext,
    ) -> float:
        """Calculate how suspicious a completion appears.

        Checks for mismatches between reported results and expected outcomes:
        - Edits count vs task complexity
        - Execution time vs expected duration
        - Result length/detail vs task requirements

        Args:
            context: Context about the completed subtask.

        Returns:
            Suspicion score between 0.0 (not suspicious) and 1.0 (highly suspicious).
        """
        ...

    def get_verifier_config(
        self,
        subtask: Subtask,
        original_child_config: dict,
    ) -> dict:
        """Get the configuration for the verifier agent.

        The verifier should be different from the original agent, typically
        a more advanced model with deep-thinking capabilities.

        Args:
            subtask: The subtask to be verified.
            original_child_config: Config of the agent that completed the task.

        Returns:
            Configuration dict for the verifier agent.
        """
        ...
