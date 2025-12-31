"""Pipeline factory for agent orchestration.

Creates configured pipelines for different orchestration operations:
- Complexity evaluation (PENDING -> WORKER/MANAGER)
- Task decomposition (BOSS/MANAGER -> children)
- Worker execution (WORKER -> complete)

Each pipeline is a sequence of focused, testable steps that together
implement the orchestration logic previously in AgentOrchestrator methods.
"""



from typing import TYPE_CHECKING

from core.application.pipeline.executor import Pipeline
from core.application.pipeline.steps.domain import (
    ApplyComplexityResult,
    ExtractHierarchyLimits,
    SpawnChildren,
    StartWorkerExecution,
)
from core.application.pipeline.steps.limits import CheckLimitViolations, DetermineChildRole
from core.application.pipeline.steps.llm import QueryLLM
from core.application.pipeline.steps.observability import EmitPromptSent, EmitTokensConsumed
from core.application.pipeline.steps.parsing import ParseComplexityResult, ParseSubtasks
from core.application.pipeline.steps.prompt import (
    BuildComplexityPrompt,
    BuildDecompositionPrompt,
    BuildWorkerPrompt,
)
from core.application.pipeline.steps.validation import (
    ValidateDecomposingAgent,
    ValidatePendingAgent,
    ValidateWorkerAgent,
)
from core.application.pipeline.steps.worker import RunWorkerSession

if TYPE_CHECKING:
    from core.application.services.prompt_builder import PromptBuilder
    from core.ports.llm_port import LLMPort
    from core.ports.worker_port import WorkerToolPort


class PipelineFactory:
    """Factory for creating configured pipelines.

    Centralizes pipeline construction with dependency injection.
    Each pipeline is a sequence of steps that implements a specific
    orchestration operation.

    Usage:
        factory = PipelineFactory(llm_port, worker_port, prompt_builder)
        complexity_pipeline = factory.create_complexity_pipeline()
        decomposition_pipeline = factory.create_decomposition_pipeline()
        worker_pipeline = factory.create_worker_pipeline()
    """

    def __init__(
        self,
        llm_port: "LLMPort",
        worker_port: "WorkerToolPort",
        prompt_builder: "PromptBuilder",
    ) -> None:
        """Initialize factory with required ports.

        Args:
            llm_port: Port for LLM interactions
            worker_port: Port for worker tool execution
            prompt_builder: Builder for constructing prompts
        """
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder

    def create_complexity_pipeline(self) -> Pipeline:
        """Create pipeline for PENDING agent complexity evaluation.

        Pipeline steps:
        1. ValidatePendingAgent - Assert PENDING + ANALYZING
        2. BuildComplexityPrompt - Via PromptBuilder
        3. EmitPromptSent - Observability
        4. QueryLLM - Call LLM port
        5. EmitTokensConsumed - Cost tracking
        6. ParseComplexityResult - Parse JSON (simple/complex)
        7. ApplyComplexityResult - Call agent.apply_complexity_result()

        Returns:
            Configured Pipeline for complexity evaluation
        """
        return Pipeline(
            name="complexity_evaluation",
            steps=[
                ValidatePendingAgent(),
                BuildComplexityPrompt(self._prompt_builder),
                EmitPromptSent(prompt_type="complexity_evaluation", target="llm"),
                QueryLLM(self._llm_port, operation="complexity_evaluation"),
                EmitTokensConsumed(operation="complexity_evaluation"),
                ParseComplexityResult(),
                ApplyComplexityResult(),
            ],
        )

    def create_decomposition_pipeline(self) -> Pipeline:
        """Create pipeline for BOSS/MANAGER task decomposition.

        Pipeline steps:
        1. ValidateDecomposingAgent - Assert BOSS/MANAGER + ANALYZING
        2. ExtractHierarchyLimits - Document limits extraction point
        3. BuildDecompositionPrompt - Role-specific (BOSS vs MANAGER)
        4. EmitPromptSent - Observability
        5. QueryLLM - Call LLM port
        6. EmitTokensConsumed - Cost tracking
        7. ParseSubtasks - Parse JSON, handle ConstraintFailure
        8. CheckLimitViolations - Hard enforcement (children, total_agents)
        9. DetermineChildRole - Soft enforcement (force WORKER at max depth)
        10. SpawnChildren - Call agent.apply_subtasks_and_spawn_children()

        Returns:
            Configured Pipeline for task decomposition
        """
        return Pipeline(
            name="task_decomposition",
            steps=[
                ValidateDecomposingAgent(),
                ExtractHierarchyLimits(),
                BuildDecompositionPrompt(self._prompt_builder),
                EmitPromptSent(prompt_type="task_decomposition", target="llm"),
                QueryLLM(self._llm_port, operation="task_decomposition"),
                EmitTokensConsumed(operation="task_decomposition"),
                ParseSubtasks(),
                CheckLimitViolations(),
                DetermineChildRole(),
                SpawnChildren(),
            ],
        )

    def create_worker_pipeline(self) -> Pipeline:
        """Create pipeline for WORKER task execution.

        Pipeline steps:
        1. ValidateWorkerAgent - Assert WORKER + ANALYZING
        2. StartWorkerExecution - Emit CodeGenerationStarted
        3. BuildWorkerPrompt - Via PromptBuilder
        4. EmitPromptSent - Observability (target=dynamic -> tool name)
        5. RunWorkerSession - Execute via worker_port, apply events

        Returns:
            Configured Pipeline for worker execution
        """
        return Pipeline(
            name="worker_execution",
            steps=[
                ValidateWorkerAgent(),
                StartWorkerExecution(),
                BuildWorkerPrompt(self._prompt_builder),
                EmitPromptSent(prompt_type="worker_execution", target="dynamic"),
                RunWorkerSession(self._worker_port),
            ],
        )
