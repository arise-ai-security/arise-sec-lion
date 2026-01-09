"""Domain method invocation steps for pipeline execution.

These steps call pure domain methods on AgentSession to emit events.
They bridge the pipeline (application layer) to the domain model.
"""

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole


class ApplyComplexityResult:
    """Apply complexity evaluation result to agent.

    Calls agent.apply_complexity_result() which emits ComplexityEvaluated event
    and transitions the agent from PENDING to WORKER, RESEARCHER, or MANAGER.

    Role determination:
    - simple complexity → WORKER (execute directly)
    - complex + needs_research → RESEARCHER (gather context first)
    - complex + no research needed → MANAGER (decompose directly)
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Apply complexity result via pure domain method."""
        if state.complexity is None:
            return StepResult.fail("No complexity result in state")

        # Determine role based on complexity and research needs
        if state.complexity == "simple":
            determined_role = AgentRole.WORKER
        elif state.needs_research:
            determined_role = AgentRole.RESEARCHER
        else:
            determined_role = AgentRole.MANAGER

        state.agent.apply_complexity_result(
            complexity=state.complexity,
            reasoning=state.reasoning or "",
            determined_role=determined_role,
            needs_research=state.needs_research,
        )

        return StepResult.ok(state)


class SpawnChildren:
    """Spawn child agents for parsed subtasks.

    Calls agent.apply_subtasks_and_spawn_children() which:
    - Emits SubtasksDefined event
    - Emits ChildSpawned event for each subtask
    - Transitions agent to WAITING status
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Spawn children via pure domain method."""
        if state.subtasks is None:
            return StepResult.fail("No subtasks in state")

        if state.child_role is None:
            return StepResult.fail("No child role determined")

        # Build spawn payload for children
        spawn_payload = state.agent.get_spawn_payload_for_child()

        state.agent.apply_subtasks_and_spawn_children(
            subtasks=state.subtasks,
            child_role=state.child_role,
            spawn_payload=spawn_payload,
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

