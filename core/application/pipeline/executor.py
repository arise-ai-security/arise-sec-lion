"""Pipeline executor.

Executes a sequence of steps, short-circuiting on first failure.
Follows Chain of Responsibility pattern with explicit step ordering.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.pipeline.context import PipelineContext, StepResult
    from core.application.pipeline.protocol import PipelineStep

logger = logging.getLogger(__name__)


class Pipeline:
    """Execute a sequence of steps, short-circuiting on failure.

    The pipeline:
    1. Takes an initial context
    2. Passes it through each step in order
    3. Each step returns success (with updated context) or failure
    4. On first failure, immediately returns that failure
    5. On success of all steps, returns final context

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

        result = await pipeline.execute(initial_ctx)
        if not result.success:
            agent.fail_with_reason(result.failure_reason)
    """

    def __init__(self, steps: list[PipelineStep], name: str = "") -> None:
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

    async def execute(self, initial_ctx: PipelineContext) -> StepResult:
        """Execute all steps in order.

        Short-circuits on first failure, returning that failure result.
        On success, returns final context after all steps.

        Args:
            initial_ctx: Starting context for the pipeline

        Returns:
            StepResult with either success (final context) or failure (reason)
        """
        from core.application.pipeline.context import StepResult

        ctx = initial_ctx

        for step in self._steps:
            step_name = type(step).__name__

            # Let exceptions propagate - ExecutionService handles them
            # via _handle_step_failure for proper error reporting
            result = await step.execute(ctx)

            if not result.success:
                logger.debug(
                    "Step %s failed in pipeline %s: %s",
                    step_name,
                    self._name,
                    result.failure_reason,
                )
                return result

            # Update context for next step
            # On success, context should always be set
            if result.context is None:
                return StepResult.fail(f"Step {step_name} returned success but no context")
            ctx = result.context

        return StepResult.ok(ctx)
