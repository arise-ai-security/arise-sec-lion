"""Pipeline step protocol.

Defines the interface that all pipeline steps must implement.
Uses Python's Protocol for structural typing (duck typing with type checking).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.application.pipeline.context import PipelineContext, StepResult


class PipelineStep(Protocol):
    """Protocol for pipeline steps.

    Each step receives context, performs one focused operation, and returns result.
    Steps should be:
    - Small and focused (Single Responsibility)
    - Independently testable
    - Side-effect aware (I/O vs pure logic)

    Implementation pattern:
        class MyStep:
            def __init__(self, some_port: SomePort) -> None:
                self._port = some_port

            async def execute(self, ctx: PipelineContext) -> StepResult:
                # Perform operation
                result = await self._port.do_something(ctx.agent)
                # Return success with updated context or failure
                return StepResult.ok(ctx.with_some_field(result))
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Execute the step.

        Args:
            ctx: Current pipeline context with all accumulated state.

        Returns:
            StepResult.ok(updated_ctx) on success
            StepResult.fail(reason) on failure
        """
        ...
