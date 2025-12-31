"""Observability steps for pipeline execution.

These steps emit events for monitoring, cost tracking, and debugging.
They call pure domain methods on AgentSession to emit events.
"""

from __future__ import annotations

from core.application.pipeline.context import PipelineContext, StepResult


class EmitPromptSent:
    """Emit PromptSent event for observability.

    Records the prompt sent to LLM or worker tool for debugging and analysis.
    """

    def __init__(self, prompt_type: str, target: str = "llm") -> None:
        """Initialize with prompt metadata.

        Args:
            prompt_type: Type of prompt (complexity_evaluation, task_decomposition,
                        worker_execution)
            target: Where prompt is sent (llm, claude_code, openhands, etc.)
        """
        self._prompt_type = prompt_type
        self._target = target

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Emit PromptSent event via agent's pure domain method."""
        if ctx.prompt is None:
            return StepResult.fail("No prompt set in context")

        target = self._target
        # For worker execution, use the actual tool name
        if self._target == "dynamic":
            target = ctx.agent.config.tool

        ctx.agent.emit_prompt_sent(
            prompt=ctx.prompt,
            prompt_type=self._prompt_type,
            target=target,
        )

        return StepResult.ok(ctx)


class EmitTokensConsumed:
    """Emit TokensConsumed event for cost tracking.

    Records token usage and cost from LLM response for budget tracking.
    """

    def __init__(self, operation: str) -> None:
        """Initialize with operation name.

        Args:
            operation: Operation type for cost categorization
                      (complexity_evaluation, task_decomposition)
        """
        self._operation = operation

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Emit TokensConsumed event from LLM response."""
        if ctx.llm_response is None:
            return StepResult.fail("No LLM response in context")

        response = ctx.llm_response
        ctx.agent.emit_tokens_consumed(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost_usd=response.cost_usd,
            operation=self._operation,
        )

        return StepResult.ok(ctx)
