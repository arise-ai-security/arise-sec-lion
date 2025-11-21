"""Domain service for building hierarchical prompts.

This module provides a domain service that composes prompts from modular
templates following the industry-standard hierarchical structure:
1. System Prompt (Master) - Agent identity and capabilities
2. Strategy Prompt - Node-specific operational strategies
3. Task Prompt - Specific task description and context
4. Output Format Prompt - Expected response structure

Reference:
    - OpenHands prompt structure (XML sections)
    - Anthropic context engineering best practices
    - ReAct prompting pattern
"""

from pathlib import Path
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


class PromptBuilder:
    """Domain service for composing hierarchical prompts.

    Responsibility: Build complete prompts by composing modular templates
    in the correct hierarchical order. Ensures separation of concerns between
    system configuration, operational strategy, task definition, and output format.

    Usage:
        builder = PromptBuilder(template_dir="prompts")
        prompt = builder.build_manager_decomposition_prompt(
            task_description="Implement authentication system",
            agent_id=uuid4(),
            parent_task="Build web application"
        )
    """

    def __init__(self, template_dir: str | Path = "prompts") -> None:
        """Initialize the prompt builder with template directory.

        Args:
            template_dir: Path to the root prompts directory containing
                         system/, strategies/, tasks/, output_formats/ subdirectories.
        """
        self.template_dir = Path(template_dir)
        # Autoescape disabled intentionally: templates generate LLM prompts (plain text),
        # not HTML. XSS is not a risk, and escaping would corrupt prompt content.
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build_complexity_evaluation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
    ) -> str:
        """Build complete prompt for PENDING agent complexity evaluation.

        Prompt Structure:
            1. System: role_pending.j2 - PENDING agent identity
            2. Strategy: complexity_evaluation.j2 - Evaluation methodology
            3. Task: complexity_evaluation.j2 - Task to evaluate
            4. Output Format: complexity_result.j2 - JSON schema

        Args:
            task_description: The task to evaluate for complexity.
            agent_id: UUID of the PENDING agent performing evaluation.
            parent_task: Optional description of parent's task for context.

        Returns:
            Complete hierarchical prompt string ready for LLM.

        Raises:
            TemplateNotFound: If any required template file is missing.
        """
        try:
            # 1. System Prompt - PENDING agent identity
            system = self.env.get_template("system/role_pending.j2").render()

            # 2. Strategy Prompt - Complexity evaluation methodology
            strategy = self.env.get_template("strategies/complexity_evaluation.j2").render()

            # 3. Task Prompt - Specific task to evaluate
            task = self.env.get_template("tasks/complexity_evaluation.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                parent_task=parent_task,
            )

            # 4. Output Format - JSON schema for complexity result
            output_format = self.env.get_template("output_formats/complexity_result.j2").render()

            # Compose in hierarchical order
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"

        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: str = "MANAGER",
        parent_task: str | None = None,
    ) -> str:
        """Build complete prompt for MANAGER agent task decomposition.

        Prompt Structure:
            1. System: role_manager.j2 - MANAGER agent identity
            2. Strategy: manager_decomposition.j2 - Decomposition heuristics
            3. Task: task_decomposition.j2 - Task to decompose
            4. Output Format: subtask_list.j2 - JSON schema

        Args:
            task_description: The task to decompose into subtasks.
            agent_id: UUID of the MANAGER agent performing decomposition.
            agent_role: Role name (MANAGER or BOSS) for context.
            parent_task: Optional description of parent's task for context.

        Returns:
            Complete hierarchical prompt string ready for LLM.

        Raises:
            TemplateNotFound: If any required template file is missing.
        """
        try:
            # 1. System Prompt - MANAGER agent identity
            system = self.env.get_template("system/role_manager.j2").render()

            # 2. Strategy Prompt - Decomposition methodology
            strategy = self.env.get_template("strategies/manager_decomposition.j2").render()

            # 3. Task Prompt - Specific task to decompose
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role=agent_role,
                parent_task=parent_task,
            )

            # 4. Output Format - JSON schema for subtask list
            output_format = self.env.get_template("output_formats/subtask_list.j2").render()

            # Compose in hierarchical order
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"

        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
    ) -> str:
        """Build complete prompt for BOSS agent task delegation.

        Prompt Structure:
            1. System: role_boss.j2 - BOSS agent identity
            2. Strategy: boss_delegation.j2 - Strategic delegation approach
            3. Task: task_decomposition.j2 - Task to decompose
            4. Output Format: subtask_list.j2 - JSON schema

        Args:
            task_description: The high-level task to delegate.
            agent_id: UUID of the BOSS agent performing delegation.
            parent_task: Optional description of parent context (usually None for root BOSS).

        Returns:
            Complete hierarchical prompt string ready for LLM.

        Raises:
            TemplateNotFound: If any required template file is missing.
        """
        try:
            # 1. System Prompt - BOSS agent identity
            system = self.env.get_template("system/role_boss.j2").render()

            # 2. Strategy Prompt - Strategic delegation methodology
            strategy = self.env.get_template("strategies/boss_delegation.j2").render()

            # 3. Task Prompt - Specific task to delegate
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role="BOSS",
                parent_task=parent_task,
            )

            # 4. Output Format - JSON schema for subtask list
            output_format = self.env.get_template("output_formats/subtask_list.j2").render()

            # Compose in hierarchical order
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"

        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e
