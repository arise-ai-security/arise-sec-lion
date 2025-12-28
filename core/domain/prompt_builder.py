"""Domain service for building hierarchical prompts from Jinja2 templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.enums import AgentRole


class TemplateChain:
    """Fluent builder for chaining template renders."""

    __slots__ = ("_env", "_parts", "_error_context")

    def __init__(
        self,
        env: Environment,
        error_context: str = "template",
    ) -> None:
        self._env = env
        self._parts: list[str] = []
        self._error_context = error_context

    def _template_exists(self, name: str) -> bool:
        """Check if a template exists without raising."""
        try:
            self._env.get_template(name)
            return True
        except TemplateNotFound:
            return False

    def render(self, template: str, **kwargs: Any) -> Self:
        """Render a template and add to chain."""
        try:
            self._parts.append(self._env.get_template(template).render(**kwargs))
        except TemplateNotFound as e:
            raise TemplateNotFound(f"Required {self._error_context} not found: {e.name}") from e
        return self

    def render_if(self, condition: Any, template: str, **kwargs: Any) -> Self:
        """Render a template only if condition is truthy."""
        if condition:
            return self.render(template, **kwargs)
        return self

    def render_optional(self, template: str, **kwargs: Any) -> Self:
        """Render a template if it exists, skip silently otherwise."""
        if self._template_exists(template):
            self._parts.append(self._env.get_template(template).render(**kwargs))
        return self

    def with_security(self, overlay_name: str, **kwargs: Any) -> Self:
        """Inject security template if it exists. Looks in security/ directory."""
        return self.render_optional(f"security/{overlay_name}", **kwargs)

    def text(self, content: str) -> Self:
        """Add raw text to chain."""
        self._parts.append(content)
        return self

    def text_if(self, condition: Any, content: str) -> Self:
        """Add raw text only if condition is truthy."""
        if condition:
            self._parts.append(content)
        return self

    def build(self, separator: str = "\n\n") -> str:
        """Join all parts into final prompt."""
        return separator.join(self._parts)


class PromptBuilder:
    """Compose hierarchical prompts with automatic security injection."""

    def __init__(
        self,
        template_dir: str | Path,
        default_tool: str,
    ) -> None:
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701 - plain text prompts, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def chain(self) -> TemplateChain:
        """Create a new template chain builder."""
        return TemplateChain(self.env, "template")

    def _task_context(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole,
        parent_task: str | None = None,
    ) -> dict[str, Any]:
        """Build common task context dict."""
        return {
            "task_description": task_description,
            "agent_id": str(agent_id),
            "agent_role": agent_role.value.upper(),
            "parent_task": parent_task,
            "default_tool": self.default_tool,
        }

    def build_complexity_evaluation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
    ) -> str:
        """Build prompt for PENDING agent complexity evaluation."""
        return (
            self.chain()
            .render("system/role_pending.j2")
            .render("strategies/complexity_evaluation.j2")
            .render("tasks/complexity_evaluation.j2",
                    task_description=task_description,
                    agent_id=str(agent_id),
                    parent_task=parent_task)
            .render("output_formats/complexity_result.j2")
            .with_security("complexity_security.j2")
            .build()
        )

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition."""
        ctx = self._task_context(task_description, agent_id, agent_role, parent_task)
        tool = self.default_tool
        return (
            self.chain()
            .render("system/role_manager.j2", default_tool=tool)
            .with_security("manager_security.j2", default_tool=tool)
            .render("strategies/manager_decomposition.j2", default_tool=tool)
            .render("tasks/task_decomposition.j2", **ctx)
            .render("output_formats/subtask_list.j2", default_tool=tool)
            .build()
        )

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation."""
        ctx = self._task_context(task_description, agent_id, AgentRole.BOSS, parent_task)
        tool = self.default_tool
        return (
            self.chain()
            .render("system/role_boss.j2", default_tool=tool)
            .with_security("role_boss_security.j2", default_tool=tool)
            .render("strategies/boss_delegation.j2", default_tool=tool)
            .with_security("strategy_cve_benchmark.j2", default_tool=tool)
            .render("tasks/task_decomposition.j2", **ctx)
            .render("output_formats/subtask_list.j2", default_tool=tool)
            .with_security("output_format_benchmark.j2", default_tool=tool)
            .build()
        )

    def build_auto_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole,
        parent_task: str | None = None,
    ) -> str:
        """Automatically select appropriate prompt based on agent role."""
        match agent_role:
            case AgentRole.BOSS:
                return self.build_boss_delegation_prompt(task_description, agent_id, parent_task)
            case AgentRole.MANAGER:
                return self.build_manager_decomposition_prompt(
                    task_description, agent_id, agent_role, parent_task
                )
            case _:
                return self.build_complexity_evaluation_prompt(task_description, agent_id, parent_task)

    def build_worker_prompt(
        self,
        task_description: str,
        sibling_context: Any = None,
        workspace_context: str | None = None,
    ) -> str:
        """Build prompt for WORKER agent task execution."""
        chain = self.chain()

        # Sibling context (if provided)
        if sibling_context is not None:
            chain.render("worker/sibling_context.j2", **sibling_context.to_template_dict())

        # Instructions + security overlay
        chain.render("worker/execution_instructions.j2")
        chain.with_security("worker_guidance.j2")

        # Task description
        chain.text(f"<TASK>\n{task_description}\n</TASK>")

        # Workspace context (if provided)
        if workspace_context:
            chain.render("worker/workspace_context.j2", files=workspace_context)

        return chain.build()
