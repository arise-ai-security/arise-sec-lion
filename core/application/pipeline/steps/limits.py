"""Limit enforcement steps for pipeline execution.

These steps enforce system limits on agent hierarchies:
- max_children_per_node: Hard enforcement (fail if exceeded)
- max_total_agents: Hard enforcement (fail if exceeded)
- max_depth: Soft enforcement (force WORKER role)
"""

from __future__ import annotations

from typing import Any

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.values.enums import AgentRole


class CheckLimitViolations:
    """Check for hard limit violations (children, total_agents).

    If subtasks would violate limits, emits LimitEnforced events
    and fails the pipeline. This is the fail-fast enforcement.
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Check if subtasks violate any hard limits."""
        exec_ctx = ctx.execution_context
        if exec_ctx is None:
            return StepResult.ok(ctx)

        if ctx.subtasks is None:
            return StepResult.fail("No subtasks in context to check limits")

        subtasks = ctx.subtasks
        violations: list[dict[str, Any]] = []

        # Check children per node limit (hard enforcement)
        if exec_ctx.is_children_limited() and len(subtasks) > exec_ctx.max_children_per_node:
            violations.append({
                "limit_type": "children",
                "limit_value": exec_ctx.max_children_per_node,
                "attempted_value": len(subtasks),
                "action_taken": "agent_failed",
            })

        # Check total agents limit (hard enforcement)
        if exec_ctx.is_total_agents_limited():
            remaining = exec_ctx.agents_remaining()
            if len(subtasks) > remaining:
                violations.append({
                    "limit_type": "total_agents",
                    "limit_value": exec_ctx.max_total_agents,
                    "attempted_value": exec_ctx.current_total_agents + len(subtasks),
                    "action_taken": "agent_failed",
                })

        if violations:
            # Emit limit enforced events for each violation
            for v in violations:
                ctx.agent.emit_limit_enforced(**v)
            violation_types = [v["limit_type"] for v in violations]
            return StepResult.fail(f"LLM violated limits: {violation_types}")

        return StepResult.ok(ctx)


class DetermineChildRole:
    """Determine child role based on depth limit (soft enforcement).

    If at max_depth, forces children to WORKER role instead of PENDING.
    Emits LimitEnforced event with action_taken="forced_worker_role".
    """

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Determine child role, forcing WORKER if at max depth."""
        exec_ctx = ctx.execution_context
        agent = ctx.agent

        force_worker = False

        if exec_ctx is not None and not exec_ctx.can_spawn_child():
            force_worker = True
            agent.emit_limit_enforced(
                limit_type="depth",
                limit_value=exec_ctx.max_depth,
                attempted_value=exec_ctx.current_depth + 1,
                action_taken="forced_worker_role",
            )

        child_role = AgentRole.WORKER.value if force_worker else AgentRole.PENDING.value

        return StepResult.ok(ctx.with_child_role(child_role, force_worker))
