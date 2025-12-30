"""Domain service for building hierarchical prompts from Jinja2 templates."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.context import ParentContext
    from core.domain.cve_instance import CVEInstance
    from core.domain.services import RegisteredTask


def _detect_benchmark_branch(parent_context: ParentContext | None) -> str | None:
    """Detect which SEC-bench branch (builder/exploiter/fixer) this agent belongs to.

    Checks ancestry chain for top-level subtask keywords.
    Returns None if not in a benchmark run or branch not determinable.
    """
    if parent_context is None:
        return None

    # Check each ancestor's task summary (starting from immediate parent)
    for ancestor in parent_context.ancestry:
        task_lower = ancestor.task_summary.lower()
        # Builder keywords
        if any(kw in task_lower for kw in ("builder", "environment", "setup", "docker pull")):
            return "builder"
        # Exploiter keywords
        if any(kw in task_lower for kw in ("exploiter", "poc", "exploit", "proof of concept")):
            return "exploiter"
        # Fixer keywords
        if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
            return "fixer"

    return None


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

    def with_secbench(self, template_name: str, **kwargs: Any) -> Self:
        """Inject SEC-bench template if it exists. Looks in secbench/ directory."""
        return self.render_optional(f"secbench/{template_name}", **kwargs)

    def with_cve_context(self, cve_instance: CVEInstance | None) -> Self:
        """Inject SEC-bench CVE context if available.

        Renders secbench/context.j2 with CVE instance data.
        """
        if cve_instance is not None:
            return self.render_optional(
                "secbench/context.j2",
                **cve_instance.to_template_context(),
            )
        return self

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
            .render("core/roles/pending.j2")
            .render("core/strategies/complexity.j2")
            .text(f"<TASK_TO_EVALUATE>\n{task_description}\n</TASK_TO_EVALUATE>")
            .text_if(parent_task, f"<PARENT_TASK>\n{parent_task}\n</PARENT_TASK>")
            .render("core/output/complexity.j2")
            .with_security("complexity_overlay.j2")
            .build()
        )

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
        registered_tasks: list[RegisteredTask] | None = None,
        cve_instance: CVEInstance | None = None,
        parent_context: ParentContext | None = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition.

        Args:
            task_description: The task to decompose.
            agent_id: Current agent's ID.
            agent_role: Agent role (should be MANAGER).
            parent_task: Parent task description.
            registered_tasks: List of already-registered tasks (for dedup hint).
            cve_instance: SEC-bench CVE instance for benchmark runs.
            parent_context: Parent context for branch detection.
        """
        ctx = self._task_context(task_description, agent_id, agent_role, parent_task)
        tool = self.default_tool
        cve_ctx = cve_instance.to_template_context() if cve_instance else {}

        # Detect SEC-bench branch for specialized manager prompts
        branch = _detect_benchmark_branch(parent_context) if cve_instance else None

        # Also detect branch from task description if parent_context is None
        if branch is None and cve_instance:
            task_lower = task_description.lower()
            if any(kw in task_lower for kw in ("[builder]", "builder", "environment", "setup")):
                branch = "builder"
            elif any(kw in task_lower for kw in ("[exploiter]", "exploiter", "poc", "exploit")):
                branch = "exploiter"
            elif any(kw in task_lower for kw in ("[fixer]", "fixer", "patch", "fix")):
                branch = "fixer"

        # Use SEC-bench specific manager prompts if in benchmark branch
        if branch:
            return (
                self.chain()
                .render("core/roles/manager.j2", default_tool=tool)
                .with_cve_context(cve_instance)
                .render_if(branch == "builder", "secbench/manager/builder.j2", **cve_ctx, default_tool=tool)
                .render_if(branch == "exploiter", "secbench/manager/exploiter.j2", **cve_ctx, default_tool=tool)
                .render_if(branch == "fixer", "secbench/manager/fixer.j2", **cve_ctx, default_tool=tool)
                .build()
            )

        # Standard manager decomposition prompt
        return (
            self.chain()
            .render("core/roles/manager.j2", default_tool=tool)
            .with_security("manager_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool)
            .render("core/context/task.j2", **ctx, registered_tasks=registered_tasks)
            .render("core/output/subtasks.j2", default_tool=tool)
            .build()
        )

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        cve_instance: CVEInstance | None = None,
        registered_tasks: list[RegisteredTask] | None = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation.

        Args:
            task_description: The task to delegate.
            agent_id: Current agent's ID.
            parent_task: Parent task description.
            cve_instance: SEC-bench CVE instance for benchmark runs.
            registered_tasks: List of already-registered tasks (for dedup hint).
        """
        ctx = self._task_context(task_description, agent_id, AgentRole.BOSS, parent_task)
        tool = self.default_tool
        cve_ctx = cve_instance.to_template_context() if cve_instance else {}

        # SEC-bench mode: use streamlined boss prompt
        if cve_instance:
            return (
                self.chain()
                .render("core/roles/boss.j2", default_tool=tool)
                .with_cve_context(cve_instance)
                .render("secbench/boss.j2", **cve_ctx, default_tool=tool)
                .build()
            )

        # General mode: layered composition
        return (
            self.chain()
            .render("core/roles/boss.j2", default_tool=tool)
            .with_security("boss_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool)
            .render("core/context/task.j2", **ctx, registered_tasks=registered_tasks)
            .render("core/output/subtasks.j2", default_tool=tool)
            .build()
        )

    def build_auto_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole,
        parent_task: str | None = None,
        cve_instance: CVEInstance | None = None,
        registered_tasks: list[RegisteredTask] | None = None,
        parent_context: ParentContext | None = None,
    ) -> str:
        """Automatically select appropriate prompt based on agent role.

        Args:
            task_description: The task to process.
            agent_id: Current agent's ID.
            agent_role: Agent role (BOSS, MANAGER, or other).
            parent_task: Parent task description.
            cve_instance: SEC-bench CVE instance for benchmark runs.
            registered_tasks: List of already-registered tasks (for dedup hint).
            parent_context: Parent context for branch detection.
        """
        match agent_role:
            case AgentRole.BOSS:
                return self.build_boss_delegation_prompt(
                    task_description,
                    agent_id,
                    parent_task,
                    cve_instance,
                    registered_tasks,
                )
            case AgentRole.MANAGER:
                return self.build_manager_decomposition_prompt(
                    task_description,
                    agent_id,
                    agent_role,
                    parent_task,
                    registered_tasks,
                    cve_instance,
                    parent_context,
                )
            case _:
                return self.build_complexity_evaluation_prompt(
                    task_description, agent_id, parent_task
                )

    def build_worker_prompt(
        self,
        task_description: str,
        sibling_context: Any = None,
        workspace_context: str | None = None,
        cve_instance: CVEInstance | None = None,
        parent_context: ParentContext | None = None,
    ) -> str:
        """Build prompt for WORKER agent task execution.

        Args:
            task_description: The task to execute.
            sibling_context: Context from sibling workers (completed tasks).
            workspace_context: Files in the workspace.
            cve_instance: SEC-bench CVE instance for benchmark runs.
            parent_context: Parent context for branch detection.
        """
        # Pre-compute context dicts for chaining
        sibling_ctx = sibling_context.to_template_dict() if sibling_context else {}
        cve_ctx = cve_instance.to_template_context() if cve_instance else {}
        branch = _detect_benchmark_branch(parent_context) if cve_instance else None

        # SEC-bench mode: use streamlined worker prompts
        if branch:
            return (
                self.chain()
                .render_if(sibling_context, "core/context/sibling.j2", **sibling_ctx)
                .with_cve_context(cve_instance)
                .render_if(branch == "builder", "secbench/worker/builder.j2", **cve_ctx)
                .render_if(branch == "exploiter", "secbench/worker/exploiter.j2", **cve_ctx)
                .render_if(branch == "fixer", "secbench/worker/fixer.j2", **cve_ctx)
                .build()
            )

        # General mode: layered composition
        return (
            self.chain()
            .render_if(sibling_context, "core/context/sibling.j2", **sibling_ctx)
            .render("core/roles/worker.j2")
            .render("core/worker/execution.j2")
            .text(f"<TASK>\n{task_description}\n</TASK>")
            .render_if(workspace_context, "core/context/workspace.j2", files=workspace_context)
            .build()
        )
