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
from core.application.pipeline.steps.budget import CheckBudgetThreshold, ProbabilisticWorkerShortcut
from core.application.pipeline.steps.context import InjectSupervisorExpectations
from core.application.pipeline.steps.domain import (
    ApplyComplexityResult,
    SpawnChildren,
    StartWorkerExecution,
)
from core.application.pipeline.steps.knowledge import (
    GenerateWorkerReport,
    InjectCoworkerKnowledge,
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
    from config.settings import OrchestrationConfig
    from core.application.services.child_factory import ChildAgentFactory
    from core.application.services.prompt_builder import PromptBuilder
    from core.ports.llm_port import LLMPort
    from core.ports.shared_context_port import SharedContextPort
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
        orchestration_config: "OrchestrationConfig | None" = None,
        shared_context_port: "SharedContextPort | None" = None,
    ) -> None:
        """Initialize factory with required ports.

        Args:
            llm_port: Port for LLM interactions
            worker_port: Port for worker tool execution
            prompt_builder: Builder for constructing prompts
            child_factory: Factory for child agents (source of truth for agent counts)
            orchestration_config: Configuration for orchestration (includes complexity_budget)
            shared_context_port: Port for shared context access (Design Choice 5)
        """
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._orchestration_config = orchestration_config
        self._shared_context_port = shared_context_port

    def create_complexity_pipeline(self) -> Pipeline:
        """Create pipeline for PENDING agent complexity evaluation.

        Pipeline steps:
        1. ValidatePendingAgent - Assert PENDING + ANALYZING
        2. ProbabilisticWorkerShortcut - Russian Roulette chance to force WORKER (Design Choice 3)
        3. CheckBudgetThreshold - Short-circuit if budget too low (Design Choice 2)
        4. InjectSupervisorExpectations - Inject parent justification context (Design Choice 4)
        5. BuildComplexityPrompt - Via PromptBuilder
        6. EmitPromptSent - Observability
        7. QueryLLM - Call LLM port
        8. EmitTokensConsumed - Cost tracking
        9. ParseComplexityResult - Parse JSON (simple/complex)
        10. ApplyComplexityResult - Call agent.apply_complexity_result()

        Returns:
            Configured Pipeline for complexity evaluation
        """
        steps: list = [ValidatePendingAgent]

        # Add budget-based steps if config is available
        if self._orchestration_config is not None:
            budget_config = self._orchestration_config.complexity_budget
            # Design Choice 3: Russian Roulette shortcut (runs first)
            steps.append(ProbabilisticWorkerShortcut(budget_config))
            # Design Choice 2: Budget threshold check
            steps.append(CheckBudgetThreshold(budget_config))

        steps.extend([
            InjectSupervisorExpectations(),  # Design Choice 4
            BuildComplexityPrompt(self._prompt_builder),
            EmitPromptSent(prompt_type="complexity_evaluation", target="llm"),
            QueryLLM(self._llm_port, operation="complexity_evaluation"),
            EmitTokensConsumed(operation="complexity_evaluation"),
            ParseComplexityResult(),
            ApplyComplexityResult(),
        ])

        return Pipeline(name="complexity_evaluation", steps=steps)

    def create_decomposition_pipeline(self) -> Pipeline:
        """Create pipeline for BOSS/MANAGER task decomposition.

        Pipeline steps:
        1. ValidateDecomposingAgent - Assert BOSS/MANAGER + ANALYZING
        2. BuildDecompositionPrompt - Role-specific (BOSS vs MANAGER) with budget context
        3. EmitPromptSent - Observability
        4. QueryLLM - Call LLM port
        5. EmitTokensConsumed - Cost tracking
        6. ParseSubtasks - Parse JSON, handle ConstraintFailure
        7. CheckLimitViolations - Hard enforcement (children, total_agents)
        8. DetermineChildRole - Soft enforcement (force WORKER at max depth)
        9. SpawnChildren - Call agent.apply_subtasks_and_spawn_children()

        Returns:
            Configured Pipeline for task decomposition
        """
        return Pipeline(
            name="task_decomposition",
            steps=[
                ValidateDecomposingAgent,
                BuildDecompositionPrompt(self._prompt_builder, self._orchestration_config),
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
        3. InjectSupervisorExpectations - Inject parent justification context (Design Choice 4)
        4. InjectCoworkerKnowledge - Inject published knowledge from earlier workers (Design Choice 5)
        5. BuildWorkerPrompt - Via PromptBuilder
        6. EmitPromptSent - Observability (target=dynamic -> tool name)
        7. RunWorkerSession - Execute via worker_port, apply events
        8. GenerateWorkerReport - Generate structured report for parent (Design Choice 5)

        Returns:
            Configured Pipeline for worker execution
        """
        steps: list = [
            ValidateWorkerAgent,
            StartWorkerExecution(),
            InjectSupervisorExpectations(),  # Design Choice 4
        ]

        # Design Choice 5: Inject coworker knowledge if shared context port is available
        if self._shared_context_port is not None:
            steps.append(InjectCoworkerKnowledge(self._shared_context_port))

        steps.extend([
            BuildWorkerPrompt(self._prompt_builder),
            EmitPromptSent(prompt_type="worker_execution", target="dynamic"),
            RunWorkerSession(self._worker_port),
            GenerateWorkerReport(),  # Design Choice 5: Generate report for parent
        ])

        return Pipeline(name="worker_execution", steps=steps)
