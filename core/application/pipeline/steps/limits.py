"""Limit enforcement steps for pipeline execution.

These steps enforce system limits on agent hierarchies:
- max_children_per_node: Hard enforcement (fail if exceeded)
- max_total_agents: Hard enforcement (fail if exceeded)
- max_depth: Soft enforcement (force WORKER role)
"""



from typing import Any

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole


class CheckLimitViolations:
    """Check for hard limit violations (children, total_agents).

    If subtasks would violate limits, emits LimitEnforced events
    and fails the pipeline. This is the fail-fast enforcement.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Check if subtasks violate any hard limits."""
        limits = state.hierarchy_limits
        if limits is None:
            return StepResult.ok(state)

        if state.subtasks is None:
            return StepResult.fail("No subtasks in context to check limits")

        subtasks = state.subtasks
        violations: list[dict[str, Any]] = []

        # Check children per node limit (hard enforcement)
        if limits.is_children_limited() and len(subtasks) > limits.max_children_per_node:
            violations.append({
                "limit_type": "children",
                "limit_value": limits.max_children_per_node,
                "attempted_value": len(subtasks),
                "action_taken": "agent_failed",
            })

        # Check total agents limit (hard enforcement)
        if limits.is_total_agents_limited():
            remaining = limits.agents_remaining()
            if len(subtasks) > remaining:
                violations.append({
                    "limit_type": "total_agents",
                    "limit_value": limits.max_total_agents,
                    "attempted_value": limits.current_total_agents + len(subtasks),
                    "action_taken": "agent_failed",
                })

        if violations:
            # Emit limit enforced events for each violation
            for v in violations:
                state.agent.emit_limit_enforced(**v)
            violation_types = [v["limit_type"] for v in violations]
            return StepResult.fail(f"LLM violated limits: {violation_types}")

        return StepResult.ok(state)


class DetermineChildRole:
    """Determine child role based on depth limit (soft enforcement).

    If at max_depth, forces children to WORKER role instead of PENDING.
    Emits LimitEnforced event with action_taken="forced_worker_role".
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Determine child role, forcing WORKER if at max depth."""
        limits = state.hierarchy_limits
        agent = state.agent

        force_worker = False

        if limits is not None and not limits.can_spawn_child():
            force_worker = True
            agent.emit_limit_enforced(
                limit_type="depth",
                limit_value=limits.max_depth,
                attempted_value=limits.current_depth + 1,
                action_taken="forced_worker_role",
            )

        child_role = AgentRole.WORKER.value if force_worker else AgentRole.PENDING.value

        return StepResult.ok(state.with_child_role(child_role, force_worker))
