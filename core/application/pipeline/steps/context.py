"""Context injection steps for pipeline execution (Design Choice 4).

These steps inject additional context into agent prompts before LLM calls.
"""

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.application.services.context_composer import ContextComposer
from core.domain.values.context import SupervisorExpectations
from core.domain.values.subtask import SubtaskJustification

if TYPE_CHECKING:
    from core.domain.values.context import SpawnPayload


class InjectSupervisorExpectations:
    """Inject supervisor expectations context into the pipeline state.

    Design Choice 4: Thinker Justification Context Passing

    This step extracts justification from the agent's spawn_payload and
    creates SupervisorExpectations context that can be rendered in prompts.

    When present, this context provides child agents with:
    - Supervisor's original task
    - Objective for this subtask
    - Why this was assigned (split_reason)
    - Suggested approach and why it should work
    - Expected deliverables
    - Budget allocation reasoning

    This step should run before prompt building steps (BuildComplexityPrompt,
    BuildWorkerPrompt) so the context is available for template rendering.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Inject supervisor expectations into pipeline state context composer.

        If the agent has a spawn_payload with justification, creates
        SupervisorExpectations and adds it to the context composer.
        """
        agent = state.agent
        spawn_payload = agent.spawn_payload

        if spawn_payload is None:
            return StepResult.ok(state)

        justification_dict = spawn_payload.subtask_justification
        if not justification_dict:
            return StepResult.ok(state)

        # Reconstruct SubtaskJustification from dict
        justification = SubtaskJustification(**justification_dict)

        # Build supervisor expectations
        supervisor_expectations = self._build_supervisor_expectations(
            spawn_payload=spawn_payload,
            justification=justification,
        )

        # Get or create context composer and add supervisor expectations
        composer = state.context_composer or ContextComposer()
        composer.add(supervisor_expectations)

        return StepResult.ok(state.with_context_composer(composer))

    def _build_supervisor_expectations(
        self,
        spawn_payload: "SpawnPayload",
        justification: SubtaskJustification,
    ) -> SupervisorExpectations:
        """Build SupervisorExpectations from spawn_payload and justification.

        Args:
            spawn_payload: The spawn payload from parent agent.
            justification: The subtask justification.

        Returns:
            SupervisorExpectations context object.
        """
        # Build budget allocation string if budget info is available
        budget_allocation = justification.budget_allocation
        if not budget_allocation and spawn_payload.complexity_budget > 0:
            child_budget = spawn_payload.complexity_budget
            budget_weight = spawn_payload.budget_weight
            total_weights = spawn_payload.total_weights
            num_siblings = spawn_payload.num_siblings

            if budget_weight is not None and total_weights is not None and total_weights > 0:
                pct = (budget_weight / total_weights * 100)
                budget_allocation = (
                    f"{pct:.0f}% of budget "
                    f"(weight {budget_weight:.1f} of {total_weights:.1f}"
                    + (f" across {num_siblings} subtasks)" if num_siblings else ")")
                )
            else:
                budget_allocation = f"Budget: {child_budget:.0f} units"

        return SupervisorExpectations(
            supervisor_task=spawn_payload.parent_task,
            objective=justification.objective,
            split_reason=justification.split_reason,
            suggested_approach=justification.plan,
            why_it_works=justification.why_it_works,
            expected_deliverables=justification.expected_results,
            budget_allocation=budget_allocation,
            complexity_assessment=justification.complexity_assessment,
            significance=justification.significance_weight,
            resource_justification=justification.resource_justification,
        )
