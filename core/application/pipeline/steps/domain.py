"""Domain method invocation steps for pipeline execution.

These steps call pure domain methods on AgentSession to emit events.
They bridge the pipeline (application layer) to the domain model.
"""

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole


class ApplyComplexityResult:
    """Apply complexity evaluation result to agent.

    Calls agent.apply_complexity_result() which emits ComplexityEvaluated event
    and transitions the agent from PENDING to WORKER or MANAGER.

    Note: If the agent's role has already been updated (e.g., by CheckBudgetThreshold
    step in Design Choice 2), this step skips application to avoid duplicate events.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Apply complexity result via pure domain method."""
        if state.complexity is None:
            return StepResult.fail("No complexity result in state")

        # Skip if already applied (e.g., by CheckBudgetThreshold)
        # PENDING agents haven't had complexity evaluated yet
        if state.agent.role != AgentRole.PENDING:
            return StepResult.ok(state)

        determined_role = (
            AgentRole.WORKER if state.complexity == "simple" else AgentRole.MANAGER
        )

        state.agent.apply_complexity_result(
            complexity=state.complexity,
            reasoning=state.reasoning or "",
            determined_role=determined_role,
        )

        return StepResult.ok(state)


class SpawnChildren:
    """Spawn child agents for parsed subtasks.

    Calls agent.apply_subtasks_and_spawn_children() which:
    - Emits SubtasksDefined event
    - Emits ChildSpawned event for each subtask
    - Transitions agent to WAITING status

    Design Choice 3: Calculates proportional budget allocation for children
    based on subtask.budget_weight when complexity budget is enabled.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Spawn children via pure domain method."""
        if state.subtasks is None:
            return StepResult.fail("No subtasks in state")

        if state.child_role is None:
            return StepResult.fail("No child role determined")

        # Build spawn payload for children
        spawn_payload = state.agent.get_spawn_payload_for_child()

        # Calculate proportional budget allocation for children (Design Choice 3)
        child_budgets = state.agent.calculate_child_budgets(state.subtasks)

        state.agent.apply_subtasks_and_spawn_children(
            subtasks=state.subtasks,
            child_role=state.child_role,
            spawn_payload=spawn_payload,
            child_budgets=child_budgets if any(b > 0 for b in child_budgets) else None,
        )

        return StepResult.ok(state)


class StartWorkerExecution:
    """Start worker execution by emitting CodeGenerationStarted event.

    Calls agent.start_worker_execution() which transitions the agent
    to IN_PROGRESS status.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Start worker execution via pure domain method."""
        tool_name = state.agent.config.tool

        state.agent.start_worker_execution(tool_name)

        return StepResult.ok(state)

