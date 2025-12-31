"""LLM interaction step for pipeline execution.

This step handles the actual LLM call using the injected LLM port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.services.config_resolver import ConfigResolver, OperationType

if TYPE_CHECKING:
    from core.ports.llm_port import LLMPort


class QueryLLM:
    """Execute LLM query with proper config resolution.

    Uses ConfigResolver to get operation-specific LLM configuration,
    then calls the LLM port and stores the response in context.
    """

    _llm_port: LLMPort
    _operation: OperationType

    def __init__(self, llm_port: LLMPort, operation: OperationType) -> None:
        """Initialize with LLM port and operation type.

        Args:
            llm_port: Port for LLM interactions
            operation: Operation type for config resolution
                      (complexity_evaluation, task_decomposition)
        """
        self._llm_port = llm_port
        self._operation = operation

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Query LLM and store response in context.

        Note: This step does NOT catch LLM exceptions. Errors from the LLM
        service should propagate to the caller (ExecutionService) which
        handles them via _handle_step_failure.
        """
        if ctx.prompt is None:
            return StepResult.fail("No prompt set in context")

        llm_config = ConfigResolver.resolve(ctx.agent.config, operation=self._operation)

        # Let exceptions propagate - ExecutionService handles them
        response = await self._llm_port.query_with_usage(
            ctx.prompt,
            llm_config.model_dump(),
        )

        return StepResult.ok(ctx.with_llm_response(response))
