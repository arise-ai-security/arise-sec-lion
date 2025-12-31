"""Validation steps for pipeline execution.

These steps validate agent state at the start of each pipeline,
ensuring the agent is in the correct role and status before proceeding.
"""

from __future__ import annotations

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.enums import AgentRole, AgentStatus


class ValidatePendingAgent:
    """Validate agent is PENDING with ANALYZING status.

    Used at the start of the complexity evaluation pipeline.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Validate agent state for complexity evaluation."""
        agent = ctx.agent

        if agent.role != AgentRole.PENDING:
            return StepResult.fail(f"Requires PENDING role, got {agent.role}")

        if agent.status != AgentStatus.ANALYZING:
            return StepResult.fail(f"Requires ANALYZING status, got {agent.status}")

        if not agent.task_description:
            return StepResult.fail("Requires assigned task")

        return StepResult.ok(ctx)


class ValidateDecomposingAgent:
    """Validate agent is BOSS or MANAGER with ANALYZING status.

    Used at the start of the task decomposition pipeline.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Validate agent state for task decomposition."""
        agent = ctx.agent

        if agent.role not in (AgentRole.BOSS, AgentRole.MANAGER):
            return StepResult.fail(f"Requires BOSS/MANAGER role, got {agent.role}")

        if agent.status != AgentStatus.ANALYZING:
            return StepResult.fail(f"Requires ANALYZING status, got {agent.status}")

        return StepResult.ok(ctx)


class ValidateWorkerAgent:
    """Validate agent is WORKER with ANALYZING status.

    Used at the start of the worker execution pipeline.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Validate agent state for worker execution."""
        agent = ctx.agent

        if agent.role != AgentRole.WORKER:
            return StepResult.fail(f"Requires WORKER role, got {agent.role}")

        if agent.status != AgentStatus.ANALYZING:
            return StepResult.fail(f"Requires ANALYZING status, got {agent.status}")

        return StepResult.ok(ctx)
