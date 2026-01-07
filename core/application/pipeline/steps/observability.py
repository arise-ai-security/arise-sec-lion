"""Observability steps for pipeline execution.

These steps emit events for monitoring, cost tracking, and debugging.
They call pure domain methods on AgentSession to emit events.
"""

import time

from dataclasses import replace

from core.application.pipeline.context import PipelineState, StepResult


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

    async def execute(self, state: PipelineState) -> StepResult:
        """Emit PromptSent event via agent's pure domain method."""
        if state.prompt is None:
            return StepResult.fail("No prompt set in context")

        target = self._target
        # For worker execution, use the actual tool name
        if self._target == "dynamic":
            target = state.agent.config.tool

        state.agent.emit_prompt_sent(
            prompt=state.prompt,
            prompt_type=self._prompt_type,
            target=target,
        )

        return StepResult.ok(state)


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

    async def execute(self, state: PipelineState) -> StepResult:
        """Emit TokensConsumed event from LLM response."""
        if state.llm_response is None:
            return StepResult.fail("No LLM response in context")

        response = state.llm_response
        state.agent.emit_tokens_consumed(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost_usd=response.cost_usd,
            operation=self._operation,
        )

        return StepResult.ok(state)


class EmitOperationStarted:
    """Emit OperationStarted event and store start time.

    Records when an LLM operation begins for duration tracking.
    Stores start time in pipeline state for later calculation.
    """

    def __init__(self, operation_type: str) -> None:
        """Initialize with operation type.

        Args:
            operation_type: Type of operation (complexity_evaluation,
                           task_decomposition, worker_execution)
        """
        self._operation_type = operation_type

    async def execute(self, state: PipelineState) -> StepResult:
        """Emit OperationStarted event and store start time."""
        start_time = time.monotonic()

        state.agent.emit_operation_started(operation_type=self._operation_type)

        # Store start time in state for duration calculation
        new_state = replace(state, operation_start_time=start_time)
        return StepResult.ok(new_state)


class EmitOperationFinished:
    """Emit OperationFinished event with calculated duration.

    Records when an LLM operation completes with duration.
    Uses start time from pipeline state to calculate elapsed time.
    """

    def __init__(self, operation_type: str) -> None:
        """Initialize with operation type.

        Args:
            operation_type: Type of operation (complexity_evaluation,
                           task_decomposition, worker_execution)
        """
        self._operation_type = operation_type

    async def execute(self, state: PipelineState) -> StepResult:
        """Emit OperationFinished event with duration."""
        if state.operation_start_time is None:
            # No start time recorded, use 0 duration
            duration = 0.0
        else:
            duration = time.monotonic() - state.operation_start_time

        state.agent.emit_operation_finished(
            operation_type=self._operation_type,
            duration_seconds=duration,
        )

        # Clear start time from state
        new_state = replace(state, operation_start_time=None)
        return StepResult.ok(new_state)
