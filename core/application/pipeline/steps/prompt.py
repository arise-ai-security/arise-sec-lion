"""Prompt building steps for pipeline execution.

These steps construct prompts for LLM calls using the PromptBuilder.
Each prompt type has its own step for clarity and testability.
"""



from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.application.services.prompt_builder import PromptBuilder


class BuildComplexityPrompt:
    """Build complexity evaluation prompt for PENDING agent.

    Uses PromptBuilder to construct a prompt that asks the LLM
    to evaluate whether a task is simple (WORKER) or complex (MANAGER).
    """

    def __init__(self, prompt_builder: "PromptBuilder") -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Build complexity evaluation prompt."""
        agent = state.agent

        prompt = self._prompt_builder.build_complexity_evaluation_prompt(
            task_description=agent.task_description,
            agent_id=agent.agent_id,
            parent_task=None,
        )

        return StepResult.ok(state.with_prompt(prompt))


class BuildDecompositionPrompt:
    """Build task decomposition prompt for BOSS or MANAGER agent.

    Uses PromptBuilder to construct a role-specific prompt:
    - BOSS: Uses build_boss_delegation_prompt (or build_boss_prompt_with_context)
    - MANAGER: Uses build_manager_decomposition_prompt (or build_manager_prompt_with_context)

    The prompt includes limit-aware context from HierarchyLimits and
    global config from ContextComposer when available.
    """

    def __init__(self, prompt_builder: "PromptBuilder") -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Build role-specific decomposition prompt.

        Uses build_*_prompt_with_context() if a ContextComposer is
        available, otherwise falls back to standard build methods.
        """
        agent = state.agent

        # Use context-aware prompt building if composer available
        if state.context_composer:
            if agent.role == AgentRole.BOSS:
                prompt = self._prompt_builder.build_boss_prompt_with_context(
                    task_description=agent.task_description,
                    context=state.context_composer,
                    hierarchy_limits=state.hierarchy_limits,
                    cve_instance=state.cve_instance,
                )
            else:  # MANAGER
                prompt = self._prompt_builder.build_manager_prompt_with_context(
                    task_description=agent.task_description,
                    context=state.context_composer,
                    hierarchy_limits=state.hierarchy_limits,
                    cve_instance=state.cve_instance,
                )
        else:
            if agent.role == AgentRole.BOSS:
                prompt = self._prompt_builder.build_boss_delegation_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    cve_instance=state.cve_instance,
                    hierarchy_limits=state.hierarchy_limits,
                )
            else:  # MANAGER
                prompt = self._prompt_builder.build_manager_decomposition_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    cve_instance=state.cve_instance,
                    spawn_payload=agent.spawn_payload,
                    hierarchy_limits=state.hierarchy_limits,
                )

        return StepResult.ok(state.with_prompt(prompt))


class BuildWorkerPrompt:
    """Build worker execution prompt for WORKER agent.

    Uses PromptBuilder to construct an enhanced prompt that includes:
    - Global config for system-wide context (via ContextComposer)
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
        """Build enhanced worker prompt.

        Uses build_worker_prompt_with_context() if a ContextComposer is
        available, otherwise falls back to build_worker_prompt().
        """
        agent = state.agent

        # Use context-aware prompt building if composer available
        if state.context_composer:
            enhanced_description = self._prompt_builder.build_worker_prompt_with_context(
                task_description=agent.task_description,
                context=state.context_composer,
                workspace_context=state.workspace_context,
                cve_instance=state.cve_instance,
            )
        else:
            enhanced_description = self._prompt_builder.build_worker_prompt(
                task_description=agent.task_description,
                sibling_view=state.sibling_view,
                workspace_context=state.workspace_context,
                cve_instance=state.cve_instance,
                spawn_payload=agent.spawn_payload,
            )

        return StepResult.ok(state.with_prompt(enhanced_description))
