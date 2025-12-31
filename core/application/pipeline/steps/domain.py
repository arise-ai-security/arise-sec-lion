"""Domain method invocation steps for pipeline execution.

These steps call pure domain methods on AgentSession to emit events.
They bridge the pipeline (application layer) to the domain model.
"""

from __future__ import annotations

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.enums import AgentRole


class ApplyComplexityResult:
    """Apply complexity evaluation result to agent.

    Calls agent.apply_complexity_result() which emits ComplexityEvaluated event
    and transitions the agent from PENDING to WORKER or MANAGER.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Apply complexity result via pure domain method."""
        if ctx.complexity is None:
            return StepResult.fail("No complexity result in context")

        determined_role = (
            AgentRole.WORKER if ctx.complexity == "simple" else AgentRole.MANAGER
        )

        ctx.agent.apply_complexity_result(
            complexity=ctx.complexity,
            reasoning=ctx.reasoning or "",
            determined_role=determined_role,
        )

        return StepResult.ok(ctx)


class SpawnChildren:
    """Spawn child agents for parsed subtasks.

    Calls agent.apply_subtasks_and_spawn_children() which:
    - Emits SubtasksDefined event
    - Emits ChildSpawned event for each subtask
    - Transitions agent to WAITING status
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Spawn children via pure domain method."""
        if ctx.subtasks is None:
            return StepResult.fail("No subtasks in context")

        if ctx.child_role is None:
            return StepResult.fail("No child role determined")

        # Build parent context for children
        parent_context = ctx.agent.get_context_for_child()

        ctx.agent.apply_subtasks_and_spawn_children(
            subtasks=ctx.subtasks,
            child_role=ctx.child_role,
            parent_context=parent_context,
        )

        return StepResult.ok(ctx)


class StartWorkerExecution:
    """Start worker execution by emitting CodeGenerationStarted event.

    Calls agent.start_worker_execution() which transitions the agent
    to IN_PROGRESS status.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Start worker execution via pure domain method."""
        tool_name = ctx.agent.config.tool

        ctx.agent.start_worker_execution(tool_name)

        return StepResult.ok(ctx)


class ExtractExecutionContext:
    """Extract CVE instance and other context from execution context.

    This step is mostly a no-op since PipelineContext delegates to
    agent.execution_context, but it explicitly documents the extraction point.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Verify execution context is available."""
        # PipelineContext already delegates to agent.execution_context
        # This step serves as documentation and potential extension point
        return StepResult.ok(ctx)
