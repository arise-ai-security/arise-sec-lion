"""Worker execution steps for pipeline execution.

These steps handle worker tool execution (Claude Code, OpenHands, etc.).
"""


from typing import TYPE_CHECKING, Any

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.exceptions import ToolNotAvailableError

if TYPE_CHECKING:
    from core.ports.worker_port import WorkerToolPort
    from core.ports.realtime_callback_port import RealtimeCallbackPort


class RunWorkerSession:
    """Run worker tool session and apply events.

    Executes the task via worker_port.run_session() and applies
    each tool event to the agent. This is the main execution step
    for WORKER agents.

    Optionally streams events in real-time via realtime_callback.
    """

    def __init__(
        self,
        worker_port: "WorkerToolPort",
        realtime_callback: "RealtimeCallbackPort | None" = None,
    ) -> None:
        """Initialize with worker port.

        Args:
            worker_port: Port for worker tool execution
            realtime_callback: Optional callback for real-time event streaming
        """
        self._worker_port = worker_port
        self._realtime_callback = realtime_callback

    async def execute(self, state: PipelineState) -> StepResult:
        """Execute worker session and apply events."""
        agent = state.agent

        if state.prompt is None:
            return StepResult.fail("No prompt set in context")

        task_context: dict[str, Any] = {
            "agent_id": agent.agent_id,
            "task_description": state.prompt,
            "tool_name": agent.config.tool,
            "config": agent.config,
        }

        if state.working_directory:
            task_context["working_directory"] = state.working_directory

        # Get root_id for real-time streaming
        root_id = state.root_id

        try:
            async for tool_event in self._worker_port.run_session(task_context):
                agent.apply_worker_event(tool_event)

                # Stream event in real-time if callback is available
                if self._realtime_callback and root_id:
                    await self._realtime_callback.on_event(tool_event, root_id)
        except ToolNotAvailableError as e:
            # Only catch ToolNotAvailableError - let other exceptions propagate
            # to ExecutionService which handles them via _handle_step_failure
            return StepResult.fail(str(e))

        return StepResult.ok(state)
