"""Prompt building steps for pipeline execution.

These steps construct prompts for LLM calls using the PromptBuilder.
Each prompt type has its own step for clarity and testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.services.prompt_builder import PromptBuilder


class BuildComplexityPrompt:
    """Build complexity evaluation prompt for PENDING agent.

    Uses PromptBuilder to construct a prompt that asks the LLM
    to evaluate whether a task is simple (WORKER) or complex (MANAGER).
    """

    def __init__(self, prompt_builder: PromptBuilder) -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Build complexity evaluation prompt."""
        agent = ctx.agent

        prompt = self._prompt_builder.build_complexity_evaluation_prompt(
            task_description=agent.task_description,
            agent_id=agent.agent_id,
            parent_task=None,
        )

        return StepResult.ok(ctx.with_prompt(prompt))


class BuildDecompositionPrompt:
    """Build task decomposition prompt for BOSS or MANAGER agent.

    Uses PromptBuilder to construct a role-specific prompt:
    - BOSS: Uses build_boss_delegation_prompt
    - MANAGER: Uses build_manager_decomposition_prompt

    The prompt includes limit-aware context from ExecutionContext.
    """

    def __init__(self, prompt_builder: PromptBuilder) -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Build role-specific decomposition prompt."""
        agent = ctx.agent

        if agent.role == AgentRole.BOSS:
            prompt = self._prompt_builder.build_boss_delegation_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                cve_instance=ctx.cve_instance,
                registered_tasks=ctx.registered_tasks,
                execution_context=ctx.execution_context,
            )
        else:  # MANAGER
            prompt = self._prompt_builder.build_manager_decomposition_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                registered_tasks=ctx.registered_tasks,
                cve_instance=ctx.cve_instance,
                parent_context=agent.parent_context,
                execution_context=ctx.execution_context,
            )

        return StepResult.ok(ctx.with_prompt(prompt))


class BuildWorkerPrompt:
    """Build worker execution prompt for WORKER agent.

    Uses PromptBuilder to construct an enhanced prompt that includes:
    - Sibling context for coordination
    - Workspace context for existing files
    - CVE instance for security tasks
    - Parent context for hierarchy awareness
    """

    def __init__(self, prompt_builder: PromptBuilder) -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Build enhanced worker prompt."""
        agent = ctx.agent

        enhanced_description = self._prompt_builder.build_worker_prompt(
            task_description=agent.task_description,
            sibling_context=ctx.sibling_context,
            workspace_context=ctx.workspace_context,
            cve_instance=ctx.cve_instance,
            parent_context=agent.parent_context,
        )

        return StepResult.ok(ctx.with_prompt(enhanced_description))
