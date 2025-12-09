"""Domain service for building hierarchical prompts from Jinja2 templates."""

from pathlib import Path
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


class PromptBuilder:
    """Compose hierarchical prompts: system → strategy → task → output format."""

    def __init__(
        self,
        template_dir: str | Path = "prompts",
        default_tool: str = "claude_code",
    ) -> None:
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        # Autoescape disabled: templates generate LLM prompts (plain text), not HTML
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
        """Build prompt for PENDING agent complexity evaluation."""
        try:
            system = self.env.get_template("system/role_pending.j2").render()
            strategy = self.env.get_template("strategies/complexity_evaluation.j2").render()
            task = self.env.get_template("tasks/complexity_evaluation.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                parent_task=parent_task,
            )
            output_format = self.env.get_template("output_formats/complexity_result.j2").render()
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
        """Build prompt for MANAGER agent task decomposition."""
        try:
            system = self.env.get_template("system/role_manager.j2").render(
                default_tool=self.default_tool,
            )
            strategy = self.env.get_template("strategies/manager_decomposition.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role=agent_role,
                parent_task=parent_task,
                default_tool=self.default_tool,
            )
            output_format = self.env.get_template("output_formats/subtask_list.j2").render(
                default_tool=self.default_tool,
            )
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
        """Build prompt for BOSS agent task delegation."""
        try:
            system = self.env.get_template("system/role_boss.j2").render(
                default_tool=self.default_tool,
            )
            strategy = self.env.get_template("strategies/boss_delegation.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role="BOSS",
                parent_task=parent_task,
                default_tool=self.default_tool,
            )
            output_format = self.env.get_template("output_formats/subtask_list.j2").render(
                default_tool=self.default_tool,
            )
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e
