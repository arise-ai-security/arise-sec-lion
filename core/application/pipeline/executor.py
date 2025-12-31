"""Pipeline executor.

Executes a sequence of steps, short-circuiting on first failure.
Follows Chain of Responsibility pattern with explicit step ordering.
Uses PipelineState for state passing between steps.
"""



import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.pipeline.context import PipelineState, StepResult
    from core.application.pipeline.protocol import PipelineStep

logger = logging.getLogger(__name__)


class Pipeline:
    """Execute a sequence of steps, short-circuiting on failure.

    The pipeline:
    1. Takes an initial state
    2. Passes it through each step in order
    3. Each step returns success (with updated state) or failure
    4. On first failure, immediately returns that failure
    5. On success of all steps, returns final state

    Usage:
        pipeline = Pipeline(
            name="complexity_evaluation",
            steps=[
                ValidatePendingAgent(),
                BuildComplexityPrompt(prompt_builder),
                EmitPromptSent("complexity_evaluation"),
                QueryLLM(llm_port, "complexity_evaluation"),
                EmitTokensConsumed("complexity_evaluation"),
                ParseComplexityResult(),
                ApplyComplexityResult(),
            ],
        )

        result = await pipeline.execute(initial_state)
        if not result.success:
            agent.fail_with_reason(result.failure_reason)
    """

    def __init__(self, steps: "list[PipelineStep]", name: str = "") -> None:
        """Initialize pipeline with steps.

        Args:
            steps: Ordered list of steps to execute
            name: Optional name for logging and debugging
        """
        self._steps = steps
        self._name = name

    @property
    def name(self) -> str:
        """Pipeline name for logging."""
        return self._name

    async def execute(self, initial_state: "PipelineState") -> "StepResult":
        """Execute all steps in order.

        Short-circuits on first failure, returning that failure result.
        On success, returns final state after all steps.

        Args:
            initial_state: Starting state for the pipeline

        Returns:
            StepResult with either success (final state) or failure (reason)
        """
        from core.application.pipeline.context import StepResult

        state = initial_state

        for step in self._steps:
            step_name = type(step).__name__

            # Let exceptions propagate - ExecutionService handles them
            # via _handle_step_failure for proper error reporting
            result = await step.execute(state)

            if not result.success:
                logger.debug(
                    "Step %s failed in pipeline %s: %s",
                    step_name,
                    self._name,
                    result.failure_reason,
                )
                return result

            # Update state for next step
            # On success, state should always be set
            if result.state is None:
                return StepResult.fail(f"Step {step_name} returned success but no state")
            state = result.state

        return StepResult.ok(state)
