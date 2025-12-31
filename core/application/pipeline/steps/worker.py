"""Worker execution steps for pipeline execution.

These steps handle worker tool execution (Claude Code, OpenHands, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.exceptions import ToolNotAvailableError

if TYPE_CHECKING:
    from core.ports.worker_port import WorkerToolPort


class RunWorkerSession:
    """Run worker tool session and apply events.

    Executes the task via worker_port.run_session() and applies
    each tool event to the agent. This is the main execution step
    for WORKER agents.
    """

    def __init__(self, worker_port: WorkerToolPort) -> None:
        """Initialize with worker port.

        Args:
            worker_port: Port for worker tool execution
        """
        self._worker_port = worker_port

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Execute worker session and apply events."""
        agent = ctx.agent

        if ctx.prompt is None:
            return StepResult.fail("No prompt set in context")

        task_context: dict[str, Any] = {
            "agent_id": agent.agent_id,
            "task_description": ctx.prompt,
            "tool_name": agent.config.tool,
            "config": agent.config,
        }

        if ctx.working_directory:
            task_context["working_directory"] = ctx.working_directory

        try:
            async for tool_event in self._worker_port.run_session(task_context):
                agent.apply_worker_event(tool_event)
        except ToolNotAvailableError as e:
            # Only catch ToolNotAvailableError - let other exceptions propagate
            # to ExecutionService which handles them via _handle_step_failure
            return StepResult.fail(str(e))

        return StepResult.ok(ctx)
