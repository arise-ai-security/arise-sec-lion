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
from core.application.pipeline.steps.context import InjectGlobalConfig
from core.application.pipeline.steps.domain import (
    ApplyComplexityResult,
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
    from core.application.services.child_factory import ChildAgentFactory
    from core.application.services.global_config_provider import GlobalConfigProvider
    from core.application.services.prompt_builder import PromptBuilder
    from core.ports.llm_port import LLMPort
    from core.ports.worker_port import WorkerToolPort


class PipelineFactory:
    """Factory for creating configured pipelines.

    Centralizes pipeline construction with dependency injection.
    Each pipeline is a sequence of steps that implements a specific
    orchestration operation.

    Usage:
        factory = PipelineFactory(llm_port, worker_port, prompt_builder, child_factory)
        complexity_pipeline = factory.create_complexity_pipeline()
        decomposition_pipeline = factory.create_decomposition_pipeline()
        worker_pipeline = factory.create_worker_pipeline()
    """

    def __init__(
        self,
        llm_port: "LLMPort",
        worker_port: "WorkerToolPort",
        prompt_builder: "PromptBuilder",
        child_factory: "ChildAgentFactory",
        global_config_provider: "GlobalConfigProvider | None" = None,
    ) -> None:
        """Initialize factory with required ports.

        Args:
            llm_port: Port for LLM interactions
            worker_port: Port for worker tool execution
            prompt_builder: Builder for constructing prompts
            child_factory: Factory for child agents (source of truth for agent counts)
            global_config_provider: Optional provider for global configuration context
        """
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._global_config_provider = global_config_provider

    def _get_context_step(self) -> list:
        """Get context injection step(s) if global config provider is set.

        Returns:
            List containing InjectGlobalConfig step, or empty list if no provider.
        """
        if self._global_config_provider:
            return [InjectGlobalConfig(self._global_config_provider)]
        return []

    def create_complexity_pipeline(self) -> Pipeline:
        """Create pipeline for PENDING agent complexity evaluation.

        Pipeline steps:
        1. ValidatePendingAgent - Assert PENDING + ANALYZING
        2. InjectGlobalConfig - Inject global context (if provider set)
        3. BuildComplexityPrompt - Via PromptBuilder
        4. EmitPromptSent - Observability
        5. QueryLLM - Call LLM port
        6. EmitTokensConsumed - Cost tracking
        7. ParseComplexityResult - Parse JSON (simple/complex)
        8. ApplyComplexityResult - Call agent.apply_complexity_result()

        Returns:
            Configured Pipeline for complexity evaluation
        """
        return Pipeline(
            name="complexity_evaluation",
            steps=[
                ValidatePendingAgent,
                *self._get_context_step(),
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
        2. InjectGlobalConfig - Inject global context (if provider set)
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
                ValidateDecomposingAgent,
                *self._get_context_step(),
                BuildDecompositionPrompt(self._prompt_builder),
                EmitPromptSent(prompt_type="task_decomposition", target="llm"),
                QueryLLM(self._llm_port, operation="task_decomposition"),
                EmitTokensConsumed(operation="task_decomposition"),
                ParseSubtasks(),
                CheckLimitViolations(self._child_factory),
                DetermineChildRole(),
                SpawnChildren(),
            ],
        )

    def create_worker_pipeline(self) -> Pipeline:
        """Create pipeline for WORKER task execution.

        Pipeline steps:
        1. ValidateWorkerAgent - Assert WORKER + ANALYZING
        2. StartWorkerExecution - Emit CodeGenerationStarted
        3. InjectGlobalConfig - Inject global context (if provider set)
        4. BuildWorkerPrompt - Via PromptBuilder
        5. EmitPromptSent - Observability (target=dynamic -> tool name)
        6. RunWorkerSession - Execute via worker_port, apply events

        Returns:
            Configured Pipeline for worker execution
        """
        return Pipeline(
            name="worker_execution",
            steps=[
                ValidateWorkerAgent,
                StartWorkerExecution(),
                *self._get_context_step(),
                BuildWorkerPrompt(self._prompt_builder),
                EmitPromptSent(prompt_type="worker_execution", target="dynamic"),
                RunWorkerSession(self._worker_port),
            ],
        )
