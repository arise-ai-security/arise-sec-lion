"""Response parsing steps for pipeline execution.

These steps parse LLM responses into domain objects.
"""



import json
import logging

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.services import (
    parse_subtasks_from_llm,
    strip_markdown_code_block,
)
from core.domain.values.constraint_failure import ConstraintFailure

logger = logging.getLogger(__name__)


class ParseComplexityResult:
    """Parse complexity evaluation response from LLM.

    Expects JSON with format:
    {
        "complexity": "simple" | "complex",
        "reasoning": "explanation..."
    }
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Parse complexity result from LLM response."""
        if state.llm_response is None:
            return StepResult.fail("No LLM response in context")

        try:
            clean_response = strip_markdown_code_block(state.llm_response.content)
            data = json.loads(clean_response)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            if complexity not in ("simple", "complex"):
                return StepResult.fail(f"Invalid complexity value: {complexity}")

        except json.JSONDecodeError as e:
            return StepResult.fail(f"Failed to parse complexity JSON: {e}")
        except (ValueError, KeyError) as e:
            return StepResult.fail(f"Failed to extract complexity: {e}")

        return StepResult.ok(state.with_complexity_result(complexity, reasoning))


class ParseSubtasks:
    """Parse subtask definitions from LLM decomposition response.

    Uses parse_subtasks_from_llm which handles:
    - Various JSON response formats
    - ConstraintFailure detection
    - Subtask validation
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Parse subtasks from LLM response."""
        if state.llm_response is None:
            return StepResult.fail("No LLM response in context")

        try:
            result = parse_subtasks_from_llm(state.llm_response.content)
        except ValueError as e:
            logger.warning(
                "Failed to parse subtasks for agent=%s: %s",
                state.agent.agent_id,
                e,
            )
            return StepResult.fail(f"Failed to parse subtasks: {e}")

        # Handle ConstraintFailure - graceful failure when limits are unsatisfiable
        if isinstance(result, ConstraintFailure):
            logger.warning(
                "Agent %s reported unsatisfiable constraints: %s",
                state.agent.agent_id,
                result.format_message(),
            )
            return StepResult.fail(result.format_message())

        return StepResult.ok(state.with_subtasks(result))
