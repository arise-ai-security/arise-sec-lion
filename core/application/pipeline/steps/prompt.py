"""Prompt building steps for pipeline execution.

These steps construct prompts for LLM calls using the PromptBuilder.
Each prompt type has its own step for clarity and testability.
"""



from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from config.settings import OrchestrationConfig
    from core.application.services.prompt_builder import PromptBuilder


class BuildComplexityPrompt:
    """Build complexity evaluation prompt for PENDING agent.

    Uses PromptBuilder to construct a prompt that asks the LLM
    to evaluate whether a task is simple (WORKER) or complex (MANAGER).

    If InjectSupervisorExpectations was run before this step,
    supervisor context will be prepended to the prompt.
    """

    def __init__(self, prompt_builder: "PromptBuilder") -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Build complexity evaluation prompt with optional supervisor context."""
        agent = state.agent

        # Build base complexity evaluation prompt
        base_prompt = self._prompt_builder.build_complexity_evaluation_prompt(
            task_description=agent.task_description,
            agent_id=agent.agent_id,
            parent_task=None,
        )

        # Prepend supervisor expectations context if available (Design Choice 4)
        prompt = self._prepend_context(base_prompt, state)

        return StepResult.ok(state.with_prompt(prompt))

    def _prepend_context(self, prompt: str, state: PipelineState) -> str:
        """Prepend supervisor expectations context to prompt if available."""
        if state.context_composer is None:
            return prompt

        if not state.context_composer.has("supervisor_expectations"):
            return prompt

        # Render supervisor expectations template
        supervisor_prompt = self._prompt_builder.chain().render(
            "core/context/supervisor_expectations.j2",
            **state.context_composer.build(),
        ).build()

        return supervisor_prompt + "\n\n" + prompt if supervisor_prompt else prompt


class BuildDecompositionPrompt:
    """Build task decomposition prompt for BOSS or MANAGER agent.

    Uses PromptBuilder to construct a role-specific prompt:
    - BOSS: Uses build_boss_delegation_prompt
    - MANAGER: Uses build_manager_decomposition_prompt

    The prompt includes limit-aware context from HierarchyLimits and
    complexity budget context (Design Choices 2-3).
    """

    def __init__(
        self,
        prompt_builder: "PromptBuilder",
        orchestration_config: "OrchestrationConfig | None" = None,
    ) -> None:
        """Initialize with prompt builder and optional config.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
            orchestration_config: Configuration for orchestration (includes complexity_budget)
        """
        self._prompt_builder = prompt_builder
        self._config = orchestration_config

    async def execute(self, state: PipelineState) -> StepResult:
        """Build role-specific decomposition prompt with budget context."""
        agent = state.agent

        # Extract budget context (Design Choices 2-3)
        complexity_budget: float | None = None
        initial_complexity_budget: float | None = None
        budget_threshold_ratio: float | None = None

        if self._config is not None and self._config.complexity_budget.is_enabled():
            budget_config = self._config.complexity_budget
            complexity_budget = agent.complexity_budget if agent.complexity_budget > 0 else None
            initial_complexity_budget = budget_config.initial_amount
            budget_threshold_ratio = budget_config.threshold_ratio

        if agent.role == AgentRole.BOSS:
            prompt = self._prompt_builder.build_boss_delegation_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                cve_instance=state.cve_instance,
                hierarchy_limits=state.hierarchy_limits,
                complexity_budget=complexity_budget,
                initial_complexity_budget=initial_complexity_budget,
                budget_threshold_ratio=budget_threshold_ratio,
            )
        else:  # MANAGER
            prompt = self._prompt_builder.build_manager_decomposition_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                cve_instance=state.cve_instance,
                spawn_payload=agent.spawn_payload,
                hierarchy_limits=state.hierarchy_limits,
                complexity_budget=complexity_budget,
                initial_complexity_budget=initial_complexity_budget,
                budget_threshold_ratio=budget_threshold_ratio,
            )

        return StepResult.ok(state.with_prompt(prompt))


class BuildWorkerPrompt:
    """Build worker execution prompt for WORKER agent.

    Uses PromptBuilder to construct an enhanced prompt that includes:
    - Supervisor expectations context (Design Choice 4)
    - Sibling view for coordination
    - Workspace context for existing files
    - CVE instance for security tasks
    - Spawn payload for hierarchy awareness
    """

    def __init__(self, prompt_builder: "PromptBuilder") -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Build enhanced worker prompt with supervisor context."""
        agent = state.agent

        # Build base worker prompt
        base_prompt = self._prompt_builder.build_worker_prompt(
            task_description=agent.task_description,
            sibling_view=state.sibling_view,
            workspace_context=state.workspace_context,
            cve_instance=state.cve_instance,
            spawn_payload=agent.spawn_payload,
        )

        # Prepend supervisor expectations context if available (Design Choice 4)
        prompt = self._prepend_context(base_prompt, state)

        return StepResult.ok(state.with_prompt(prompt))

    def _prepend_context(self, prompt: str, state: PipelineState) -> str:
        """Prepend supervisor expectations context to prompt if available."""
        if state.context_composer is None:
            return prompt

        if not state.context_composer.has("supervisor_expectations"):
            return prompt

        # Render supervisor expectations template
        supervisor_prompt = self._prompt_builder.chain().render(
            "core/context/supervisor_expectations.j2",
            **state.context_composer.build(),
        ).build()

        return supervisor_prompt + "\n\n" + prompt if supervisor_prompt else prompt
