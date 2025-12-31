"""Pipeline step protocol.

Defines the interface that all pipeline steps must implement.
Uses Python's Protocol for structural typing (duck typing with type checking).
"""



from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.application.pipeline.context import PipelineState, StepResult


class PipelineStep(Protocol):
    """Protocol for pipeline steps.

    Each step receives state, performs one focused operation, and returns result.
    Steps should be:
    - Small and focused (Single Responsibility)
    - Independently testable
    - Side-effect aware (I/O vs pure logic)

    Implementation pattern:
        class MyStep:
            def __init__(self, some_port: SomePort) -> None:
                self._port = some_port

            async def execute(self, state: PipelineState) -> StepResult:
                # Perform operation
                result = await self._port.do_something(state.agent)
                # Return success with updated state or failure
                return StepResult.ok(state.with_some_field(result))
    """

    async def execute(self, state: "PipelineState") -> "StepResult":
        """Execute the step.

        Args:
            state: Current pipeline state with all accumulated data.

        Returns:
            StepResult.ok(updated_state) on success
            StepResult.fail(reason) on failure
        """
        ...
